"""Calibration of the premium selling threshold (SPEC-020, section 4.9, decision 1260).

`calibrate` opens the switch of SPEC-019 for one desk and one underlying, and only with the evidence of a backtest. It reads a recorded history
of the option chain, measures the metric that Caleb reads on every day, tries a grid of thresholds on every structure that sells premium and
validates the grid with a walk forward. When every number of the gate passes it writes a calibrated row with `record_calibration`; when one does not
it writes no row and names each reason. A recalibration that fails calls `retire_calibration` and the switch closes. There is no other way to
open it: no argument, no tool and no text of an agent can.

What the backtest does, in the order of the spec:

- A signal on the snapshot of day i (the metric passes the threshold) fills on the snapshot of day i + 1 and not on the one that gave the signal:
  the same rule of the next open of the rest of the service. A leg that is sold fills at the bid and a leg that is bought at the ask, always.
- The commission is the one of the fee table of the gate (with its cap when the table has one), per leg, on both sides of the trade.
- The margin of a structure and the early assignment come from the rules of the options module (`OptionRules`), never from this file.
- The exits are the ones of the gate: a share of the credit kept, a multiple of the credit lost and a number of days to expiry. A decision on the
  snapshot of day j is carried out on the snapshot of day j + 1, again at the bad side of the quotes.
- One position at a time per structure and threshold, sized so that its margin uses `margin_use_pct` of the capital of the desk.

The statistics come from `hokma.quant`: Yang-Zhang volatility from `stats`, the bootstrap, the deflated Sharpe ratio and the probability of
overfitting from `validation`, the drawdown from `metrics`, and the walk forward from `hokma.backtest.walkforward`.

The recorded history of the chain is an input that the code cannot make: `default_source` asks `hokma.data.option_chain_history` (the provider of
task 3304 or one that the student connects). Without it the status is `data_missing`, nothing is written and the switch stays closed.
"""
from __future__ import annotations

import bisect
import dataclasses
import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from functools import cached_property
from typing import Any, Literal, Protocol

import numpy as np
import pandas as pd

from hokma.backtest import costs as backtest_costs
from hokma.backtest import walkforward
from hokma.backtest.calibration import gate as gate_mod
from hokma.backtest.calibration.gate import PremiumGate
from hokma.backtest.spec import BacktestSpec, CostModel, FillRules
from hokma.db import Database, get_database
from hokma.exec.guards.premium_selling import CalibratedRow, calibrated_rows, record_calibration, retire_calibration
from hokma.exec.guards.premium_selling import _clean_underlying
from hokma.quant import limits, metrics
from hokma.quant.errors import BadInput
from hokma.quant.stats import volatility as stats_volatility
from hokma.quant import validation
from hokma.quant.validation import bootstrap
from hokma.quant.validation.evidence import EvidenceBundle, bundle

Status = Literal["calibrated", "not_passed", "data_missing", "unavailable"]
STRATEGY_ID = "premium_calibration"
MULTIPLIER = Decimal(100)
PERCENTILES: tuple[int, ...] = (10, 20, 30, 40, 50, 60, 70, 80, 90)
RV_WEEKS = 4                         # the realized volatility of a day is the root mean square of the last four complete weeks of Yang-Zhang
WEEKS_PER_YEAR = 52
RANK_WINDOW = 252                    # snapshots of the implied volatility that its rank looks back over
MIN_RANK_POINTS = 60
MIN_METRIC_POINTS = 60
BOOTSTRAP_SAMPLES = 1000
CSCV_BLOCKS = 10                     # the blocks of the combinatorial symmetric cross validation of the probability of overfitting (252 splits)
STALE_DAYS = 65                      # a calibration that two monthly runs could not renew (no history) is retired
JOB_NAME = "Premium threshold recalibration"
JOB_SCHEDULE = ("cron", "0 7 1 * *")


class CalibrationError(ValueError):
    """The calibration cannot be asked this way. `code` is stable: bad_calibration."""

    code = "bad_calibration"


class RulesUnavailable(CalibrationError):
    """The margin and early assignment rules of the options module are not here, so a backtest of selling premium cannot be priced."""

    code = "options_rules_unavailable"


# ---- the recorded chain ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OptionQuote:
    """One quote of the chain on one day. `iv` is the implied volatility that the source gives (a decimal, 0.20 is 20 percent) or None."""

    expiration: date
    kind: Literal["call", "put"]
    strike: Decimal
    bid: Decimal
    ask: Decimal
    iv: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("call", "put"):
            raise CalibrationError("a quote is a call or a put")
        if not (isinstance(self.strike, Decimal) and isinstance(self.bid, Decimal) and isinstance(self.ask, Decimal)):
            raise CalibrationError("strike, bid and ask are Decimal")


@dataclass(frozen=True)
class ChainDay:
    """The chain of one trading day: the spot and every quote."""

    day: date
    spot: Decimal
    quotes: tuple[OptionQuote, ...]

    @cached_property
    def index(self) -> dict[tuple[date, str, Decimal], OptionQuote]:
        return {(q.expiration, q.kind, q.strike): q for q in self.quotes}

    @cached_property
    def expirations(self) -> tuple[date, ...]:
        return tuple(sorted({q.expiration for q in self.quotes}))

    def strikes(self, expiration: date, kind: str) -> list[Decimal]:
        return sorted(k for (e, t, k) in self.index if e == expiration and t == kind)

    def quote(self, expiration: date, kind: str, strike: Decimal) -> OptionQuote | None:
        return self.index.get((expiration, kind, strike))


