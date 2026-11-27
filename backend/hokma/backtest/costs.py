"""Costs of a backtest (SPEC-020, section 4.2). Every number comes from the one place that owns it: the commission and the slippage from
`hokma.broker.costs` and the tables of `hokma.broker.fees` (H2, the same ones the simulated broker reads), the square root impact from
`hokma.exec.tca`. This file has no formula of basis points of its own: a basis point amount is always computed by `hokma.broker.costs`
(a test reads this file and fails if one appears). What is new here is only what the backtest alone needs: the share of the bar volume,
the overnight swap of a CFD and the cost of a strategy in units of Sharpe."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from hokma.backtest.fills import Side, against
from hokma.backtest.spec import CostModel, SpecError
from hokma.broker import costs as broker_costs
from hokma.broker import fees
from hokma.exec import tca

__all__ = ["CostModel", "FeeSpec", "RoundTrip", "CostInSharpe", "fee_model", "commission_usd", "round_trip", "volume_share",
           "market_impact", "apply_impact", "cfd_swap", "cost_in_sharpe", "VOLUME_COEFFICIENT_BPS"]

VOLUME_COEFFICIENT_BPS = Decimal(1000)      # the slippage of a whole bar of volume would be 1000 basis points; it grows with the share squared


@dataclass(frozen=True)
class FeeSpec:
    """The commission rule of one asset type of a fee table, ready to charge."""
    table: fees.FeeTable
    asset_type: str

    @property
    def rule(self) -> fees.FeeRule:
        return self.table.rule_of(self.asset_type)

    def engine_rate(self) -> Decimal | None:
        """The rate per contract when the engine can charge it alone (a plain per contract rule, no minimum, no cap); otherwise None
        and Hokma charges every fill with `commission_usd`."""
        rule = self.rule
        return rule.value if rule.kind == "per_contract" and not rule.minimum and rule.cap_bps is None else None


def fee_model(model: CostModel, asset_type: str = "future") -> FeeSpec:
    """The fee rule that `model.fee_table` gives to `asset_type`. A table or asset type that does not exist is a SpecError."""
    try:
        table = fees.load(model.fee_table)
    except Exception as exc:
        raise SpecError(f"fee table {model.fee_table!r} cannot be loaded: {exc}") from exc
    if table.rule_of(asset_type) is None:
        raise SpecError(f"fee table {model.fee_table!r} has no rule for {asset_type!r}")
    return FeeSpec(table, asset_type)


def commission_usd(spec: FeeSpec, *, qty: Decimal, price: Decimal, multiplier: Decimal = Decimal(1)) -> Decimal:
    """The commission of ONE side. A contract pays its rate per contract: an order of 2 contracts at 2.25 pays 4.50."""
    return fees.fee(spec.table, spec.asset_type, qty=qty, price=price, multiplier=multiplier)


@dataclass(frozen=True)
class RoundTrip:
    entry_price: Decimal
    exit_price: Decimal
    gross_usd: Decimal
    commission_usd: Decimal
    net_usd: Decimal


def round_trip(side: Literal["long", "short"], *, entry_ref: Decimal, exit_ref: Decimal, qty: Decimal, tick_size: Decimal,
               multiplier: Decimal, model: CostModel, asset_type: str = "future") -> RoundTrip:
    """A closed trade with the whole cost model: the ticks against on each side (L-14) and the commission of each side. `entry_ref` and
    `exit_ref` are the prices before the ticks, for example the open of the bar of each order."""
    entry_side, exit_side = ("buy", "sell") if side == "long" else ("sell", "buy")
    entry = against(entry_side, entry_ref, model.slippage_ticks, tick_size)
    out = against(exit_side, exit_ref, model.slippage_ticks, tick_size)
    spec = fee_model(model, asset_type)
    fee = commission_usd(spec, qty=qty, price=entry, multiplier=multiplier) + commission_usd(spec, qty=qty, price=out, multiplier=multiplier)
    gross = (out - entry if side == "long" else entry - out) * qty * multiplier
    return RoundTrip(entry, out, gross, fee, gross - fee)


def _moved(side: Side, price: Decimal, bps: Decimal) -> Decimal:
    return Decimal(repr(broker_costs.fill_price(side, float(price), float(bps))))


def volume_share(side: Side, price: Decimal, qty: Decimal, bar_volume: Decimal, *, coefficient_bps: Decimal = VOLUME_COEFFICIENT_BPS) -> Decimal:
    """The price after the slippage of taking a share of the volume of the bar: coefficient times the share squared, against the trader.
    A share of 10 percent with the default coefficient is 1000 * 0.1^2 = 10 basis points. An order bigger than the bar is not possible."""
    if bar_volume <= 0:
        raise SpecError("volume_missing: the volume of the bar must be above zero")
    share = qty / bar_volume
    if share > 1:
        raise SpecError("order_exceeds_bar_volume: the order is bigger than the volume of the bar")
    return _moved(side, price, coefficient_bps * share * share)


def market_impact(side: Side, price: Decimal, qty: Decimal, daily_volume: Decimal, daily_volatility_bps: Decimal,
                  coefficient: Decimal = Decimal(1)) -> Decimal:
    """The price after the square root impact, from the same model as the transaction cost analysis of the execution service
    (`hokma.exec.tca.sqrt_impact_bps`). Against the trader."""
    return _moved(side, price, tca.sqrt_impact_bps(daily_volatility_bps=daily_volatility_bps, qty=qty, daily_volume=daily_volume,
                                                    coefficient=coefficient))


def apply_impact(model: CostModel, side: Side, price: Decimal, qty: Decimal, *, bar_volume: Decimal | None = None,
                 daily_volume: Decimal | None = None, daily_volatility_bps: Decimal | None = None) -> Decimal:
    """`price` after the impact that `model.impact` names; "none" returns it unchanged. A missing input is a SpecError, never a default."""
    if model.impact == "none":
        return price
    if model.impact == "volume_share":
        if bar_volume is None:
            raise SpecError("volume_missing: the volume_share impact needs the volume of the bar")
        return volume_share(side, price, qty, bar_volume)
    if daily_volume is None or daily_volatility_bps is None:
        raise SpecError("impact_inputs_missing: the sqrt impact needs the daily volume and the daily volatility")
    return market_impact(side, price, qty, daily_volume, daily_volatility_bps)


def cfd_swap(side: Literal["long", "short"], notional: Decimal, nights: Sequence[date], rates: fees.SwapRates) -> Decimal:
    """The financing a CFD pays for the nights it is held: the daily rate of the broker on the notional, once per night, and three times
    on the weekday that the table says pays the weekend. A positive amount is a cost. No rates, no swap: that is a SpecError."""
    if rates is None:
        raise SpecError("swap_rates_missing: the broker table has no CFD swap rates")
    daily = rates.long_daily if side == "long" else rates.short_daily
    total = Decimal(0)
    for night in nights:
        total += notional * daily * (3 if rates.triple_weekday is not None and night.weekday() == rates.triple_weekday else 1)
    return total


@dataclass(frozen=True)
class CostInSharpe:
    cost_sharpe: float              # the annual cost, in units of Sharpe
    net_sharpe: float | None        # gross minus cost, when the gross Sharpe is given
    warnings: tuple[str, ...]


def cost_in_sharpe(*, round_trips_per_year: float, cost_per_round_trip: float, annual_volatility: float,
                   gross_sharpe: float | None = None) -> CostInSharpe:
    """Does the strategy survive its costs? The yearly cost is round trips times the cost of one, as a fraction of capital; divided by
    the yearly volatility it is the Sharpe the costs take away. `costs_kill_edge` is raised when the Sharpe left is below zero."""
    if annual_volatility <= 0 or round_trips_per_year < 0 or cost_per_round_trip < 0:
        raise SpecError("bad_spec: volatility above zero, turnover and cost zero or more")
    cost = round_trips_per_year * cost_per_round_trip / annual_volatility
    net = None if gross_sharpe is None else gross_sharpe - cost
    return CostInSharpe(cost, net, ("costs_kill_edge",) if net is not None and net < 0 else ())
