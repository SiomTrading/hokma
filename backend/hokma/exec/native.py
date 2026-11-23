"""The filter for the native orders of SPEC-015 (`order_place`, `order_replace` in the chat and the app): an order written in the Schwab shape
is read as an order proposal and goes through the same guards as any proposal before it can reach the broker. Nothing sends to a broker
without the filter. An order that cannot be read as a proposal (a symbol with no contract id, an unknown strategy) is refused `cannot_check`:
what cannot be checked does not go. The guards that clamp cannot cut a native order silently, so a clamp is a refusal that says the limit."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from hokma.contracts import symbols
from hokma.contracts.identity import NoContract
from hokma.db import Database, get_database
from hokma.exec import prefilter
from hokma.exec.money import MoneyError, dec
from hokma.exec.proposal import Leg, OrderProposal, ProposalError

_TYPES = {"MARKET": "market", "LIMIT": "limit", "STOP": "stop", "STOP_LIMIT": "stop_limit", "NET_DEBIT": "limit", "NET_CREDIT": "limit",
          "NET_ZERO": "limit", "LIMIT_ON_CLOSE": "limit", "MARKET_ON_CLOSE": "market"}


INSTRUCTIONS = frozenset({"BUY", "SELL", "BUY_TO_COVER", "SELL_SHORT", "SELL_SHORT_EXEMPT", "BUY_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_OPEN", "SELL_TO_CLOSE"})
ALL_SHARES_INSTRUCTIONS = frozenset({"SELL", "SELL_TO_CLOSE", "BUY_TO_CLOSE", "BUY_TO_COVER"})


class Unconvertible(ValueError):
    pass


def _nodes(order: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Every node of the order, parents first, children at any depth (as the rules read them)."""
    if not isinstance(order, dict) or depth > 8:
        raise Unconvertible("the order is not an object or is nested too deep")
    out = [order]
    for child in order.get("childOrderStrategies") or []:
        out += _nodes(child, depth + 1)
    return out


def _node_proposal(desk_id: int, node: dict[str, Any], *, created_by: str, line: str) -> list[tuple[OrderProposal, bool]]:
    """One node as proposals: the legs with a quantity as one proposal, and the ALL_SHARES legs as another (they carry no quantity of their own).
    Nothing is skipped: an all-shares leg is a one unit close of that symbol, so the symbol list and every other guard still read it, and it
    is exempt only where the real positions say it closes something."""
    legs, all_legs = [], []
    raw = node.get("orderLegCollection") or []
    if not raw:
        return []
    for leg in raw:
        if not isinstance(leg, dict) or not isinstance(leg.get("instrument"), dict):
            raise Unconvertible("a leg has no instrument")
        instruction = leg.get("instruction")
        if instruction not in INSTRUCTIONS:
            raise Unconvertible("a leg has an instruction that is not one of the known ones")
        side = "buy" if instruction.startswith("BUY") else "sell"
        try:
            cid = symbols.from_provider("schwab", str(leg["instrument"].get("symbol") or ""))
            if leg.get("quantityType") == "ALL_SHARES":
                # All shares can only close what is held, and only shares: an exact list of closing instructions and the equity asset type.
                if instruction not in ALL_SHARES_INSTRUCTIONS or leg["instrument"].get("assetType") != "EQUITY":
                    raise Unconvertible("ALL_SHARES is only read on a sell or a cover of an equity")
                all_legs.append(Leg(cid, side, "close", Decimal(1)))
            else:
                legs.append(Leg(cid, side, "close" if instruction.endswith("CLOSE") else "open", dec(leg.get("quantity"), "quantity")))
        except (NoContract, MoneyError, ProposalError) as exc:
            raise Unconvertible(str(exc)[:120]) from exc
    if len({g.contract_id for g in legs}) != len(legs) or len({g.contract_id for g in all_legs}) != len(all_legs):
        raise Unconvertible("a contract appears in two legs of one node")
    kind = _TYPES.get(str(node.get("orderType") or "").upper(), "market")
    try:
        out: list[tuple[OrderProposal, bool]] = []                 # (proposal, synthetic): an all-shares close has no real quantity
        if legs:
            out.append((OrderProposal(desk_id, line, "desk", tuple(legs), kind,
                                     limit_price=dec(node["price"], "price") if node.get("price") is not None and kind in ("limit", "stop_limit") else None,
                                     stop_price=dec(node["stopPrice"], "stopPrice") if node.get("stopPrice") is not None and kind in ("stop", "stop_limit") else None,
                                     tif="gtc" if "GOOD_TILL" in str(node.get("duration") or "") else "day", created_by=created_by[:80] or "owner"), False))
        if all_legs:
            out.append((OrderProposal(desk_id, line, "desk", tuple(all_legs), "market", created_by=created_by[:80] or "owner"), True))
        return out
    except (ProposalError, MoneyError) as exc:
        raise Unconvertible(str(exc)[:120]) from exc