@dataclass(frozen=True, eq=False)
class ChainHistory:
    """A recorded history of the chain of one underlying: one `ChainDay` per trading day and the daily bars of the underlying (open, high, low,
    close on a datetime index) that the realized volatility needs. Days are in order and never repeat."""

    underlying: str
    days: tuple[ChainDay, ...]
    bars: pd.DataFrame
    rate: float = 0.0                                              # the annual risk free rate of the model of early assignment of puts
    dividends: tuple[tuple[date, float], ...] = ()                 # ex dividend dates and amounts per share that the model of early assignment of calls reads

    def __post_init__(self) -> None:
        stamps = [d.day for d in self.days]
        if any(b <= a for a, b in zip(stamps, stamps[1:])):
            raise CalibrationError("the days of a chain history are in order and never repeat")
        if any(d.spot <= 0 for d in self.days):
            raise CalibrationError("every day of a chain history has a spot above zero")
        if not isinstance(self.bars, pd.DataFrame) or not isinstance(self.bars.index, pd.DatetimeIndex) or not {"open", "high", "low", "close"} <= set(self.bars.columns):
            raise CalibrationError("the bars of a chain history are a frame with open, high, low and close on a datetime index")

    def until(self, last: date) -> "ChainHistory":
        """The history cut at a day: nothing after it is read (point in time)."""
        keep = tuple(d for d in self.days if d.day <= last)
        bars = self.bars[self.bars.index.normalize() <= pd.Timestamp(last)]
        return ChainHistory(self.underlying, keep, bars, self.rate, self.dividends)

    @cached_property
    def data_hash(self) -> str:
        """A fingerprint of everything the calibration reads: the quotes in their order, the spots, the bars, the rate and the dividends."""
        digest = hashlib.sha256()
        digest.update(f"{self.underlying}|{self.rate!r}|{[(d.isoformat(), a) for d, a in self.dividends]}".encode())
        for d in self.days:
            digest.update(f"\n{d.day.isoformat()}|{d.spot}|".encode())
            digest.update(";".join(f"{q.expiration.isoformat()},{q.kind},{q.strike},{q.bid},{q.ask},{q.iv!r}" for q in d.quotes).encode())
        digest.update(f"\n{self.bars[['open', 'high', 'low', 'close']].to_csv()}".encode())
        return digest.hexdigest()


ChainSource = Callable[[str, datetime], "ChainHistory | None"]


def default_source(underlying: str, as_of: datetime) -> ChainHistory | None:
    """The history of the chain from `hokma.data.option_chain_history` when a provider has registered it there; None when nobody has (the status
    of the calibration is then `data_missing`). It is never filled with anything."""
    import hokma.data as data

    provider = getattr(data, "option_chain_history", None)
    return None if provider is None else provider(underlying, as_of=as_of)


# ---- the rules of the options module -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class LegSpec:
    kind: Literal["call", "put"]
    side: Literal["short", "long"]
    strike: Decimal
    expiration: date


class OptionRules(Protocol):
    """What a backtest of selling premium takes from the options module: the margin of a structure (one set of legs, one contract each) and the
    short legs that are marked for early assignment. `prices` are the fill prices of the legs per share, in the order of `legs`, and `spot` the price
    of the underlying that day. The margin is in USD for one set."""

    name: str

    def margin_usd(self, structure: str, legs: Sequence[LegSpec], *, prices: Sequence[Decimal], spot: Decimal) -> Decimal: ...

    def assigned_legs(self, legs: Sequence[LegSpec], *, marks: Sequence[Decimal], spot: Decimal, on: date, rate: float,
                      dividends: Sequence[tuple[date, float]]) -> frozenset[int]: ...


class ModuleRules:
    """The margin (`options.strategy_margin`, L-21) and the early assignment marks (`options.assignment`, L-56) of the options module, called as they
    are: this class only hands the legs over in their shape and returns what the module says."""

    name = "quant.options.margin+assignment"

    def __init__(self) -> None:
        try:
            from hokma.quant.options import assignment, margin
        except ImportError as exc:
            raise RulesUnavailable(f"options_rules_unavailable: {exc}") from exc
        self._margin, self._assignment = margin, assignment

    def margin_usd(self, structure: str, legs: Sequence[LegSpec], *, prices: Sequence[Decimal], spot: Decimal) -> Decimal:
        rows = [{"instrument": "option", "position": leg.side, "kind": leg.kind, "strike": float(leg.strike), "premium": float(price), "quantity": 1,
                 "multiplier": float(MULTIPLIER)} for leg, price in zip(legs, prices)]
        found = self._margin.strategy_margin(rows, spot=float(spot), cash_secured=structure == "cash_secured_put")
        if found.value is None:
            raise CalibrationError(f"the margin of {structure} cannot be computed: {'; '.join(found.warnings)[:160]}")
        return Decimal(str(found.value["margin_usd"]))

    def assigned_legs(self, legs: Sequence[LegSpec], *, marks: Sequence[Decimal], spot: Decimal, on: date, rate: float,
                      dividends: Sequence[tuple[date, float]]) -> frozenset[int]:
        rows = [{"instrument": "option", "position": leg.side, "kind": leg.kind, "strike": float(leg.strike), "quantity": 1,
                 "expiration": leg.expiration.isoformat(), "price": float(mark)} for leg, mark in zip(legs, marks)]
        found = self._assignment.assess(rows, float(spot), on.isoformat(), rate, [{"ex_date": d.isoformat(), "amount": a} for d, a in dividends])
        return frozenset(int(m["leg"]) for m in found["legs"] if m["at_risk"])


def default_rules() -> OptionRules:
    """The rules of the options module. Without them there is nothing to price a short structure with: RulesUnavailable, and the calibration answers
    `unavailable` and writes nothing."""
    return ModuleRules()


# ---- the metric of Caleb on every day -----------------------------------------------------------------------------------

