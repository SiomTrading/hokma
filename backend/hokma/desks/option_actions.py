"""Actions of the Option detail panel of a desk (SPEC-028, delivery 6, piece 3339-B): what the panel opens with, the strategies the owner saved and the
proposal of an order.

- The selection is the strategy the panel shows (the one the owner opened from the Strategies panel, the one the chief opened by chat, or the one the owner
  picked). It is kept in memory like the last consultation of the Strategies panel: it is a convenience of the screen, not a record.
- Saving keeps the legs of a strategy under a strategy_key of the desk, inside desks.layout (no table). The key is the one SPEC-027 binds to an account.
- Proposing sends the strategy through the one door of execution (hokma.exec.submit) as a desk proposal: it never sends by itself. The account is resolved
  there from the strategy_key (SPEC-027 section 5), the rules of the owner (SPEC-026) and the guards decide, and the level of the desk says who approves.
  Selling premium is refused here before the door while the switch of the desk is closed, with its reason. A desk without an account, or a strategy without
  one, is not an error: the answer is "needs_account" with the accounts of the desk, so the screen asks which one (or opens Add account).
"""
from __future__ import annotations

import dataclasses
import logging
import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from hokma.desks import option_detail, service
from hokma.desks.errors import DeskError
from hokma.i18n import t

log = logging.getLogger("hokma.desks")
PRINCIPAL = "principal"
MAX_SAVED = 30
MAX_NAME = 60
MAX_KEY = 40
SHARES_PER_CONTRACT = 100

_SELECTION: dict[int, dict[str, Any]] = {}


def _bad() -> DeskError:
    return DeskError("bad_option_detail_args", 422)


# ---- the selection of the panel ---------------------------------------------------------------------------------------------------------------

def _form(req: dict[str, Any], strategy_key: str | None = None, name: str | None = None) -> dict[str, Any]:
    """The request in a plain form the panel can send back as it is (dates as text)."""
    return {"symbol": req["symbol"], "expiration": req["expiration"].isoformat(), "structure": req["structure"], "strikes": req["strikes"], "legs": req["legs"],
            "quantity": req["quantity"], "range_pct": req["range_pct"], "iv_mult": req["iv_mult"], "strategy_key": strategy_key, "name": name}


def _clean_key(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
        raise _bad()
    return value.strip()


def remember(desk_id: int, symbol: Any, expiration: Any, legs: Any = None, structure: Any = None, strikes: Any = None, quantity: Any = None,
             range_pct: Any = None, iv_mult: Any = None, strategy_key: Any = None, name: Any = None) -> dict[str, Any]:
    """Keep the strategy the panel opens with. The request is checked like the one of the detail; nothing is read from the market here."""
    req = option_detail.check_arguments(symbol, expiration, legs, structure, strikes, quantity, range_pct, iv_mult)
    label = name.strip()[:MAX_NAME] if isinstance(name, str) and name.strip() else None
    _SELECTION[desk_id] = _form(req, _clean_key(strategy_key), label)
    return {"selection": _SELECTION[desk_id]}


def selection(desk_id: int) -> dict[str, Any]:
    return {"selection": _SELECTION.get(desk_id)}


def forget(desk_id: int) -> None:
    _SELECTION.pop(desk_id, None)


# ---- saved strategies -------------------------------------------------------------------------------------------------------------------------

def make_key(name: Any) -> str:
    """The strategy_key of a name: lower case letters, digits and hyphens, at most MAX_KEY characters."""
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
        raise DeskError("bad_saved_strategy", 422)
    ascii_name = unicodedata.normalize("NFKD", name.strip()).encode("ascii", "ignore").decode()
    key = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:MAX_KEY].strip("-")
    if not key or key == PRINCIPAL:
        raise DeskError("bad_saved_strategy", 422)
    return key


def saved_list(desk_id: int) -> dict[str, Any]:
    service.get_desk(desk_id)
    items = service.saved_strategies(desk_id)
    return {"strategies": [{"key": k, **v} for k, v in sorted(items.items(), key=lambda kv: kv[1].get("saved_at") or "")]}


