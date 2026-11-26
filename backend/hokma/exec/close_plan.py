"""What the Close button of a rule notice would close, worked out BEFORE anything is sent (SPEC-026, piece 3390b, second round of Vera).

Closing is the most dangerous thing a button can do, so it never goes in one tap: the first tap only builds this plan and shows it, the second one
(a new token in Telegram, a confirm button in the chat) sends exactly that list. The rule of the plan:

- by default it closes only what THIS desk holds in the account (its own book, `book_entries.desk_id`), never the lots of another desk that shares the account,
  and never shares or ETF held long (they are long-term holdings: they are sold only if the owner chose it);
- only when the owner chose `block_and_flatten_all` as what happens at the limit of a loss or a drawdown rule of that account (in the rules of the desk or in
  the rules of the account) does it close the whole account, shares and other desks included, and the plan says so;
- what is NOT closed is listed too, with the reason, so the owner sees both halves before confirming.

`flatten_all_chosen` is the one place that answers "did the owner choose to flatten everything in this account?" and is the one piece 3390c reuses to tell
`block_and_flatten` from `block_and_flatten_all`."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from hokma.broker import rule_modes
from hokma.db import Database

ZERO = Decimal(0)
FLATTEN_ALL = "block_and_flatten_all"
SHARES = ("equity", "etf")


def flatten_all_chosen(u: Any, desk_id: int, account_id: int) -> bool:
    """True when a loss or drawdown rule that reaches this desk in this account has `block_and_flatten_all` as its on_trip: in the rules of the desk
    (all its accounts) or in the rules of the account (the whole account, or this desk inside it). The owner is the only one who writes it."""
    modes = (u.broker.get_limits(desk_id) or {}).get("rule_modes")
    if isinstance(modes, Mapping):
        for rule_id, entry in modes.items():
            if isinstance(entry, Mapping) and entry.get("on_trip") == FLATTEN_ALL and str(rule_id).partition(":")[0] in rule_modes.TRIP_CONTROLS:
                return True
    row = u.session.one("SELECT 1 AS x FROM account_rules WHERE account_id = :a AND (desk_id IS NULL OR desk_id = :d) AND on_trip = :o LIMIT 1",
                        {"a": account_id, "d": desk_id, "o": FLATTEN_ALL})
    return row is not None


def _is_shares(contract_id: str) -> bool:
    from hokma.contracts.identity import NoContract, parse_symbol

    try:
        return str(parse_symbol(contract_id).asset_type) in SHARES
    except NoContract:
        return False


def build(database: Database, desk_id: int, account_id: int) -> dict[str, Any]:
    """The plan: {"all": bool, "close": [{contract_id, qty (signed, what the account holds and will be closed), side, desks}], "keep": [{contract_id, qty, why,
    desks}]}. `why` of what stays is `long_holding` or `other_desk`. Quantities are text."""
    from hokma.backoffice.book import Book
    from hokma.broker import service
    from hokma.exec import prefilter

    with database.read() as u:
        broker, account = service.broker_of(u, desk_id, account_id)
        positions = prefilter._normalize_positions(broker.positions(account.id))
        everything = flatten_all_chosen(u, desk_id, account_id)
        linked = {r["desk_id"] for r in u.session.query("SELECT desk_id FROM desk_account_links WHERE account_id = :a", {"a": account_id})} | {desk_id}
        books = {d: Book.load(account_id, desk_id=d, session=u.session).cost_positions() for d in linked}
        names = {d: (u.desks.find(d).name if u.desks.find(d) is not None else str(d)) for d in linked}
    out: dict[str, Any] = {"all": everything, "close": [], "keep": []}
    for pos in positions:
        cid, qty = getattr(pos, "contract_id", None), Decimal(str(pos.qty))
        if qty == 0 or not cid:
            continue
        held = {d: books[d][cid].qty for d in books if cid in books[d] and books[d][cid].qty != 0}
        desks = [names[d] for d in sorted(held)]
        mine = held.get(desk_id, ZERO)
        if everything:
            out["close"].append(_line(cid, qty, desks))
            continue
        if qty > 0 and _is_shares(cid):                                    # shares and ETF held long: sold only when the owner chose to close everything
            out["keep"].append({**_line(cid, qty, desks), "why": "long_holding"})
            continue
        share = ZERO if mine == 0 or (mine > 0) != (qty > 0) else min(abs(mine), abs(qty)) * (1 if qty > 0 else -1)
        if share != 0:
            out["close"].append(_line(cid, share, [names[desk_id]]))
        rest = qty - share
        if rest != 0:                                                      # what the account holds and this desk does not: another desk's, or unknown to the book
            out["keep"].append({**_line(cid, rest, [n for d, n in names.items() if d != desk_id and d in held]), "why": "other_desk"})
    return out


def _line(contract_id: str, qty: Decimal, desks: list[str]) -> dict[str, Any]:
    return {"contract_id": contract_id, "qty": format(qty.normalize(), "f"), "side": "sell" if qty > 0 else "buy", "desks": desks}


def token(plan: Mapping[str, Any]) -> str:
    """A short fingerprint of a plan: the chat sends it back when it confirms, so the list that is closed is the one it showed and not a newer one."""
    import hashlib
    import json

    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def only_of(plan: Mapping[str, Any]) -> dict[str, Decimal]:
    """The signed quantity of each contract the plan closes: what `submit.flatten` is allowed to send, and nothing else."""
    return {line["contract_id"]: Decimal(line["qty"]) for line in plan.get("close", [])}


def text(plan: Mapping[str, Any], account_label: str = "") -> str:
    """The confirmation the owner reads, in his language: what will be closed and what stays."""
    from hokma.i18n import t

    def one(line: Mapping[str, Any]) -> str:
        who = ", ".join(line.get("desks") or []) or t("exec.notice.close_unknown_desk")
        reasons = {"long_holding": lambda: t("exec.notice.close_keep_long_holding"), "other_desk": lambda: t("exec.notice.close_keep_other_desk")}
        why = "" if "why" not in line else " [" + reasons[line["why"]]() + "]"
        return f"- {str(line['side']).upper()} {str(line['qty']).lstrip('-')} {line['contract_id']} ({who}){why}"

    parts = [t("exec.notice.close_head", account=account_label)]
    if plan.get("all"):
        parts.append(t("exec.notice.close_all"))
    parts += [one(line) for line in plan.get("close", [])] or [t("exec.notice.close_none")]
    if plan.get("keep"):
        parts.append(t("exec.notice.close_keep"))
        parts += [one(line) for line in plan["keep"]]
    parts.append(t("exec.notice.close_ask"))
    return "\n".join(parts)