def _pick_expiry(day: ChainDay, dte: tuple[int, int]) -> date | None:
    """The expiration whose days to expiry are closest to the middle of the window; the earlier one on a tie. None when none is inside."""
    middle = (dte[0] + dte[1]) / 2
    inside = [e for e in day.expirations if dte[0] <= (e - day.day).days <= dte[1]]
    return min(inside, key=lambda e: (abs((e - day.day).days - middle), e)) if inside else None


def atm_iv(day: ChainDay, dte: tuple[int, int]) -> float | None:
    """The implied volatility at the money of the expiration picked in the window: the mean of the call and the put of the strike nearest to the spot."""
    expiry = _pick_expiry(day, dte)
    if expiry is None:
        return None
    both = [k for k in day.strikes(expiry, "call") if (p := day.quote(expiry, "put", k)) is not None and p.iv is not None
            and (c := day.quote(expiry, "call", k)) is not None and c.iv is not None]
    if not both:
        return None
    strike = min(both, key=lambda k: (abs(k - day.spot), k))
    call, put = day.quote(expiry, "call", strike), day.quote(expiry, "put", strike)
    assert call is not None and put is not None and call.iv is not None and put.iv is not None
    values = (call.iv, put.iv)
    return float(sum(values) / 2) if all(math.isfinite(v) and v > 0 for v in values) else None


def realized_volatility(history: ChainHistory, weeks: int = RV_WEEKS) -> np.ndarray:
    """Annualized realized volatility known at each day: the root mean square of the last `weeks` complete weeks of the Yang-Zhang estimator of
    `quant.stats`, in the days of the chain. A week that has not ended before the day is never used. NaN where there are not enough weeks."""
    out = np.full(len(history.days), np.nan)
    bars = history.bars
    result = stats_volatility.yang_zhang(bars["open"], bars["high"], bars["low"], bars["close"], "weekly")
    if result.value is None:
        return out
    ends = [date.fromisoformat(label.split("/")[1]) for label in result.value["index"]]
    values = np.asarray(result.value["values"], dtype=float)
    for i, day in enumerate(history.days):
        last = bisect.bisect_left(ends, day.day) - 1               # the last week that ended before this day
        if last >= weeks - 1:
            out[i] = math.sqrt(float(np.mean(values[last - weeks + 1:last + 1] ** 2))) * math.sqrt(WEEKS_PER_YEAR)
    return out


def metric_series(history: ChainHistory, gate: PremiumGate) -> np.ndarray:
    """The metric of the gate on every day of the history (NaN where it cannot be read). iv_rank looks back at the days before and including the day."""
    iv = np.array([v if (v := atm_iv(d, gate.dte)) is not None else np.nan for d in history.days])
    if gate.metric == "iv_rank":
        out = np.full(len(iv), np.nan)
        for i in range(len(iv)):
            window = iv[max(0, i - RANK_WINDOW + 1):i + 1]
            window = window[np.isfinite(window)]
            if len(window) >= MIN_RANK_POINTS and np.isfinite(iv[i]) and window.max() > window.min():
                out[i] = (iv[i] - window.min()) / (window.max() - window.min()) * 100.0
        return out
    rv = realized_volatility(history)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = iv / rv if gate.metric == "iv_rv_ratio" else iv - rv
    out[~np.isfinite(out)] = np.nan
    return out


def threshold_grid(metric: np.ndarray) -> list[float]:
    """The thresholds to try: the percentiles 10 to 90 of the metric, rounded and without repeats. The grid reads the metric alone, never a result."""
    values = metric[np.isfinite(metric)]
    grid = sorted({round(float(np.quantile(values, p / 100)), 6) for p in PERCENTILES})
    return grid


# ---- the backtest of one structure from one day --------------------------------------------------------------------------

@dataclass(frozen=True)
class Trade:
    """A position from its fill to its exit. `path` is its result in USD, cumulative, one value per day from the fill to the exit (or to the last
    day of the history when it has not closed: `finished` is False and the trade counts in no statistic)."""

    structure: str
    signal_idx: int
    fill_idx: int
    exit_idx: int
    net_pnl: float
    margin_usd: float
    contracts: int
    finished: bool
    exit_reason: str
    path: tuple[float, ...]


def _near(strikes: Sequence[Decimal], target: float) -> Decimal | None:
    return min(strikes, key=lambda k: (abs(float(k) - target), k)) if strikes else None


def legs_for(structure: str, day: ChainDay, gate: PremiumGate) -> list[LegSpec] | None:
    """The legs of a structure on a day: the expiration nearest to the middle of the window, the short strikes `short_otm_pct` away from the spot
    and the wings `wing_pct` beyond them, each on the nearest listed strike. None when the chain cannot make it."""
    expiry = _pick_expiry(day, gate.dte)
    if expiry is None:
        return None
    spot, otm, wing = float(day.spot), gate.legs["short_otm_pct"] / 100, gate.legs["wing_pct"] / 100
    puts, calls = day.strikes(expiry, "put"), day.strikes(expiry, "call")
    short_put, short_call = _near(puts, spot * (1 - otm)), _near(calls, spot * (1 + otm))
    straddle = _near([k for k in calls if k in set(puts)], spot)
    long_put = None if short_put is None else _near([k for k in puts if k < short_put], float(short_put) * (1 - wing))
    long_call = None if short_call is None else _near([k for k in calls if k > short_call], float(short_call) * (1 + wing))
    parts: dict[str, list[tuple[str, str, Decimal | None]]] = {
        "short_put": [("put", "short", short_put)], "cash_secured_put": [("put", "short", short_put)], "short_call": [("call", "short", short_call)],
        "credit_put_spread": [("put", "short", short_put), ("put", "long", long_put)],
        "credit_call_spread": [("call", "short", short_call), ("call", "long", long_call)],
        "iron_condor": [("put", "short", short_put), ("put", "long", long_put), ("call", "short", short_call), ("call", "long", long_call)],
        "short_straddle": [("call", "short", straddle), ("put", "short", straddle)],
        "short_strangle": [("put", "short", short_put), ("call", "short", short_call)]}
    chosen = parts.get(structure)
    if chosen is None or any(strike is None for _k, _s, strike in chosen):
        return None
    return [LegSpec(kind, side, strike, expiry) for kind, side, strike in chosen if strike is not None]       # type: ignore[arg-type]


