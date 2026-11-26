"""Continuous futures with back adjustment and a validated chain of rolls (SPEC-020: L-22, L-23, N-33, P-06).

One implementation for the three questions: how a roll date is chosen (`RollRule`), how the prices before a roll are moved so that
the series does not jump (`Adjustment`), and what the size of a position is computed with (the price of the real contract, never the
adjusted one). A chain with a missing contract, a day with no bars or no price to compare at a roll fails with the date of the hole:
nothing is filled.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from math import floor
from typing import Literal

import numpy as np
import pandas as pd

from hokma.contracts import expiry, sessions
from hokma.contracts.identity import parse_canonical
from hokma.quant import limits
from hokma.quant.errors import BadInput

PRICE_COLUMNS = ("open", "high", "low", "close")
COLUMNS = (*PRICE_COLUMNS, "volume")
MAX_CONTRACTS = 200


class ChainError(ValueError):
    """The chain of contracts cannot make a continuous series. `code` is stable: bad_chain, missing_contract, data_gap, no_overlap,
    empty_segment, open_interest_missing. `day` is the date of the hole when there is one."""

    def __init__(self, code: str, detail: str, day: date | None = None) -> None:
        self.code, self.day, self.detail = code, day, detail
        super().__init__(f"{code}{' on ' + day.isoformat() if day else ''}: {detail}")


class Adjustment(str, Enum):
    """How the prices before a roll are moved. `raw` leaves the jump; `difference` (Panama) adds the roll gap and `ratio` multiplies by
    the roll ratio, both back from the last contract so the newest prices are the real ones."""
    RAW = "raw"
    DIFFERENCE = "difference"
    RATIO = "ratio"


@dataclass(frozen=True)
class RollRule:
    """When to leave a contract for the next one: N calendar days before its expiration, or the day the next contract trades
    more volume or has more open interest."""
    kind: Literal["days_before_expiry", "volume", "open_interest"]
    days: int = 0

    def __post_init__(self) -> None:
        if self.kind not in ("days_before_expiry", "volume", "open_interest"):
            raise ChainError("bad_chain", f"unknown roll rule {self.kind!r}")
        if self.kind == "days_before_expiry" and not (isinstance(self.days, int) and not isinstance(self.days, bool) and 0 <= self.days <= 400):
            raise ChainError("bad_chain", "days before expiry is a whole number from 0 to 400")

    @staticmethod
    def days_before(days: int) -> "RollRule":
        return RollRule("days_before_expiry", days)

    @staticmethod
    def by_volume() -> "RollRule":
        return RollRule("volume")

    @staticmethod
    def by_open_interest() -> "RollRule":
        return RollRule("open_interest")


@dataclass(frozen=True)
class ContractBars:
    """The bars of one contract: UTC index (a daily series is labelled at 00:00 UTC), columns open, high, low, close, volume."""
    contract_id: str
    frame: pd.DataFrame
    open_interest: Mapping[date, int] | None = None

    @property
    def root(self) -> str:
        return str(parse_canonical(self.contract_id).root)

    @property
    def expiry_day(self) -> date:
        parsed = parse_canonical(self.contract_id)
        if parsed.asset_type != "future" or parsed.expiration is None:
            raise ChainError("bad_chain", f"{self.contract_id} is not a future contract")
        return expiry.rules(self.root).expiry(parsed.expiration.year, parsed.expiration.month).day


@dataclass(frozen=True)
class Roll:
    from_contract: str
    to_contract: str
    day: date                   # the first day of the new contract
    reference_at: pd.Timestamp  # the last bar of the old contract, where both prices are compared
    old_price: float
    new_price: float

    @property
    def gap(self) -> float:
        return self.new_price - self.old_price

    @property
    def ratio(self) -> float:
        return self.new_price / self.old_price


@dataclass(frozen=True)
class ContinuousSeries:
    root: str
    adjustment: Adjustment
    frame: pd.DataFrame         # open, high, low, close, volume, raw_close, contract
    rolls: tuple[Roll, ...]


def _days(index: pd.DatetimeIndex, tz: str) -> np.ndarray:
    """The calendar date of every bar: the exchange date, or the label date of a series stamped at 00:00 UTC (daily and weekly)."""
    if (index == index.normalize()).all():
        return np.asarray(index.date)
    return np.asarray(index.tz_convert(tz).date)


def _check_frame(item: ContractBars) -> pd.DataFrame:
    frame = item.frame
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ChainError("bad_chain", f"{item.contract_id}: the bars need a time zone aware index")
    if missing := [c for c in COLUMNS if c not in frame.columns]:
        raise ChainError("bad_chain", f"{item.contract_id}: missing columns {missing}")
    if frame.empty or not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ChainError("bad_chain", f"{item.contract_id}: the bars are empty, unsorted or duplicated")
    return frame.tz_convert("UTC") if str(frame.index.tz) != "UTC" else frame


def _metric(item: ContractBars, kind: str, tz: str) -> dict[date, int | float]:
    if kind == "open_interest":
        if not item.open_interest:
            raise ChainError("open_interest_missing", f"{item.contract_id} has no open interest")
        return dict(item.open_interest)
    frame = item.frame
    return dict(frame["volume"].groupby(_days(frame.index, tz)).sum())


def _roll_day(rule: RollRule, old: ContractBars, new: ContractBars, tz: str) -> date:
    if rule.kind == "days_before_expiry":
        return old.expiry_day - timedelta(days=rule.days)
    metrics = {old.contract_id: _metric(old, rule.kind, tz), new.contract_id: _metric(new, rule.kind, tz)}
    front = expiry.front_by_open_interest(metrics, [old.contract_id, new.contract_id])
    return next((day for day, cid in sorted(front.items()) if cid == new.contract_id), old.expiry_day)


def validate_chain(contracts: Sequence[ContractBars], rule: RollRule) -> tuple[ContractBars, ...]:
    """The contracts sorted by expiration, after the checks that need no prices: one root, no duplicates and no listed month between
    two neighbours (a missing contract fails with the day the chain would have nobody)."""
    if not contracts or len(contracts) > MAX_CONTRACTS:
        raise ChainError("bad_chain", f"the chain needs from 1 to {MAX_CONTRACTS} contracts")
    if len({c.contract_id for c in contracts}) != len(contracts):
        raise ChainError("bad_chain", "a contract appears twice")
    ordered = tuple(sorted(contracts, key=lambda c: c.expiry_day))
    root = ordered[0].root
    if any(c.root != root for c in ordered):
        raise ChainError("bad_chain", "all the contracts must have the same root")
    for old, new in zip(ordered, ordered[1:]):
        a, b = parse_canonical(old.contract_id).expiration, parse_canonical(new.contract_id).expiration
        between = expiry.listed_months(root, (a.replace(day=28) + timedelta(days=4)).replace(day=1), (b.replace(day=1) - timedelta(days=1)))
        if between:
            day = old.expiry_day - timedelta(days=rule.days) if rule.kind == "days_before_expiry" else old.expiry_day
            raise ChainError("missing_contract", f"no bars for {root} {between[0]:%Y-%m} between {old.contract_id} and {new.contract_id}", day)
    return ordered


def build(contracts: Sequence[ContractBars], rule: RollRule, adjustment: Adjustment = Adjustment.DIFFERENCE) -> ContinuousSeries:
    """The continuous series of a chain of contracts. Each contract serves from the roll day of the previous one to its own roll day;
    at every roll the new and old prices are compared at the last bar of the old contract (the new contract's last bar of that
    day or before, never later) and the older segments are moved by the gap or the ratio."""
    if sum(len(c.frame) * (len(c.frame.columns) + 1) for c in contracts) > limits.MAX_INPUT_CELLS:
        raise BadInput("the bars of the chain are above the limit of cells")
    ordered = validate_chain(contracts, rule)
    root = ordered[0].root
    tz = sessions.schedule(root).tz
    holidays = sessions.holidays(root)
    frames = [_check_frame(c) for c in ordered]
    days = [_days(f.index, tz) for f in frames]
    roll_days = [_roll_day(rule, ordered[i], ordered[i + 1], tz) for i in range(len(ordered) - 1)]
    if any(b <= a for a, b in zip(roll_days, roll_days[1:])):
        raise ChainError("bad_chain", "the roll days are not increasing", roll_days[0])
    pieces, rolls = [], []
    for i, (item, frame, day_of) in enumerate(zip(ordered, frames, days)):
        start = roll_days[i - 1] if i else None
        end = roll_days[i] if i < len(roll_days) else None
        keep = np.ones(len(frame), dtype=bool)
        if start is not None:
            keep &= day_of >= start
        if end is not None:
            keep &= day_of < end
        part = frame[keep]
        first_day, last_day = start or day_of[0], (end - timedelta(days=1)) if end else day_of[-1]
        if part.empty:
            raise ChainError("empty_segment", f"{item.contract_id} has no bars in its segment", first_day)
        present = set(day_of[keep])
        for day in expiry.business_days(first_day, last_day, holidays):
            if day not in present:
                raise ChainError("data_gap", f"{item.contract_id} has no bars on a business day it is the active contract", day)
        pieces.append(part)
    for i in range(len(pieces) - 1):
        at = pieces[i].index[-1]
        reference_day = days[i][frames[i].index.get_loc(at)]
        new_frame = frames[i + 1]
        position = new_frame.index.searchsorted(at, side="right") - 1
        if position < 0 or days[i + 1][position] != reference_day:
            raise ChainError("no_overlap", f"{ordered[i + 1].contract_id} has no price on {reference_day.isoformat()} to compare with {ordered[i].contract_id}", reference_day)
        rolls.append(Roll(ordered[i].contract_id, ordered[i + 1].contract_id, roll_days[i], at,
                          float(pieces[i]["close"].iloc[-1]), float(new_frame["close"].iloc[position])))
    adjusted = []
    for i, part in enumerate(pieces):
        out = part[list(COLUMNS)].astype(float).copy()
        out["raw_close"] = out["close"]
        later = rolls[i:]
        if adjustment is Adjustment.DIFFERENCE and later:
            out[list(PRICE_COLUMNS)] = out[list(PRICE_COLUMNS)] + float(sum(r.gap for r in later))
        elif adjustment is Adjustment.RATIO and later:
            out[list(PRICE_COLUMNS)] = out[list(PRICE_COLUMNS)] * float(np.prod([r.ratio for r in later]))
        out["contract"] = ordered[i].contract_id
        adjusted.append(out)
    return ContinuousSeries(root, adjustment, pd.concat(adjusted), tuple(rolls))


def panama(contracts: Sequence[ContractBars], rule: RollRule) -> ContinuousSeries:
    """The difference adjusted series (Panama splice). Size positions with `real_price`, never with its adjusted close."""
    return build(contracts, rule, Adjustment.DIFFERENCE)


def real_price(series: ContinuousSeries, at: pd.Timestamp) -> float:
    """The close of the contract that was active at `at`, as traded: the price a position is sized with."""
    return float(series.frame["raw_close"].loc[at])


def contracts_for_budget(series: ContinuousSeries, at: pd.Timestamp, *, budget: Decimal, multiplier: Decimal) -> int:
    """Whole contracts that a notional budget buys at the real price of the active contract."""
    price = Decimal(repr(real_price(series, at)))
    if price <= 0 or multiplier <= 0:
        raise BadInput("price and multiplier must be above 0")
    return floor(budget / (price * multiplier))
