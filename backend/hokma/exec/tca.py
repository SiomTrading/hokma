"""Transaction cost analysis (SPEC-018: ECT-018). The implementation shortfall of Perold split into delay, impact, spread, opportunity and
commission, with an exact sum; the fill against the arrival price, the VWAP and the close; the square root impact model; a table of brokers;
and the real slippage against what a backtest assumes (next open plus one tick), by symbol, to recalibrate the simulated broker.

All amounts are Decimal and carry the multiplier of the contract sheet. The commission is per contract and the multiplier is never forgotten."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hokma.exec.money import ZERO

BPS = Decimal(10_000)


@dataclass(frozen=True)
class Shortfall:
    delay: Decimal
    impact: Decimal
    spread: Decimal
    opportunity: Decimal
    commission: Decimal

    @property
    def total(self) -> Decimal:
        return self.delay + self.impact + self.spread + self.opportunity + self.commission


def _sign(side: str) -> Decimal:
    if side not in ("buy", "sell"):
        raise ValueError("side is buy or sell")
    return Decimal(1) if side == "buy" else Decimal(-1)


def implementation_shortfall(*, side: str, ordered_qty: Decimal, fills: Sequence[tuple[Decimal, Decimal]], decision_price: Decimal,
                             arrival_price: Decimal, close_price: Decimal, spread: Decimal, multiplier: Decimal, commission: Decimal) -> Shortfall:
    """Paper result of the whole order at the decision price minus the real result, in USD. `fills` are (qty, price). The spread is counted
    once, as half the quoted spread on what was filled, and taken OUT of the impact, so it is not counted twice. What was not filled is the
    opportunity cost against the close."""
    s = _sign(side)
    filled = sum((q for q, _p in fills), ZERO)
    if filled > ordered_qty or filled < 0:
        raise ValueError("filled quantity is between 0 and the ordered quantity")
    avg = sum((q * p for q, p in fills), ZERO) / filled if filled else arrival_price
    delay = s * (arrival_price - decision_price) * filled * multiplier
    spread_cost = spread / 2 * filled * multiplier
    impact = s * (avg - arrival_price) * filled * multiplier - spread_cost
    opportunity = s * (close_price - decision_price) * (ordered_qty - filled) * multiplier
    return Shortfall(delay, impact, spread_cost, opportunity, commission)


def against_benchmarks(*, side: str, avg_fill: Decimal, arrival: Decimal, vwap: Decimal, close: Decimal) -> dict[str, Decimal]:
    """The fill compared with the arrival price, the VWAP and the close, in basis points; positive is worse for the trader."""
    s = _sign(side)
    return {name: s * (avg_fill - ref) / ref * BPS for name, ref in (("arrival", arrival), ("vwap", vwap), ("close", close))}


def sqrt_impact_bps(*, daily_volatility_bps: Decimal, qty: Decimal, daily_volume: Decimal, coefficient: Decimal = Decimal(1)) -> Decimal:
    """Square root model: impact = coefficient * volatility * sqrt(qty / volume)."""
    if daily_volume <= 0 or qty < 0:
        raise ValueError("volume must be above zero and quantity zero or more")
    return coefficient * daily_volatility_bps * (qty / daily_volume).sqrt()


def commission_per_contract(contracts: Decimal, rate_per_contract: Decimal) -> Decimal:
    """Futures and options pay per contract, whatever the multiplier is."""
    return contracts * rate_per_contract


def broker_table(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per broker: the average slippage in basis points and the total commission, worst first."""
    acc: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        acc.setdefault(str(r["broker"]), []).append(r)
    table = [{"broker": b, "fills": len(items), "avg_slippage_bps": sum((Decimal(str(i["slippage_bps"])) for i in items), ZERO) / len(items),
              "commission": sum((Decimal(str(i["commission"])) for i in items), ZERO)} for b, items in acc.items()]
    return sorted(table, key=lambda x: x["avg_slippage_bps"], reverse=True)


def slippage_vs_backtest(fills: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Decimal]]:
    """The real deviation against the backtest assumption (the next open plus one tick against the trader), by symbol. Each fill carries
    side, price, next_open and tick. The result says the average extra cost in ticks the backtest did not charge: the number to recalibrate
    the slippage of the simulated broker with."""
    by: dict[str, list[Decimal]] = {}
    for f in fills:
        s = _sign(f["side"])
        assumed = Decimal(str(f["next_open"])) + s * Decimal(str(f["tick"]))
        by.setdefault(str(f["symbol"]), []).append(s * (Decimal(str(f["price"])) - assumed) / Decimal(str(f["tick"])))
    return {sym: {"fills": Decimal(len(v)), "avg_extra_ticks": sum(v, ZERO) / len(v)} for sym, v in by.items()}


def compare_with_preview(*, side: str, sim_price: Decimal, preview_price: Decimal) -> dict[str, Decimal]:
    """The simulated fill against the recorded preview of a real broker: the difference the report shows (positive is worse in the simulation)."""
    s = _sign(side)
    diff = s * (sim_price - preview_price)
    return {"difference": diff, "bps": diff / preview_price * BPS}