def _intrinsic(leg: LegSpec, spot: Decimal) -> Decimal:
    return max(spot - leg.strike, Decimal(0)) if leg.kind == "call" else max(leg.strike - spot, Decimal(0))


def _mid(q: OptionQuote) -> Decimal:
    return (q.bid + q.ask) / 2


class Simulation:
    """The trades of every structure from every day of a history. A candidate is made once and shared by every threshold that asks for it."""

    def __init__(self, history: ChainHistory, gate: PremiumGate, rules: OptionRules, capital: Decimal) -> None:
        self.days, self.gate, self.rules, self.capital = history.days, gate, rules, capital
        self.rate, self.dividends = history.rate, history.dividends
        self.fee = backtest_costs.fee_model(CostModel(gate.fee_table), "option")
        self._memo: dict[tuple[str, int], Trade | None] = {}

    def commission(self, qty: int, price: Decimal) -> Decimal:
        return backtest_costs.commission_usd(self.fee, qty=Decimal(qty), price=price, multiplier=MULTIPLIER)

    @staticmethod
    def _can_sell(q: OptionQuote | None) -> bool:
        return q is not None and q.ask >= q.bid > 0

    @staticmethod
    def _can_buy(q: OptionQuote | None) -> bool:
        return q is not None and q.ask >= q.bid >= 0 and q.ask > 0

    def candidate(self, structure: str, i: int) -> Trade | None:
        """The trade that a signal on day `i` makes, or None (no legs, a leg that cannot be filled the next day, no credit, or a margin that does not fit)."""
        key = (structure, i)
        if key not in self._memo:
            self._memo[key] = self._make(structure, i)
        return self._memo[key]

    def _make(self, structure: str, i: int) -> Trade | None:
        if i + 1 >= len(self.days):
            return None
        legs = legs_for(structure, self.days[i], self.gate)
        fill_day = self.days[i + 1]
        if legs is None:
            return None
        quotes = [fill_day.quote(leg.expiration, leg.kind, leg.strike) for leg in legs]
        if not all(self._can_sell(q) if leg.side == "short" else self._can_buy(q) for leg, q in zip(legs, quotes)):
            return None
        prices = [q.bid if leg.side == "short" else q.ask for leg, q in zip(legs, quotes) if q is not None]
        credit = sum((p if leg.side == "short" else -p for leg, p in zip(legs, prices)), Decimal(0))
        if credit <= 0:
            return None
        one_set = self.rules.margin_usd(structure, legs, prices=prices, spot=fill_day.spot)
        if one_set <= 0:
            return None
        contracts = int(self.capital * Decimal(str(self.gate.margin_use_pct)) / 100 / one_set)
        if contracts < 1:
            return None
        n = Decimal(contracts)
        cash_in = credit * MULTIPLIER * n - sum((self.commission(contracts, p) for p in prices), Decimal(0))
        return self._run(structure, i, legs, credit, cash_in, contracts, float(one_set * n))

    def _marks(self, legs: Sequence[LegSpec], day: ChainDay, last: list[Decimal]) -> list[Decimal]:
        out = []
        for k, leg in enumerate(legs):
            q = day.quote(leg.expiration, leg.kind, leg.strike)
            last[k] = _mid(q) if q is not None and q.ask >= q.bid else last[k]
            out.append(last[k])
        return out

    def _run(self, structure: str, signal: int, legs: list[LegSpec], credit: Decimal, cash_in: Decimal, contracts: int, margin_usd: float) -> Trade:
        gate, n, expiration = self.gate, Decimal(contracts), legs[0].expiration
        exits = gate.exits
        fill = signal + 1
        mids = [Decimal(0)] * len(legs)
        for k, leg in enumerate(legs):
            q = self.days[fill].quote(leg.expiration, leg.kind, leg.strike)
            mids[k] = _mid(q) if q is not None else Decimal(0)
        path: list[float] = []
        pending: str | None = None
        assigned: frozenset[int] = frozenset()
        for d in range(fill, len(self.days)):
            day = self.days[d]
            marks = self._marks(legs, day, mids)
            short_cost = sum((m for leg, m in zip(legs, marks) if leg.side == "short"), Decimal(0))
            long_value = sum((m for leg, m in zip(legs, marks) if leg.side == "long"), Decimal(0))
            close_mid = short_cost - long_value
            if d > fill:
                settled = self._exit(legs, day, pending, assigned, expiration, contracts)
                if settled is not None:
                    cost, reason = settled
                    path.append(float(cash_in - cost))
                    return Trade(structure, signal, fill, d, path[-1], margin_usd, contracts, True, reason, tuple(path))
            path.append(float(cash_in - close_mid * MULTIPLIER * n))
            if d > fill and pending is None and day.day < expiration:
                dte = (expiration - day.day).days
                kept, lost = credit - close_mid, close_mid - credit
                assigned = self.rules.assigned_legs(legs, marks=marks, spot=day.spot, on=day.day, rate=self.rate, dividends=self.dividends)
                if assigned:
                    pending = "early_assignment"                       # marked today, assigned at the next open
                elif kept >= credit * Decimal(str(exits["profit_take_pct"])) / 100:
                    pending = "profit_take"
                elif lost >= credit * Decimal(str(exits["stop_loss_multiple"])):
                    pending = "stop_loss"
                elif dte <= exits["close_at_dte"]:
                    pending = "close_at_dte"
        return Trade(structure, signal, fill, len(self.days) - 1, path[-1], margin_usd, contracts, False, "history_ended", tuple(path))

    def _exit(self, legs: Sequence[LegSpec], day: ChainDay, pending: str | None, assigned: frozenset[int], expiration: date,
              contracts: int) -> tuple[Decimal, str] | None:
        """What it costs to leave on this day (USD, commissions included) and why, or None when the position stays. Expiry settles every leg at the
        intrinsic value of the spot. A decision of an earlier day closes at the bad side of the quotes of this day, except the legs marked for
        early assignment, which are exercised at their intrinsic value."""
        n = Decimal(contracts)
        if day.day >= expiration:
            return self._settle(legs, day.spot, n, set(range(len(legs)))), "expiry"
        if pending is None:
            return None
        quotes = [day.quote(leg.expiration, leg.kind, leg.strike) for leg in legs]
        if not all(k in assigned or (q is not None and q.ask >= q.bid >= 0 and (leg.side == "long" or q.ask > 0)) for k, (leg, q) in enumerate(zip(legs, quotes))):
            return None
        return self._settle(legs, day.spot, n, set(assigned), quotes_day=day), pending

    def _settle(self, legs: Sequence[LegSpec], spot: Decimal, n: Decimal, intrinsic_legs: set[int], quotes_day: ChainDay | None = None) -> Decimal:
        """The cost of closing: the legs in `intrinsic_legs` at their intrinsic value (an exercise fee for the ones that are assigned early), the
        others at the bad side of the quotes of `quotes_day` (shorts at the ask, longs at the bid). Without `quotes_day` every leg is at intrinsic."""
        cost = Decimal(0)
        for k, leg in enumerate(legs):
            q = None if quotes_day is None else quotes_day.quote(leg.expiration, leg.kind, leg.strike)
            at_intrinsic = k in intrinsic_legs or q is None
            price = _intrinsic(leg, spot) if at_intrinsic else (q.ask if leg.side == "short" else q.bid)       # type: ignore[union-attr]
            cost += (price if leg.side == "short" else -price) * MULTIPLIER * n
            if quotes_day is not None:                                 # a leg that trades, or is exercised early, pays its commission; expiry pays none
                cost += self.commission(int(n), price)
        return cost

    def column(self, structure: str, threshold: float, metric: np.ndarray) -> tuple[np.ndarray, list[Trade]]:
        """The daily result and the trades of one structure with one threshold: it enters when the metric is at or above the threshold and the
        structure is flat, and leaves by the exits of the gate."""
        pnl = np.zeros(len(self.days))
        trades: list[Trade] = []
        blocked = 0
        for i in range(len(self.days) - 1):
            if i < blocked or not np.isfinite(metric[i]) or metric[i] < threshold:
                continue
            trade = self.candidate(structure, i)
            if trade is None:
                continue
            previous = 0.0
            for offset, value in enumerate(trade.path):
                pnl[trade.fill_idx + offset] += value - previous
                previous = value
            trades.append(trade)
            blocked = trade.exit_idx if trade.finished else len(self.days)
        return pnl, trades