def _signed(leg: Leg) -> Decimal:
    return leg.qty if leg.side == "buy" else -leg.qty


class _Tree:
    """One node of the order as it was read: its kind, its proposals (real and all-shares), its real legs and its children."""

    def __init__(self, kind: str, props: list[tuple[OrderProposal, bool]], children: list["_Tree"]) -> None:
        self.kind, self.props, self.children = kind, props, children
        self.legs = [g for p, _synthetic in props for g in p.legs]


def _parse(node: dict[str, Any], *, desk_id: int, created_by: str, line: str) -> _Tree:
    props = _node_proposal(desk_id, node, created_by=created_by, line=line)
    kind = str(node.get("orderStrategyType") or "SINGLE").upper()
    raw = node.get("childOrderStrategies") or []
    return _Tree(kind, props, [_parse(c, desk_id=desk_id, created_by=created_by, line=line) for c in raw])


def _exit_nodes(t: _Tree) -> list[list[_Tree]]:
    """The exits under a root, as groups: a lone single is a group of one, an OCO is a group of its branches. Only these two shapes read."""
    groups: list[list[_Tree]] = []
    for child in t.children:
        if child.kind == "SINGLE" and child.props and not child.children:
            groups.append([child])
        elif child.kind == "OCO" and not child.props and child.children and all(b.kind == "SINGLE" and b.props and not b.children for b in child.children):
            groups.append(list(child.children))
        else:
            raise Unconvertible("the exits of a TRIGGER can only be singles or an OCO of singles")
    return groups


def _exit_budget(groups: list[list[_Tree]], root_side: dict[str, str] | None) -> dict[str, Decimal]:
    """What the whole tree can take out of each contract, counted once: the exits of a TRIGGER add up, the branches of an OCO count the biggest.
    An all-shares exit takes everything of its contract, so nothing else of that contract may exit in the same tree."""
    sides: dict[str, str] = {}
    count: dict[str, int] = {}
    shares: set[str] = set()
    demand: dict[str, Decimal] = {}
    for group in groups:
        biggest: dict[str, Decimal] = {}
        for branch in group:
            for g in branch.legs:
                if root_side is not None and root_side.get(g.contract_id) in (None, g.side):
                    raise Unconvertible("an exit has to be on a contract of the root, on the opposite side")
                if sides.setdefault(g.contract_id, g.side) != g.side:
                    raise Unconvertible("the exits of one contract are on both sides")
                count[g.contract_id] = count.get(g.contract_id, 0) + 1
                biggest[g.contract_id] = max(biggest.get(g.contract_id, Decimal(0)), g.qty)
            for p, synthetic in branch.props:
                if synthetic:
                    shares.update(g.contract_id for g in p.legs)
        for cid, q in biggest.items():
            demand[cid] = demand.get(cid, Decimal(0)) + q
    if any(count[c] > 1 for c in shares):
        raise Unconvertible("an all-shares exit takes everything of its contract: nothing else of it can exit in the same order")
    return demand


