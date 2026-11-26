"""The specification of a backtest run (SPEC-020, section 4.2). One frozen value that fully determines a run.

`Line` is the one of `hokma.desks.core.signal`. The spec is hashed with `hokma.desks.core.hashing.content_hash`: two runs with the
same fingerprint, the same data and the same seed give the same result byte for byte.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Sequence

from hokma.contracts.identity import ContractSheet
from hokma.desks.core.hashing import content_hash
from hokma.desks.core.signal import Line

BARS = ("1m", "5m", "15m", "1h", "1d", "1w")
Bar = Literal["1m", "5m", "15m", "1h", "1d", "1w"]


class SpecError(ValueError):
    """The spec is not valid. `code` is stable: forbidden_fill, bad_spec."""

    code = "bad_spec"


class ForbiddenFill(SpecError):
    code = "forbidden_fill"


class BarNotAllowed(SpecError):
    code = "bar_not_allowed_for_line"


# P-14: the time scale of each line. DT runs on intraday bars; every other line on daily or weekly bars.
ALLOWED_BARS: dict[str, tuple[str, ...]] = {
    "DT": ("1m", "5m", "15m", "1h"),
    "SWC": ("1d", "1w"),
    "SWO": ("1d", "1w"),
    "LP": ("1d", "1w"),
    "RF": ("1d", "1w"),
}


@dataclass(frozen=True)
class FillRules:
    next_open_ticks: int = 1          # decided at bar N close, filled at bar N+1 open plus ticks against (B-05)
    stop_first: bool = True           # stop and target in the same bar: the stop fills (B-06)
    gap_fills_at_open: bool = True    # a stop crossed by a gap fills at the open, not at the stop (L-13)
    model: Literal["one_tick", "probabilistic"] = "one_tick"   # "best_price" is forbidden (N-09)
    reject_prob: float = 0.0          # robustness runs only (V-06)
    trade_on_close: bool = False      # filling at the close of the signal bar is forbidden (B-15)

    def __post_init__(self) -> None:
        if self.trade_on_close:
            raise ForbiddenFill("filling at the close of the signal bar is forbidden: the order fills at the next open")
        if self.model not in ("one_tick", "probabilistic"):
            raise ForbiddenFill(f"fill model {self.model!r} is not allowed: a best price fill is forbidden")
        if not self.stop_first or not self.gap_fills_at_open:
            raise ForbiddenFill("the optimistic fill rules (target before stop, stop filled at its price through a gap) are forbidden")
        if not isinstance(self.next_open_ticks, int) or isinstance(self.next_open_ticks, bool) or self.next_open_ticks < 1:
            raise ForbiddenFill("a fill at the next open needs at least one tick against")
        if not 0.0 <= float(self.reject_prob) < 1.0:
            raise SpecError("reject_prob is in [0, 1)")


@dataclass(frozen=True)
class CostModel:
    fee_table: str                    # file in backend/hokma/broker/fees/, the same one the simulated broker reads (L-16, L-17)
    slippage_ticks: int = 1           # per side, through hokma.broker.costs.fill_price (L-14)
    impact: Literal["none", "volume_share", "sqrt"] = "none"   # L-15, same model as exec/tca.py
    cfd_swap: bool = False            # SWC overnight financing (N-28)


@dataclass(frozen=True)
class BacktestSpec:
    strategy_id: str
    strategy_version: int
    params: Mapping[str, Any]         # validated by factory.params (B-03)
    line: Line
    instruments: tuple[str, ...]      # canonical contract ids (H2 contracts); continuous futures as "CONT:NQ"
    bar: Bar
    start: datetime
    end: datetime
    as_of: datetime                   # point in time cut for every input (H1)
    capital_usd: Decimal
    costs: CostModel
    fill: FillRules = field(default_factory=FillRules)
    oms: Literal["netting", "hedging"] = "netting"      # hedging only for SWC CFD desks (B-10)
    qty_mode: Literal["integer", "fractional"] = "integer"   # fractional only for LP (B-34)
    on_conflict: Literal["exit_first"] = "exit_first"   # V-10
    finalize: Literal["mark_to_market", "close_at_end"] = "mark_to_market"   # B-15
    session: Literal["ETH", "RTH"] | None = None        # V-25
    desk_id: int | None = None        # when set, the desk's limits and guards apply through guards_bridge (L-48, N-20)
    accumulate: bool = False          # a second entry on the same side adds to the position only when True (V-10)
    seed: int = 0

    def __post_init__(self) -> None:
        validate(self)

    def canonical(self) -> dict[str, Any]:
        """Plain values with Decimal and datetime as text, keys sorted by the hash."""
        def plain(value: Any) -> Any:
            if isinstance(value, Decimal):
                return format(value, "f")
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, Mapping):
                return {str(k): plain(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [plain(v) for v in value]
            if hasattr(value, "__dataclass_fields__"):
                return {k: plain(getattr(value, k)) for k in value.__dataclass_fields__}
            return value
        return plain(self)

    def fingerprint(self) -> str:
        return content_hash(self.canonical())


def validate_bar(bar: str, line: str) -> None:
    """P-14: BarNotAllowed (`bar_not_allowed_for_line`) when the bar size does not belong to the line."""
    if bar not in BARS:
        raise SpecError(f"unknown bar size {bar!r}")
    allowed = ALLOWED_BARS.get(line)
    if allowed is None:
        raise SpecError(f"unknown line {line!r}")
    if bar not in allowed:
        raise BarNotAllowed(f"a {line} backtest runs on {', '.join(allowed)} bars, not {bar}")


def validate(spec: "BacktestSpec") -> None:
    """The checks of a spec that do not need data: bar per line (P-14), hedging (B-10) and fractional size (B-34)."""
    validate_bar(spec.bar, spec.line)
    if spec.oms not in ("netting", "hedging"):
        raise SpecError("oms is netting or hedging")
    if spec.oms == "hedging" and spec.line != "SWC":
        raise SpecError("hedging is only for SWC CFD desks: every other line nets in one account")
    if spec.qty_mode not in ("integer", "fractional"):
        raise SpecError("qty_mode is integer or fractional")
    if spec.qty_mode == "fractional" and spec.line != "LP":
        raise SpecError("fractional quantities are only for LP (stocks and ETF)")
    if spec.finalize not in ("mark_to_market", "close_at_end"):
        raise SpecError("finalize is mark_to_market or close_at_end")
    if spec.on_conflict != "exit_first":
        raise SpecError("on_conflict is exit_first")
    if spec.start >= spec.end:
        raise SpecError("start must be before end")
    if spec.as_of < spec.end:
        raise SpecError("as_of is the point in time cut: it cannot be before the end of the run")
    if spec.costs.slippage_ticks != spec.fill.next_open_ticks:
        raise SpecError("costs.slippage_ticks and fill.next_open_ticks describe the same tick against: they must be equal")
    if not spec.instruments:
        raise SpecError("a backtest needs at least one instrument")


def round_qty(qty: Decimal, sheet: ContractSheet, *, mode: str = "integer", lot: Decimal = Decimal(1)) -> tuple[Decimal, tuple[str, ...]]:
    """V-07: the size rounded down to the lot of the sheet; the remainder that was cut is returned as a warning.

    Futures and options always trade whole contracts. Stocks use the lot (1 by default) in integer mode and keep the
    fraction in fractional mode (LP). A size below one lot becomes 0 and the warning says so.
    """
    if not isinstance(qty, Decimal) or not qty.is_finite() or qty < 0:
        raise SpecError("a size is a Decimal of zero or more")
    if lot <= 0:
        raise SpecError("a lot is above zero")
    if mode == "fractional" and sheet.asset_type in ("equity", "etf"):
        return qty, ()
    step = lot if sheet.asset_type in ("equity", "etf", "cfd", "crypto", "bond", "bill") else Decimal(1)
    rounded = (qty // step) * step
    if rounded == qty:
        return rounded, ()
    return rounded, (f"size_rounded_down:{qty}->{rounded}",)


def sequence_same_bar(exits: Sequence[Any], entries: Sequence[Any], *, holding_side: str | None, accumulate: bool = False,
                      entry_side: Any = lambda intent: intent.side) -> tuple[list[Any], tuple[str, ...]]:
    """V-10: what happens with an exit and an entry on the same bar. Exits go first (on_conflict = exit_first), then entries.

    With accumulation off, an entry on the side the account already holds is ignored and the warning says so. `holding_side`
    is the side held after the exits ("buy", "sell" or None).
    """
    warnings: list[str] = []
    ordered = list(exits)
    held = holding_side if not exits else None
    for intent in entries:
        side = entry_side(intent)
        if not accumulate and held is not None and side == held:
            warnings.append("entry_ignored_no_accumulation")
            continue
        ordered.append(intent)
        held = side
    return ordered, tuple(warnings)