# ---- the evidence ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CalibrationResult:
    """What a calibration found. `status` is calibrated (a row was written), not_passed (every reason is listed and nothing was written),
    data_missing (no usable history of the chain, nothing was written) or unavailable (the rules of the options module are not here)."""

    status: Status
    desk_id: int
    underlying: str
    metric: str
    threshold: Decimal | None = None
    reasons: tuple[str, ...] = ()
    measures: Mapping[str, Any] = field(default_factory=dict)
    run_id: int | None = None
    backtest_run_ref: str | None = None
    threshold_id: int | None = None
    retired_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "desk_id": self.desk_id, "underlying": self.underlying, "metric": self.metric,
                "threshold": None if self.threshold is None else str(self.threshold), "reasons": list(self.reasons), "measures": _clean(self.measures),
                "run_id": self.run_id, "backtest_run_ref": self.backtest_run_ref, "threshold_id": self.threshold_id, "retired_id": self.retired_id}


def _clean(value: Any) -> Any:
    """Plain JSON values: no NaN, no numpy types, no Decimal, no date."""
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (Decimal, date, datetime)):
        return str(value)
    return value


def expectancy_per_margin_dollar(trades: Sequence[Trade], *, seed: int = 0) -> dict[str, Any]:
    """The mean of net result over margin of the closed trades, with the 95 percent interval of a stationary bootstrap of the mean."""
    ratios = np.array([t.net_pnl / t.margin_usd for t in trades if t.finished], dtype=float)
    out: dict[str, Any] = {"n": int(len(ratios)), "mean": None, "ci_low": None, "ci_high": None}
    if len(ratios) == 0:
        return out
    out["mean"] = float(ratios.mean())
    if len(ratios) < 2 or float(ratios.std()) == 0.0:
        out["ci_low"] = out["ci_high"] = out["mean"]
        return out
    limits.check_param("n_samples", BOOTSTRAP_SAMPLES)
    limits.check_product("n_samples x length", BOOTSTRAP_SAMPLES, len(ratios))
    block = max(1.0, bootstrap.block_size_of(ratios)) if len(ratios) >= 20 else 1.0
    means = bootstrap.stationary_bootstrap(ratios.reshape(-1, 1), BOOTSTRAP_SAMPLES, float(block), int(seed))[:, :, 0].mean(axis=1)
    out["ci_low"], out["ci_high"] = float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))
    return out