def _walked(desk_id: int, order: Any, *, created_by: str, line: str) -> list[tuple[OrderProposal, dict[str, Decimal], dict[str, Decimal] | None, bool]]:
    """Only these shapes read; anything else is `cannot_check` and never goes by itself:
    (a) a SINGLE; (b) an OCO whose branches are all exits of the same contracts; (c) a TRIGGER with an entry root and, as children, only exits
    (a single or an OCO of singles) of the root's contracts. Each record is (proposal, what the root opens, the exit budget of the whole tree
    or None when the proposal is not an exit, synthetic)."""
    _nodes(order)
    tree = _parse(order, desk_id=desk_id, created_by=created_by, line=line)
    out: list[tuple[OrderProposal, dict[str, Decimal], dict[str, Decimal] | None, bool]] = []
    if tree.kind == "SINGLE":
        if tree.children or not tree.props:
            raise Unconvertible("a single order has legs and no children")
        if len({g.contract_id for g in tree.legs}) != len(tree.legs):
            raise Unconvertible("a contract appears in two legs of one node")
        out += [(p, {}, None, s) for p, s in tree.props]
    elif tree.kind == "OCO":
        if tree.props or not tree.children or not all(b.kind == "SINGLE" and b.props and not b.children for b in tree.children):
            raise Unconvertible("an OCO is read only as branches that are singles")
        if len({frozenset(g.contract_id for g in b.legs) for b in tree.children}) != 1:
            raise Unconvertible("the branches of an OCO have to exit the same contracts")
        budget = _exit_budget([list(tree.children)], None)
        for b in tree.children:
            out += [(p, {}, budget, s) for p, s in b.props]
    elif tree.kind == "TRIGGER":
        if not tree.props or not tree.children or any(s for _p, s in tree.props):
            raise Unconvertible("a TRIGGER is read only with an entry of its own (no all-shares) and exits as children")
        root_side: dict[str, str] = {}
        credit: dict[str, Decimal] = {}
        for g in tree.legs:
            if g.contract_id in root_side:
                raise Unconvertible("a contract appears in two legs of one node")
            root_side[g.contract_id] = g.side
            credit[g.contract_id] = _signed(g)
        groups = _exit_nodes(tree)
        budget = _exit_budget(groups, root_side)
        out += [(p, {}, None, s) for p, s in tree.props]
        for group in groups:
            for b in group:
                out += [(p, dict(credit), budget, s) for p, s in b.props]
    else:
        raise Unconvertible("the kind of order is not one of SINGLE, OCO or TRIGGER")
    return out


def to_proposals(desk_id: int, order: Any, *, created_by: str, line: str) -> list[OrderProposal]:
    """One proposal per node of the order, every leg of every child. Nothing is skipped."""
    return [p for p, *_rest in _walked(desk_id, order, created_by=created_by, line=line)]


def check(desk_id: int, order: Any, *, preview: Any, who: Any, db: Database | None = None) -> dict[str, Any] | None:
    """None when the order may go on to the rules of SPEC-015; otherwise the refusal in the shape of `rules.evaluate`."""
    database = db or get_database()
    with database.read() as u:
        row = u.session.one("SELECT line FROM desk_profile WHERE desk_id = :d", {"d": desk_id})
    try:
        walked = _walked(desk_id, order, created_by=getattr(who, "agent", None) or "owner", line=row["line"] if row else "DT")
    except Unconvertible as exc:
        return _refused("cannot_check", f"the order cannot be checked against the limits, so it is not sent: {exc}")
    for proposal, credit, budget, _synthetic in walked:
        # Only the rules of SPEC-015 are skipped: the service already evaluated them with the preview and its own words. Every other guard
        # (the symbol list included) runs for every node. An exit reads the live positions plus what its ROOT opens (never what a sibling
        # opens) and is held to the budget of the whole tree.
        virtual = tuple(SimpleNamespace(contract_id=c, qty=q, market_value=None, multiplier=None, day_pl=None) for c, q in credit.items() if q != 0)
        result = prefilter.run(desk_id, proposal, db=database, preview=preview, skip=("broker_rules",), virtual=virtual, exit_budget=budget)
        if result.verdict == "BLOCK":
            block = result.blocked_by
            return _refused(block.code if block else "blocked", block.why if block else "")
        if result.clamps:
            return _refused("limit_clamp", "the order is over a limit of the desk: " + "; ".join(str(c.get("why")) for c in result.clamps))
    return None


def _refused(code: str, why: str) -> dict[str, Any]:
    return {"result": "refused", "code": code, "why": why, "flags": [], "numbers": {}}
