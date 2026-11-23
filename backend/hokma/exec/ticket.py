"""The ticket the owner reads (SPEC-018: DTS-08a) and the short messages of the execution. The ticket shows what will be sent, in the
language of the owner: symbol, side, quantity, multiplier, value, stop, target, risk in USD, the level of the desk and the short
fingerprint of the order. The fingerprint is the hash of the proposal: approving the ticket approves exactly that order."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from hokma.exec.money import ZERO, text as dtext
from hokma.exec.proposal import OrderProposal
from hokma.i18n import t

SHORT = 12

_WHY: dict[str, Callable[..., str]] = {         # one literal t(...) per message, for the catalog check
    "level_1": lambda **p: t("exec.why.level_1", **p),
    "hash_mismatch": lambda **p: t("exec.why.hash_mismatch", **p),
    "no_contract": lambda **p: t("exec.why.no_contract", **p),
    "already_sent": lambda **p: t("exec.why.already_sent", **p),
    "expired": lambda **p: t("exec.why.expired", **p),
    "rule_level": lambda **p: t("exec.why.rule_level", **p),
    "owner_required": lambda **p: t("exec.why.owner_required", **p),
    "not_found": lambda **p: t("exec.why.not_found", **p),
    "closed": lambda **p: t("exec.why.closed", **p),
    "no_account": lambda **p: t("exec.why.no_account", **p),
    "awaiting": lambda **p: t("exec.why.awaiting", **p),
    "rejected": lambda **p: t("exec.why.rejected", **p),
    "no_line": lambda **p: t("exec.why.no_line", **p),
    "line_mismatch": lambda **p: t("exec.why.line_mismatch", **p),
    "owner_risk_short": lambda **p: t("exec.why.owner_risk_short", **p),
    "owner_rejected": lambda **p: t("exec.why.owner_rejected", **p),
}


def why(code: str, **params: Any) -> str:
    return _WHY[code](**params)


def short(fingerprint: str) -> str:
    return fingerprint[:SHORT]


def legs_text(p: OrderProposal, contracts: Mapping[str, Any]) -> str:
    parts = []
    for leg in p.legs:
        sheet = contracts.get(leg.contract_id)
        mult = "?" if sheet is None else dtext(sheet.multiplier)
        parts.append(f"{leg.side.upper()} {dtext(leg.qty)} {leg.contract_id} x{mult} ({leg.effect})")
    return "; ".join(parts)


def order_value(p: OrderProposal, contracts: Mapping[str, Any], marks: Mapping[str, Decimal] | None = None) -> Decimal | None:
    """Value of what the order opens or closes: qty * price * multiplier. None when a price or a sheet is missing."""
    total = ZERO
    for leg in p.legs:
        sheet = contracts.get(leg.contract_id)
        price = p.limit_price if len(p.legs) == 1 and p.limit_price is not None else (p.stop_price if len(p.legs) == 1 and p.stop_price is not None
                                                                                       else (marks or {}).get(leg.contract_id))
        if sheet is None or price is None:
            return None
        total += abs(leg.qty * Decimal(price) * sheet.multiplier)
    return total


def ticket(p: OrderProposal, *, desk_name: str, level: int, contracts: Mapping[str, Any], marks: Mapping[str, Decimal] | None = None,
           clamps: tuple[dict, ...] = ()) -> str:
    none = t("exec.ticket.none")
    value = order_value(p, contracts, marks)
    lines = [t("exec.ticket.header", desk=desk_name),
             t("exec.ticket.legs", legs=legs_text(p, contracts)),
             t("exec.ticket.type", type=p.order_type, price=dtext(p.limit_price) or dtext(p.stop_price) or none),
             t("exec.ticket.value", value=none if value is None else f"{value:,.2f}"),
             t("exec.ticket.stop", stop=dtext(p.stop_loss) or none),
             t("exec.ticket.target", target=dtext(p.take_profit) or none),
             t("exec.ticket.risk", risk=dtext(p.risk_usd) or none),
             t("exec.ticket.level", level=str(level)),
             t("exec.ticket.fingerprint", fingerprint=short(p.fingerprint()))]
    for clamp in clamps:
        lines.append(t("exec.ticket.clamp", limit=str(clamp.get("limit")), from_qty=str(clamp.get("from_qty")), to_qty=str(clamp.get("to_qty"))))
    if p.thesis:
        lines.append(t("exec.ticket.thesis", thesis=p.thesis[:600]))
    return "\n".join(lines)