def traded_pbo(matrix: pd.DataFrame, n_blocks: int = CSCV_BLOCKS) -> tuple[float | None, int]:
    """The probability of backtest overfitting (CSCV, `quant.validation`) over the columns of the matrix that earned or lost something in every block.
    A column with no trade in a block has no performance there, and the CSCV of the library is undefined with it: those columns are left out of this
    figure only (they stay in the count of tries of the deflated Sharpe ratio). Returns the probability (None when fewer than two columns are left)
    and the number of columns that were used."""
    size = len(matrix) // n_blocks
    if size < 2:
        return None, 0
    body = matrix.iloc[:size * n_blocks]
    keep = [c for c in body.columns if all(float(body[c].iloc[b * size:(b + 1) * size].std()) > 0 for b in range(n_blocks))]
    if len(keep) < 2:
        return None, len(keep)
    return validation.pbo(body[keep], n_blocks=n_blocks).value, len(keep)


def worst_drawdown_pct(pnl: Sequence[float], rows: Sequence[int], days: Sequence[ChainDay], capital: Decimal) -> float | None:
    """The worst drawdown, in percent of the equity, of the out of sample results laid one after another (`quant.metrics`)."""
    values = np.asarray(pnl, dtype=float)
    if len(values) < 2:
        return None
    equity = float(capital) + np.cumsum(values)
    before = np.concatenate([[float(capital)], equity[:-1]])
    if (before <= 0).any():
        return 100.0
    series = pd.Series(values / before, index=pd.DatetimeIndex([pd.Timestamp(days[r].day) for r in rows]))
    worst = metrics.compute(series, ["max_drawdown"])["max_drawdown"].value
    return None if worst is None else float(-worst * 100)


