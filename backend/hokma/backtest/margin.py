"""Margin, margin calls and buying power of the simulated account (SPEC-020: B-11, L-11, L-19, L-20, N-07).

Pure functions over Decimal. The margin per contract of a past date comes from `hokma.contracts.history` and carries its warning
`historical_margin_missing` when the series does not reach the date; the simulator copies the warnings into the result. A rejected
order is recorded with its stable code so the result can count it.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from hokma.contracts import expiry, history

INSUFFICIENT_MARGIN = "insufficient_margin"
INTRADAY_TRADING_RULE = "intraday_trading_rule"
MARGIN_CALL_TAG = "margin_call"
PDT_MINIMUM_EQUITY = Decimal(25000)
PDT_WINDOW_BUSINESS_DAYS = 5
PDT_LIMIT = 3                           # a fourth day trade inside the window flags the account
ZERO = Decimal(0)


@dataclass(frozen=True)
class IntradayMargin:
    """The margin per contract that the rule of the funding account asks while the session is open (DT)."""
    per_contract: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.per_contract, Decimal) or not self.per_contract.is_finite() or self.per_contract <= 0:
            raise ValueError("the intraday margin is a Decimal above 0")


@dataclass(frozen=True)
class MarginRequirement:
    """Margin per contract in force. `mode` is `intraday` only while the session is open and an intraday margin is set."""
    root: str
    on: date
    initial: Decimal
    maintenance: Decimal
    source: str                         # series, current_sheet or intraday
    mode: Literal["overnight", "intraday"] = "overnight"
    warnings: tuple[str, ...] = ()

    def initial_for(self, qty: Decimal | int) -> Decimal:
        return self.initial * abs(Decimal(qty))

    def maintenance_for(self, qty: Decimal | int) -> Decimal:
        return self.maintenance * abs(Decimal(qty))


@dataclass(frozen=True)
class OrderCheck:
    ok: bool
    code: str | None
    required: Decimal
    available: Decimal


@dataclass(frozen=True)
class Rejection:
    code: str
    contract_id: str
    at: datetime
    required: Decimal
    available: Decimal


@dataclass
class Rejections:
    """The orders that the account refused. `counts()` is the `rejected` mapping of the result."""
    entries: list[Rejection] = field(default_factory=list)

    def record(self, check: OrderCheck, contract_id: str, at: datetime) -> None:
        if check.ok or check.code is None:
            raise ValueError("only a refused check is recorded")
        self.entries.append(Rejection(check.code, contract_id, at, check.required, check.available))

    def counts(self) -> Mapping[str, int]:
        return dict(Counter(e.code for e in self.entries))


# ------------------------------------------------------------------------------------------------------ L-11, N-07
def futures_margin(root: str, on: date, *, intraday: IntradayMargin | None = None, in_session: bool = True,
                   current: Mapping[str, tuple[Decimal, Decimal]] | None = None) -> MarginRequirement:
    """The margin per contract of a future on a date. With `intraday` set and the session open, the intraday amount is both the
    initial and the maintenance margin; outside the session, or without it, the exchange margin of the date applies, so a position
    carried through the close must meet the (higher) maintenance margin."""
    found = history.margin(root, on, current=current)
    if intraday is not None and in_session:
        return MarginRequirement(found.root, on, intraday.per_contract, intraday.per_contract, "intraday", "intraday", found.warnings)
    return MarginRequirement(found.root, on, found.initial, found.maintenance, found.source, "overnight", found.warnings)


def leveraged_margin(price: Decimal, qty: Decimal | int, multiplier: Decimal, leverage: Decimal) -> Decimal:
    """The margin of a leveraged instrument (a CFD): its notional divided by the leverage the sheet allows."""
    if not isinstance(leverage, Decimal) or leverage < 1:
        raise ValueError("leverage is a Decimal of at least 1")
    return abs(Decimal(qty)) * price * multiplier / leverage


# ------------------------------------------------------------------------------------------------------ B-11
def check_order(requirement: MarginRequirement, qty: Decimal | int, *, equity: Decimal, used_margin: Decimal = ZERO) -> OrderCheck:
    """Whether the account can open `qty` contracts: the free equity (equity minus the margin already used) must cover the initial
    margin. A refusal carries `insufficient_margin`."""
    available = equity - used_margin
    required = requirement.initial_for(qty)
    if available < required:
        return OrderCheck(False, INSUFFICIENT_MARGIN, required, available)
    return OrderCheck(True, None, required, available)


# ------------------------------------------------------------------------------------------------------ L-19
@dataclass(frozen=True)
class MarginCall:
    """A forced liquidation: the whole position closes at the open of the next bar and the trade is tagged `margin_call`."""
    tag: str
    trigger_bar: int
    fill_bar: int
    qty: Decimal
    equity: Decimal
    maintenance: Decimal


def margin_call(*, equity: Decimal, maintenance: Decimal, position_qty: Decimal | int, bar_index: int) -> MarginCall | None:
    """A call when the equity at the close of bar `bar_index` is below the maintenance margin of an open position."""
    qty = Decimal(position_qty)
    if qty == 0 or equity >= maintenance:
        return None
    return MarginCall(MARGIN_CALL_TAG, bar_index, bar_index + 1, abs(qty), equity, maintenance)


# ------------------------------------------------------------------------------------------------------ L-20
def buying_power(equity: Decimal, *, regular_hours: bool = True, account: Literal["margin", "cash"] = "margin") -> Decimal:
    """What a stock account can buy: cash accounts 1x; margin accounts 4x in regular hours with at least 25000 USD of equity and 2x
    otherwise (below that minimum, or outside regular hours)."""
    if account == "cash":
        return max(equity, ZERO)
    leverage = Decimal(4) if regular_hours and equity >= PDT_MINIMUM_EQUITY else Decimal(2)
    return max(equity, ZERO) * leverage


@dataclass
class DayTradeLedger:
    """Day trades of a stock account. Under 25000 USD of equity a fourth day trade inside five business days is refused."""
    holidays: Collection[date] = ()
    days: list[date] = field(default_factory=list)

    def record(self, day: date) -> None:
        self.days.append(day)

    def check(self, day: date, equity: Decimal) -> OrderCheck:
        first = expiry.add_business_days(day, -(PDT_WINDOW_BUSINESS_DAYS - 1), self.holidays)
        inside = sum(1 for d in self.days if first <= d <= day)
        if equity < PDT_MINIMUM_EQUITY and inside >= PDT_LIMIT:
            return OrderCheck(False, INTRADAY_TRADING_RULE, Decimal(inside + 1), Decimal(PDT_LIMIT))
        return OrderCheck(True, None, Decimal(inside + 1), Decimal(PDT_LIMIT))