def save(desk_id: int, name: Any, symbol: Any, expiration: Any, legs: Any = None, structure: Any = None, strikes: Any = None, quantity: Any = None) -> dict[str, Any]:
    """Save the strategy under the key of its name (the same name replaces it). The legs are the ones of the request: a premium given stays, one that is not
    given is read again from the chain when the strategy is opened."""
    key = make_key(name)
    req = option_detail.check_arguments(symbol, expiration, legs, structure, strikes, quantity)
    record = {"name": name.strip(), "symbol": req["symbol"], "expiration": req["expiration"].isoformat(), "structure": req["structure"], "strikes": req["strikes"],
              "legs": req["legs"], "quantity": req["quantity"], "saved_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    service.save_strategy(desk_id, key, record, MAX_SAVED)
    return {"key": key, **record}


def delete_saved(desk_id: int, key: str) -> dict[str, Any]:
    """Forget the legs of a saved strategy. The account bound to its key (SPEC-027) stays as it was: it is the owner's to remove."""
    service.delete_saved_strategy(desk_id, key)
    return saved_list(desk_id)


def _from_saved(desk_id: int, key: str) -> dict[str, Any] | None:
    return service.saved_strategies(desk_id).get(key)


# ---- the proposal -----------------------------------------------------------------------------------------------------------------------------

def _contract_ids(symbol: str, legs: list[dict[str, Any]], day: str) -> list[tuple[str, str, Decimal, dict[str, Any]]]:
    """(contract id, side, quantity, leg) of each leg. The ids are checked by the parser of the contracts module."""
    from hokma.contracts.identity import NoContract, canonical_id, parse_canonical, parse_symbol

    out = []
    for leg in legs:
        if leg["instrument"] == "stock":
            cid = canonical_id(parse_symbol(symbol))
            qty = Decimal(int(leg["quantity"])) if float(leg["quantity"]) == int(leg["quantity"]) else Decimal(str(leg["quantity"]))
        else:
            strike = Decimal(str(leg["strike"])).normalize()
            cid = f"OPT:{symbol}:{day}:{'C' if leg['kind'] == 'call' else 'P'}:{format(strike, 'f')}"
            if float(leg["quantity"]) != int(leg["quantity"]):
                raise _bad()                                           # a fraction of a contract is not an order
            qty = Decimal(int(leg["quantity"]))
        try:
            parse_canonical(cid)
        except NoContract:
            raise _bad() from None
        out.append((cid, "buy" if leg["position"] == "long" else "sell", qty, leg))
    return out


def _ratio(ids: list[tuple[str, str, Decimal, dict[str, Any]]]) -> bool:
    """True when the option legs differ in quantity (a 1:2 ratio): they are not one of the structures the desk sends, and the order is not guessed from the request."""
    return len({qty for _cid, _side, qty, leg in ids if leg["instrument"] == "option"}) > 1


def _answer(status: str, code: str, why: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "code": code, "why": why, **extra}


def _accounts_of(desk_id: int) -> list[dict[str, Any]]:
    from hokma.broker import desk_accounts
    from hokma.db import get_database

    with get_database().read() as u:
        return [{k: x.get(k) for k in ("id", "name", "broker", "mask")} for x in desk_accounts.of_desk(u, desk_id).get("accounts", [])]


def propose(desk_id: int, symbol: Any, expiration: Any, legs: Any = None, structure: Any = None, strikes: Any = None, quantity: Any = None,
            strategy_key: Any = None, who: Any = None, created_by: str = "option_detail_panel") -> dict[str, Any]:
    """Send the strategy to the door of execution as a proposal of the desk. The answer has a `status`:
    needs_account (nothing was recorded: the screen asks which account, or opens Add account), no_data (the chain cannot answer),
    refused (the switch of premium selling, a guard or a rule said no, with its `why`), analysis_only (the level of the desk only analyzes),
    awaiting_owner (it waits for the owner in Operations), sent or filled (a rule of level 3 of the owner allowed it)."""
    from hokma import exec as hexec
    from hokma.broker import service as broker_service
    from hokma.db import get_database
    from hokma.desks.swo import premium_switch, schema
    from hokma.desks.trading import sizing
    from hokma.exec.proposal import Leg, ProposalError
    from hokma.exec.submit import profile_line

    req = option_detail.check_arguments(symbol, expiration, legs, structure, strikes, quantity)
    key = _clean_key(strategy_key) or PRINCIPAL
    if req["quantity"] != int(req["quantity"]):
        raise _bad()
    n = int(req["quantity"])
    sym, day = req["symbol"], req["expiration"]
    database = get_database()
    if profile_line(database, desk_id) != "SWO":
        return _answer("refused", "not_options_desk", t("desks.option_detail.not_options_desk"))
    if day < option_detail._today():
        return _answer("no_data", "expired", t("desks.option_detail.no_data", source=t("desks.option_detail.source_chain"), reason=t("desks.option_detail.expired")))
    chain = option_detail._load_chain(desk_id, sym, day)
    if not chain["ok"]:
        return _answer("no_data", chain["code"], t("desks.option_detail.no_data", source=chain["source"], reason=chain["reason"]))
    resolved = option_detail._legs_from_structure(req, chain) if req["structure"] else option_detail._legs_from_request(req, chain)
    if isinstance(resolved, str):
        return _answer("no_data", "no_quote", t("desks.option_detail.no_data", source=chain["source"], reason=resolved))
    try:
        ids = _contract_ids(sym, resolved, day.isoformat())
        order_legs = tuple(Leg(cid, side, "open", qty) for cid, side, qty, leg in ids)
        derived = schema.derive_structure(order_legs)
    except (ProposalError, DeskError):
        return _answer("refused", "bad_proposal", t("desks.option_detail.propose_unreadable"))
    if derived is None and _ratio(ids):
        return _answer("refused", "bad_proposal", t("desks.option_detail.propose_ratio"))
    if derived is None or (req["structure"] and derived != req["structure"] and not (derived == "short_put" and req["structure"] == "cash_secured_put")):
        return _answer("refused", "bad_proposal", t("desks.option_detail.propose_unreadable"))
    name = req["structure"] or derived
    n = int(next(qty for _cid, _side, qty, leg in ids if leg["instrument"] == "option"))      # the contracts of the order: what each leg says, all equal for the structure derived
    try:
        premium_switch.require_offerable(name, desk_id, sym)
    except premium_switch.PremiumSwitchClosed as closed:
        return _answer("refused", "premium_selling_closed", str(closed), structure=name, locked="premium_selling")
    with database.read() as u:
        found = broker_service.resolve_account(u, desk_id, key)
    if found.state != "ok":
        none = found.state == "analysis_only"
        return _answer("needs_account", "no_account" if none else "strategy_without_account",
                       t("desks.option_detail.needs_account_none") if none else t("desks.option_detail.needs_account_pick"),
                       strategy_key=key, structure=name, accounts=_accounts_of(desk_id))
    options = [(leg, cid) for cid, _side, _qty, leg in ids if leg["instrument"] == "option"]
    net = sum((Decimal(str(leg["premium"])) * (1 if leg["position"] == "long" else -1) for leg, _ in options), Decimal(0)).quantize(Decimal("0.01"))
    if net == 0:
        return _answer("refused", "bad_proposal", t("desks.option_detail.propose_unreadable"))
    stock = next((leg for leg in resolved if leg["instrument"] == "stock"), None)
    infos = [sizing.OptionLegInfo("C" if leg["kind"] == "call" else "P", "buy" if leg["position"] == "long" else "sell", Decimal(str(leg["strike"]))) for leg, _ in options]
    try:
        loss_unit = sizing.max_loss_per_unit_usd(name, infos, net, Decimal(str(chain["multiplier"])), None if stock is None else Decimal(str(chain["spot"])))
    except sizing.SizingError:
        return _answer("refused", "bad_proposal", t("desks.option_detail.propose_unreadable"))
    max_loss = None if loss_unit is None else (loss_unit * n).quantize(Decimal("0.01"))
    first = ", ".join(f"{leg['position']} {leg['kind']} {leg['strike']:g}" for leg, _ in options)
    thesis = t("desks.option_detail.propose_thesis", structure=name, symbol=sym, date=day.isoformat(), legs=first, qty=n)
    try:
        proposal = schema.SWOProposal(desk_id=desk_id, line="SWO", origin="desk", legs=order_legs, order_type="limit", limit_price=abs(net), tif="day",
                                      risk_usd=max_loss, thesis=thesis, created_by=created_by, strategy_key=key, structure=name, expiration=day, net_premium=net,
                                      max_loss_usd=max_loss, underlying=sym)
    except (ProposalError, InvalidOperation) as exc:
        return _answer("refused", "bad_proposal", str(exc))
    who = who or broker_service.Who(agent=created_by)
    order = proposal.to_order()
    waiting = _waiting_twin(database, desk_id, found.account.id, order.fingerprint())
    if waiting is not None:                  # a double POST: the proposal that already waits for the owner is the answer, a second one is not recorded
        return {**waiting.to_dict(), "strategy_key": key, "structure": name, "net_premium": str(net), "limit_price": str(abs(net)),
                "max_loss_usd": None if max_loss is None else str(max_loss), "quantity": n}
    try:
        result = hexec.submit(order, who=who)
    except broker_service.BrokerError as exc:
        return _answer("refused", exc.code, exc.message, strategy_key=key, structure=name)
    out = {**result.to_dict(), "strategy_key": key, "structure": name, "net_premium": str(net), "limit_price": str(abs(net)),
           "max_loss_usd": None if max_loss is None else str(max_loss), "quantity": n}
    if result.status == "awaiting_owner" and result.code == "needs_account":          # the account was taken away between the check and the door
        out.update(status="needs_account", code="strategy_without_account", accounts=_accounts_of(desk_id))
    return out


def _waiting_twin(database: Any, desk_id: int, account_id: int, fingerprint: str) -> Any:
    """The proposal of this desk and account with this fingerprint that still waits for the owner (the oldest), as an ExecResult; None when there is none.
    What is past its wait expires first and is never the answer; one made at another level of the desk is not reused either (a new one is recorded)."""
    from hokma.exec import approvals, levels, submit as door, ticket

    approvals.expire_pending(database)
    level, _rules = levels.get_level(desk_id, db=database)
    with database.read() as u:
        rows = u.session.query("SELECT * FROM exec_proposals WHERE desk_id = :d AND account_id = :a AND fingerprint = :f AND status = 'awaiting_owner' "
                               "AND level = :l AND decision_id IS NOT NULL ORDER BY id", {"d": desk_id, "a": account_id, "f": fingerprint, "l": level})
        row = None
        for r in rows:
            decision = u.decisions.get(r["decision_id"])
            if decision is not None and decision.status == "proposed" and not approvals._expired(decision, r):
                row = r
                break
        desk = u.desks.find(desk_id)
    if row is None:
        return None
    found = door.status(row["id"], db=database)
    return dataclasses.replace(found, why=ticket.why("awaiting", desk=desk.name if desk is not None else str(desk_id)))


def propose_saved(desk_id: int, strategy_key: str, quantity: Any = None, who: Any = None, created_by: str = "option_detail_panel") -> dict[str, Any]:
    """Propose a saved strategy by its key (the chat does it)."""
    record = _from_saved(desk_id, strategy_key)
    if record is None:
        raise DeskError("saved_strategy_not_found", 404)
    return propose(desk_id, record["symbol"], record["expiration"], record["legs"], record["structure"], record["strikes"],
                   quantity if quantity is not None else record["quantity"], strategy_key, who, created_by)


# ---- what the chat reads -----------------------------------------------------------------------------------------------------------------

def chat_view(result: dict[str, Any]) -> dict[str, Any]:
    """The detail as the chief reads it: the figures and the events, not the table. The panel has the table and the charts."""
    if not result.get("ok"):
        return {"ok": False, "symbol": result.get("symbol"), "expiration": result.get("expiration"), "message": result.get("message")}
    m = result["metrics"]
    curves = result["curves"]
    return {"ok": True, "symbol": result["symbol"], "expiration": result["expiration"], "days": result["days"], "spot": result["spot"], "structure": result.get("structure"),
            "legs": [{k: x.get(k) for k in ("instrument", "position", "kind", "strike", "premium", "quantity", "iv_used")} for x in result["legs"]],
            "net_premium_usd": m["net_premium_usd"], "premium_type": m["premium_type"], "max_profit_usd": m["max_profit_usd"], "unbounded_profit": m["unbounded_profit"],
            "max_loss_usd": m["max_loss_usd"], "unbounded_loss": m["unbounded_loss"], "breakevens_at_expiration": m["breakevens"], "p_profit": m["p_profit"],
            "expected_value_usd": m["expected_value_usd"], "breakevens_half_term_approximate": curves.get("mid_breakevens"),
            "events": {k: (v.get("message") if isinstance(v, dict) else v) for k, v in result["events"].items() if k != "expiration"}, "warnings": result["warnings"],
            "source": result["source"], "as_of": result["as_of"]}