def _desk_context(db: Database, desk_id: int) -> tuple[Decimal, float | None]:
    """The capital of the desk and its limit of drawdown: the one of `desk_limits`, else the one of its mandate, else None."""
    with db.read() as u:
        desk = u.session.one("SELECT capital FROM desks WHERE id = :d", {"d": desk_id})
        if desk is None:
            raise CalibrationError("there is no such desk")
        limits_row = u.session.one("SELECT config FROM desk_limits WHERE desk_id = :d", {"d": desk_id})
        mandate = u.session.one("SELECT spec_json FROM desk_mandates WHERE desk_id = :d", {"d": desk_id})

    def number(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else None

    limit = number(json.loads(limits_row["config"]).get("max_drawdown_pct")) if limits_row is not None else None
    if limit is None and mandate is not None:
        limit = number((json.loads(mandate["spec_json"]).get("risk") or {}).get("max_drawdown_pct"))
    return Decimal(int(desk["capital"])), limit


def _passes(gate: PremiumGate, *, oos: Sequence[Trade], expectancy: Mapping[str, Any], evidence: EvidenceBundle, efficiency: float | None,
            drawdown: float | None, limit: float | None) -> list[str]:
    """Every reason that the gate is not met, in a fixed order. An empty list is a pass."""
    reasons: list[str] = []
    if len(oos) < gate.min_oos_trades:
        reasons.append(f"too_few_oos_trades:{len(oos)}<{gate.min_oos_trades}")
    if expectancy["mean"] is None or expectancy["mean"] <= 0:
        reasons.append(f"expectancy_not_positive:mean={expectancy['mean']}")
    if evidence.leakage_free is not True or evidence.n_observations <= 0 or evidence.best_strategy is None:
        reasons.append("evidence_insufficient:" + ";".join(evidence.warnings)[:160])
    else:
        if evidence.dsr is None or evidence.dsr < gate.min_dsr:
            reasons.append(f"dsr_below_min:{evidence.dsr}<{gate.min_dsr}")
        if evidence.pbo is None or evidence.pbo > gate.max_pbo:
            reasons.append(f"pbo_above_max:{evidence.pbo}>{gate.max_pbo}")
    if efficiency is None or efficiency < gate.min_wf_efficiency:
        reasons.append(f"wf_efficiency_below_min:{efficiency}<{gate.min_wf_efficiency}")
    if limit is None:
        reasons.append("drawdown_limit_missing")
    elif drawdown is None or drawdown > limit:
        reasons.append(f"drawdown_above_limit:{drawdown}>{limit}")
    return reasons


def _save_run(db: Database, desk_id: int, underlying: str, history: ChainHistory, gate: PremiumGate, capital: Decimal, as_of: datetime, seed: int,
              body: Mapping[str, Any]) -> int:
    """The row of `backtest_runs` (kind calibration) that names the evidence. It is saved whether the gate passed or not."""
    from hokma.backtest import runner

    first, last = history.days[0].day, history.days[-1].day
    end = datetime.combine(last, time(23, 59), tzinfo=timezone.utc)
    spec = BacktestSpec(STRATEGY_ID, 1, {"underlying": underlying, "metric": gate.metric, "structures": ",".join(gate.structures)}, "SWO", (f"EQ:{underlying}",), "1d",
                        datetime.combine(first, time.min, tzinfo=timezone.utc), end, max(as_of, end), capital, CostModel(gate.fee_table), FillRules(), desk_id=desk_id,
                        seed=seed)
    return runner.save_row(db, "calibration", spec, _clean(body), data_hash=history.data_hash)


def calibrate(desk_id: int, underlying: str, *, as_of: datetime, gate: PremiumGate | None = None, source: ChainSource | None = None,
              rules: OptionRules | None = None, db: Database | None = None, owner_loosened: bool = False, seed: int = 0) -> CalibrationResult:
    """Calibrate the threshold of a desk and an underlying (spec 4.9, steps 1 to 6).

    `as_of` is the cut in time: no day after it is read. A `gate` looser than the defaults is refused (GateLoosened) unless the owner asked for
    it (`owner_loosened`, which no tool sets). The result is decided here, from the evidence: nothing the caller passes opens the switch."""
    if isinstance(desk_id, bool) or not isinstance(desk_id, int) or desk_id < 1:
        raise CalibrationError("desk_id must be a desk id")
    try:
        name = _clean_underlying(underlying)
    except ValueError as exc:
        raise CalibrationError(str(exc)) from None
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise CalibrationError("as_of is a datetime with its zone")
    base = gate_mod.load_default()
    gate = base if gate is None else (gate if owner_loosened else gate_mod.require_not_looser(gate, base))
    database = db or get_database()
    capital, desk_limit = _desk_context(database, desk_id)

    def result(status: Status, *reasons: str, **extra: Any) -> CalibrationResult:
        return CalibrationResult(status, desk_id, name, gate.metric, reasons=tuple(reasons), **extra)

    try:
        rules = rules or default_rules()
    except RulesUnavailable as exc:
        return result("unavailable", str(exc)[:200])
    found = (source or default_source)(name, as_of)
    if found is None or not found.days:
        return result("data_missing", "no_chain_history")
    history = found.until(as_of.astimezone(timezone.utc).date())
    if len(history.days) < gate.min_history_days:
        return result("data_missing", f"chain_history_too_short:{len(history.days)}<{gate.min_history_days}")
    series = metric_series(history, gate)
    if int(np.isfinite(series).sum()) < MIN_METRIC_POINTS:
        return result("data_missing", f"metric_history_too_short:{int(np.isfinite(series).sum())}<{MIN_METRIC_POINTS}")
    grid = threshold_grid(series)
    points = [{"structure": s, "threshold": th} for s in gate.structures for th in grid]
    limits.check_param("n_combinations", len(points))
    sim = Simulation(history, gate, rules, capital)
    columns = [sim.column(p["structure"], p["threshold"], series) for p in points]
    pnl = np.column_stack([c[0] for c in columns])
    try:
        wf = walkforward.run(points, pnl, scheme="anchored", n_folds=gate.folds, purge=gate.dte[1], embargo=0)
    except (walkforward.WalkForwardError, BadInput) as exc:
        return result("data_missing", f"walk_forward_impossible:{str(exc)[:160]}")           # the history cannot hold the windows of the gate
    oos: list[Trade] = []
    for w in wf.windows:
        oos += [t for t in columns[w.column][1] if t.finished and any(a <= t.fill_idx < b for a, b in w.test_blocks)]
    expectancy = expectancy_per_margin_dollar(oos, seed=seed)
    matrix = walkforward.returns_matrix(pnl, points, capital_usd=float(capital), index=pd.DatetimeIndex([pd.Timestamp(d.day) for d in history.days]))
    evidence = bundle(matrix, n_trials=len(points), n_blocks=CSCV_BLOCKS, purge=gate.dte[1], embargo=2, wf_folds=gate.folds)
    pbo_value, pbo_columns = traded_pbo(matrix)
    evidence = dataclasses.replace(evidence, pbo=pbo_value, warnings=evidence.warnings + (f"pbo_over_traded_columns:{pbo_columns}/{matrix.shape[1]}",))
    drawdown = None if wf.oos_pnl is None or wf.oos_rows is None else worst_drawdown_pct(wf.oos_pnl, wf.oos_rows, history.days, capital)
    limit = min((x for x in (gate.max_drawdown_pct, desk_limit) if x is not None), default=None)
    reasons = _passes(gate, oos=oos, expectancy=expectancy, evidence=evidence, efficiency=wf.efficiency, drawdown=drawdown, limit=limit)
    last = wf.windows[-1]
    measures = {"oos_trades": len(oos), "expectancy_per_margin_dollar": expectancy, "dsr": evidence.dsr, "pbo": evidence.pbo, "pbo_columns": pbo_columns, "wf_efficiency": wf.efficiency,
                "max_drawdown_pct": drawdown, "drawdown_limit_pct": limit, "n_trials": len(points), "thresholds": grid, "structures": list(gate.structures),
                "chosen": dict(last.chosen), "history_days": len(history.days), "first_day": history.days[0].day, "last_day": history.days[-1].day,
                "data_hash": history.data_hash, "rules": rules.name, "walk_forward": wf.to_dict()}
    body = {"kind": "premium_calibration", "gate": gate.to_dict(), "measures": measures, "reasons": reasons, "warnings": list(evidence.warnings)}
    run_id = _save_run(database, desk_id, name, history, gate, capital, as_of, seed, body)
    ref = f"calibration-{run_id}"
    if reasons:
        return result("not_passed", *reasons, measures=measures, run_id=run_id, backtest_run_ref=ref)
    threshold = Decimal(str(last.chosen["threshold"]))
    proof = dataclasses.replace(evidence, parameters={**evidence.parameters, "premium_calibration": _clean({"gate": gate.to_dict(), "measures": measures})})
    row = record_calibration(desk_id, name, metric=gate.metric, threshold=threshold, evidence=proof, backtest_run_ref=ref, db=database)
    return result("calibrated", measures=measures, run_id=run_id, backtest_run_ref=ref, threshold=threshold, threshold_id=row)


# ---- recalibration -----------------------------------------------------------------------------------------------------

def _age_days(calibrated_at: str, now: datetime) -> float:
    stamp = datetime.fromisoformat(calibrated_at.replace("Z", "+00:00"))
    stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds() / 86400


def _recalibrate_row(row: CalibratedRow, desk_id: int, now: datetime, database: Database, **kwargs: Any) -> CalibrationResult:
    res = calibrate(desk_id, row.underlying, as_of=now, db=database, **kwargs)
    reason = ref = None
    if res.status == "not_passed":
        reason, ref = "recalibration_failed:" + ";".join(res.reasons), res.backtest_run_ref
    elif res.status == "data_missing" and _age_days(row.calibrated_at, now) > STALE_DAYS:
        reason, ref = "calibration_stale_no_history:" + ";".join(res.reasons), row.backtest_run_ref
    if reason is not None and ref is not None:
        try:
            retire_calibration(row.row_id, reason=reason[:200], backtest_run_ref=ref, db=database)
            res = dataclasses.replace(res, retired_id=row.row_id)
        except ValueError:
            pass                                                      # another run already replaced or retired this row: the switch is as it should be
    return res


def recalibrate(desk_id: int, *, as_of: datetime | None = None, db: Database | None = None, **kwargs: Any) -> list[CalibrationResult]:
    """Measure again every calibrated underlying of a desk (the monthly job and the drift alarm). One that does not pass is retired with
    `retire_calibration` and its switch closes: the reasons and the run that proved them stay in the evidence of the row. One that cannot be
    measured because the history is missing is kept until it is older than `STALE_DAYS` and is retired then. One that is measured and passes
    is replaced by the new row. `unavailable` (the options rules are not here) changes nothing. `kwargs` are those of `calibrate`."""
    database = db or get_database()
    now = as_of or datetime.now(timezone.utc)
    return [_recalibrate_row(row, desk_id, now, database, **kwargs) for row in calibrated_rows(desk_id, db=database)]


def request_calibration(desk_id: int, underlying: str | None = None, *, as_of: datetime | None = None, db: Database | None = None,
                        **kwargs: Any) -> list[CalibrationResult]:
    """What the tool `premium_calibrate` runs. Without an underlying: `recalibrate` of the desk. With one: its recalibration when it has a
    calibrated row (so a failure retires it) and a first calibration when it has not."""
    database = db or get_database()
    now = as_of or datetime.now(timezone.utc)
    if underlying is None:
        return recalibrate(desk_id, as_of=now, db=database, **kwargs)
    try:
        name = _clean_underlying(underlying)
    except ValueError as exc:
        raise CalibrationError(str(exc)) from None
    row = next((r for r in calibrated_rows(desk_id, db=database) if r.underlying == name), None)
    return [calibrate(desk_id, name, as_of=now, db=database, **kwargs) if row is None else _recalibrate_row(row, desk_id, now, database, **kwargs)]


def switch_overview(desk_id: int, underlyings: Sequence[str] | None = None, *, db: Database | None = None) -> list[dict[str, Any]]:
    """The state of the switch of a desk, for the underlyings asked or for every one that has a calibrated row. Read only."""
    from hokma.desks.swo import premium_switch

    database = db or get_database()
    names = list(underlyings) if underlyings is not None else [r.underlying for r in calibrated_rows(desk_id, db=database)]
    return premium_switch.describe(desk_id, names, db=database)


def on_drift_alarm(desk_id: int, *, alarm: bool = True, **kwargs: Any) -> list[CalibrationResult]:
    """The hook of the drift alarm of the execution monitor: when the alarm rings, the thresholds of the desk are measured again now."""
    return recalibrate(desk_id, **kwargs) if alarm else []


# ---- the scheduled job and who may ask ----------------------------------------------------------------------------------

def ensure_recalibration_job(db: Database | None = None) -> Any:
    """The monthly routine that recalibrates the desks, created OFF (turning it on is a decision of the owner). It never duplicates and never
    edits one that exists. The routine asks `premium_calibrate`; the tool lets it in because its task is the last task of this job."""
    from hokma.kit.integrity import app_root

    database = db or get_database()
    prompt = (app_root() / "kit" / "template" / "routines" / "premium_recalibration.md").read_text(encoding="utf-8")
    with database.write() as u:
        found = u.scheduled_jobs.find_by_name(JOB_NAME)
        if found is not None:
            return found
        return u.scheduled_jobs.create(JOB_NAME, "hokma", prompt, *JOB_SCHEDULE, u.profile.get_or_default().timezone, is_active=False)


class CalibrationDenied(CalibrationError):
    """The caller may not ask. `code` says why and is stable: routine_off, wrong_task, wrong_agent, task_not_running, first_calibration_by_routine."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def authorize_calibration(agent: str | None, task_id: int | None, underlying: str | None = None, desk_id: int | None = None, *,
                          db: Database | None = None) -> None:
    """Raise CalibrationDenied unless the caller may ask. The owner (a session with no agent) may always ask. The routine may ask only when the
    job is active, the caller is the agent of the job, `task_id` is the last task of the job and that task is still running and claimed by that
    agent. All of it is read in one transaction. The routine can only measure again an underlying that already has a row of a past calibration
    (a row that is calibrated now); the first calibration, and the reopening of a retired underlying, are the owner's. `agent` and `task_id` come from the engine environment, never from a tool argument."""
    if agent is None:
        return
    with (db or get_database()).read() as u:
        job = u.scheduled_jobs.find_by_name(JOB_NAME)
        if job is None or not job.is_active:
            raise CalibrationDenied("routine_off", "the recalibration routine is not active")
        if job.agent_slug != agent:
            raise CalibrationDenied("wrong_agent", "the caller is not the agent of the recalibration routine")
        if task_id is None or job.last_task_id != task_id:
            raise CalibrationDenied("wrong_task", "the task is not the last task of the recalibration routine")
        task = u.tasks.find(task_id)
        if task is None or task.status != "running" or task.claimed_by != agent:
            raise CalibrationDenied("task_not_running", "the task of the routine is not running under the agent")
        if underlying is not None:
            wanted = _clean_underlying(underlying)
            if desk_id is None or not any(r.underlying == wanted for r in calibrated_rows(desk_id, db=db)):
                raise CalibrationDenied("first_calibration_by_routine", "the first calibration of an underlying is asked by the owner, not by the routine")


def caller_may_calibrate(agent: str | None, task_id: int | None, *, db: Database | None = None) -> bool:
    """True when `authorize_calibration` lets the caller in without an underlying (the owner or the running task of the active routine)."""
    try:
        authorize_calibration(agent, task_id, db=db)
    except CalibrationDenied:
        return False
    return True
