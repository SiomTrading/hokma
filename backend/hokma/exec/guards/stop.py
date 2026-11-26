"""The stop of an entry, before it goes out (SPEC-026 v2, section 4.4 a). The protection of an operation is its stop, put at the broker: Hokma does not value what the
order risks, does not reserve anything and does not invent a stop. It only refuses an entry that has no stop where one is mandatory, or whose stop cannot work:

  futures, micros, CFD     the stop is MANDATORY, long or short. The target is optional.
  shares and ETF long      no stop needed; a stop that is given has to be below the entry.
  shares short, naked      the stop is MANDATORY and above the entry (an option sold outright is the same; the sale of premium stays shut by `premium_selling`).
  bought option, vertical  none.

The side of the stop is read against the price of the order (its limit or stop price) or, in an order at market, the last quote the filter already has (`ctx.marks`).
That is the only price this check uses, and it values nothing. A STOP exit that does not cover the quantity of its entry is refused by `hokma.exec.native`
(`stop_invalid`, `risk.stop_qty`). An order that only closes never reaches this guard."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from hokma.exec.guards import GuardContext, GuardResult, blocked, passed
from hokma.exec.money import dec_or_none
from hokma.exec.proposal import Leg, OrderProposal
from hokma.i18n import t

OPTION_TYPES = ("option", "future_option")
MARGIN_TYPES = ("future", "cfd")                      # the stop is mandatory to enter, long or short


class StopRefused(Exception):
    """The entry cannot go as it is: `code` is stable and the text is the catalog key `risk.{code}` with `params`."""

    def __init__(self, code: str, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code, self.params = code, dict(params or {})


def _label(contract_id: str) -> str:
    return contract_id.split(":", 1)[-1].replace(":", " ")


def _entry_price(p: OrderProposal, leg: Leg, marks: Mapping[str, Decimal], *, single: bool) -> Decimal | None:
    """The price the side of the stop is read against: the limit or the stop price of the order (one leg), else the last quote of the contract."""
    if single:
        if p.order_type in ("limit", "stop_limit") and p.limit_price is not None:
            return p.limit_price
        if p.order_type == "stop" and p.stop_price is not None:
            return p.stop_price
    return dec_or_none(marks.get(leg.contract_id)) if marks else None


def _side(leg: Leg, entry: Decimal, stop: Decimal) -> None:
    ok = stop < entry if leg.side == "buy" else stop > entry
    if not ok:
        raise StopRefused("stop_invalid", {"contract": _label(leg.contract_id), "stop": stop, "entry": entry,
                                           "side": t("risk.below") if leg.side == "buy" else t("risk.above")})


def _stopped(p: OrderProposal, leg: Leg, marks: Mapping[str, Decimal], *, single: bool, mandatory: bool) -> None:
    """One entry leg: its stop where it is mandatory, and the side of the stop when there is one."""
    if p.stop_loss is None:
        if mandatory:
            raise StopRefused("stop_required", {"contract": _label(leg.contract_id)})
        return
    entry = _entry_price(p, leg, marks, single=single)
    if entry is None:
        raise StopRefused("stop_unpriced", {"contract": _label(leg.contract_id)})
    _side(leg, entry, p.stop_loss)


def check_stop(p: OrderProposal, sheets: Mapping[str, Any], marks: Mapping[str, Decimal] | None) -> None:
    """Raises StopRefused when an entry of the order has no stop where one is mandatory, the stop is on the wrong side or the price to read its side is unknown."""
    marks = marks or {}
    opening = [g for g in p.legs if g.effect == "open"]
    if not opening:
        return
    single = len(p.legs) == 1
    options = [g for g in opening if sheets[g.contract_id].asset_type in OPTION_TYPES]
    others = [g for g in opening if g not in options]
    if options and _vertical(options, sheets) is None:
        for g in (x for x in options if x.side == "sell"):                    # a naked or undefined sale: the stop is mandatory
            if len(options) != 1:
                raise StopRefused("stop_shape", {"contract": _label(g.contract_id)})
            _stopped(p, g, marks, single=single, mandatory=True)
    if len(others) > 1 and any(sheets[g.contract_id].asset_type in MARGIN_TYPES for g in others):
        raise StopRefused("stop_shape", {"contract": _label(others[1].contract_id)})
    for g in others:
        _stopped(p, g, marks, single=single, mandatory=sheets[g.contract_id].asset_type in MARGIN_TYPES or g.side == "sell")


def _vertical(options: Sequence[Leg], sheets: Mapping[str, Any]) -> bool | None:
    """True when the two option legs are a vertical of defined risk: same underlying, expiration and right, opposite sides, the same number of contracts."""
    if len(options) != 2:
        return None
    a, b = options
    sa, sb = sheets[a.contract_id], sheets[b.contract_id]
    if a.side == b.side or a.qty != b.qty or sa.put_call != sb.put_call or sa.expiration != sb.expiration or sa.underlying != sb.underlying:
        return None
    if sa.strike is None or sb.strike is None or sa.strike == sb.strike:
        return None
    return True


def words(exc: StopRefused) -> str:
    """The sentence of a refusal, in the language of the system. One literal t(...) per message, for the catalog check."""
    if exc.code == "stop_required":
        return t("risk.stop_required", **exc.params)
    if exc.code == "stop_invalid":
        return t("risk.stop_invalid", **exc.params)
    if exc.code == "stop_shape":
        return t("risk.stop_shape", **exc.params)
    return t("risk.stop_unpriced", **exc.params)


class StopGuard:
    name = "stop"
    applies_to_closing = False

    def check(self, ctx: GuardContext) -> GuardResult:
        try:
            check_stop(ctx.proposal, ctx.contracts, ctx.marks)
        except StopRefused as exc:
            return blocked(self.name, exc.code, words(exc))
        return passed(self.name)
