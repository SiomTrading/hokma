"""Position sizing with exact arithmetic (SPEC-019: CTS-011).

The number of contracts is the exact floor of the risk budget over the risk of one contract, computed with rational numbers
so there is no epsilon and no float: 0.3 over 0.1 is 3, not 2. The risk of one contract is the distance between the entry and
the stop, times the multiplier of the contract sheet, times the exchange rate to USD.

Rules, each with its test:
- If not even one contract fits, the answer is `NO_TRADE`. The stop is never moved to make a contract fit: this module has no
  way to return a new stop.
- A stop closer than one tick to the entry is refused. A price that is not on the grid of the tick is refused: a bond quoted as
  `110.16` is not `110'16` (110 and 16 thirty-seconds), and only the second one is on the grid of 1/32.
- A currency other than USD with no exchange rate is refused. Absurd inputs (not finite, huge, zero or negative multiplier) are
  refused, and the count has a hard cap.

The module does not compute commissions or slippage (the budget does) and it does not know margin.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Literal, Sequence

from hokma.exec.money import MoneyError, dec

HARD_MAX_CONTRACTS = 10_000
MAX_BUDGET = Decimal("1e12")
ZERO = Decimal(0)
_BOND = re.compile(r"^(?P<whole>\d{1,6})'(?P<ticks>\d{2})(?P<frac>[+2567])?$")
_BOND_FRACTION = {None: Fraction(0), "+": Fraction(1, 2), "2": Fraction(1, 4), "5": Fraction(1, 2), "7": Fraction(3, 4), "6": Fraction(3, 4)}


class SizingError(ValueError):
    """The inputs of a size are not acceptable. `code` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SizeResult:
    status: Literal["OK", "NO_TRADE"]
    contracts: int
    risk_per_contract: Decimal          # USD
    risk_budget: Decimal                # USD
    stop_distance: Decimal              # price units, the distance the stop was given at: it is never changed
    risk_usd: Decimal                   # contracts times risk per contract
    capped: bool = False
    code: str = "ok"
    reason: str = ""


def parse_bond_price(text: str) -> Decimal:
    """A bond price in thirty-seconds: `110'16` is 110 and 16/32; a trailing `+` is half a thirty-second and a last digit
    2, 5 or 7 is a quarter, a half or three quarters of one. Anything else raises SizingError (a decimal `110.16` is not this)."""
    found = _BOND.match(text.strip()) if isinstance(text, str) else None
    if not found:
        raise SizingError("bad_price", f"{str(text)[:20]!r} is not a price in thirty-seconds such as 110'16")
    ticks = int(found.group("ticks"))
    if ticks > 31:
        raise SizingError("bad_price", "thirty-seconds go from 00 to 31")
    value = Fraction(int(found.group("whole"))) + (Fraction(ticks) + _BOND_FRACTION[found.group("frac")]) / 32
    return Decimal(value.numerator) / Decimal(value.denominator)


def _price(value: Any, name: str) -> Decimal:
    if isinstance(value, str) and "'" in value:
        return parse_bond_price(value)
    try:
        out = dec(value, name)
    except MoneyError as exc:
        raise SizingError("bad_number", str(exc)) from exc
    return out


def _amount(value: Any, name: str) -> Decimal:
    try:
        return dec(value, name)
    except MoneyError as exc:
        raise SizingError("bad_number", str(exc)) from exc


def on_grid(price: Decimal, tick: Decimal) -> bool:
    """True when `price` is a whole number of ticks (exact, in rational numbers)."""
    return (Fraction(price) / Fraction(tick)).denominator == 1


def check_grid(price: Decimal, tick: Decimal | None, name: str = "price") -> None:
    if tick is not None and not on_grid(price, tick):
        raise SizingError("off_grid", f"{name} {price} is not on the grid of the tick {tick}")


def contracts(*, risk_budget_usd: Any, entry: Any, stop: Any, multiplier: Any, tick_size: Any = None, currency: str = "USD",
              fx_rate: Any = None, max_contracts: int = HARD_MAX_CONTRACTS) -> SizeResult:
    """Contracts for one risk budget. `entry` and `stop` are prices (a bond may be written `110'16`). Raises SizingError for
    inputs that cannot be sized; returns `NO_TRADE` when the budget does not buy one contract."""
    budget = _amount(risk_budget_usd, "risk_budget_usd")
    mult = _amount(multiplier, "multiplier")
    entry_p, stop_p = _price(entry, "entry"), _price(stop, "stop")
    tick = None if tick_size is None else _amount(tick_size, "tick_size")
    if budget < 0 or budget > MAX_BUDGET:
        raise SizingError("absurd_budget", "the risk budget must be zero or more and below 1e12")
    if mult <= 0:
        raise SizingError("bad_multiplier", "the multiplier of the contract sheet must be above zero")
    if tick is not None and tick <= 0:
        raise SizingError("bad_tick", "the tick must be above zero")
    if entry_p <= 0 or stop_p <= 0:
        raise SizingError("bad_price", "entry and stop must be above zero")
    if isinstance(max_contracts, bool) or not isinstance(max_contracts, int) or not 1 <= max_contracts <= HARD_MAX_CONTRACTS:
        raise SizingError("bad_cap", f"max_contracts must be a whole number from 1 to {HARD_MAX_CONTRACTS}")
    if currency != "USD":
        if fx_rate is None:
            raise SizingError("fx_missing", f"the currency {str(currency)[:8]} needs an exchange rate to USD")
        fx = _amount(fx_rate, "fx_rate")
        if fx <= 0:
            raise SizingError("bad_fx", "the exchange rate must be above zero")
    else:
        fx = Decimal(1)
    check_grid(entry_p, tick, "entry")
    check_grid(stop_p, tick, "stop")
    distance = abs(entry_p - stop_p)
    if tick is not None and distance < tick:
        raise SizingError("stop_inside_tick", f"the stop is {distance} from the entry, less than one tick of {tick}")
    if distance == 0:
        raise SizingError("stop_inside_tick", "the stop is at the entry")
    per_contract = distance * mult * fx
    whole = (Fraction(budget) / Fraction(per_contract)).__floor__()
    if whole < 1:
        return SizeResult("NO_TRADE", 0, per_contract, budget, distance, ZERO, False, "no_trade_budget",
                          f"the budget {budget} does not buy one contract that risks {per_contract}; the stop stays where it is")
    capped = whole > max_contracts
    count = max_contracts if capped else int(whole)
    return SizeResult("OK", count, per_contract, budget, distance, per_contract * count, capped, "capped" if capped else "ok",
                      f"capped at {max_contracts} contracts" if capped else "")


# ---- the loss of an option structure --------------------------------------------------------------------------------

@dataclass(frozen=True)
class OptionLegInfo:
    kind: Literal["C", "P"]
    side: Literal["buy", "sell"]
    strike: Decimal


def max_loss_per_unit_usd(structure: str, legs: Sequence[OptionLegInfo], net_premium: Decimal, multiplier: Decimal,
                          stock_price: Decimal | None = None) -> Decimal | None:
    """The most one unit of a structure can lose, in USD, from its strikes and its net premium per share (positive is a debit).
    `None` for the structures whose loss has no bound. A covered call needs the price of the shares."""
    if multiplier <= 0:
        raise SizingError("bad_multiplier", "the multiplier of the contract sheet must be above zero")
    credit = -net_premium
    strikes = sorted(leg.strike for leg in legs)
    if structure == "protective_put":
        # the shares at their price plus the put paid, down to the strike of the put: what is left below is the most the pair can lose
        if net_premium <= 0:
            raise SizingError("bad_premium", "a debit structure has a net premium above zero")
        if stock_price is None or stock_price <= 0:
            raise SizingError("bad_price", "a protective put needs the price of the shares")
        return max(stock_price + net_premium - strikes[0], Decimal(0)) * multiplier
    if structure in ("long_call", "long_put", "debit_call_spread", "debit_put_spread"):
        if net_premium <= 0:
            raise SizingError("bad_premium", "a debit structure has a net premium above zero")
        return net_premium * multiplier
    if structure in ("short_call", "short_straddle", "short_strangle"):
        return None
    if credit <= 0:
        raise SizingError("bad_premium", "a credit structure has a net premium below zero")
    if structure in ("credit_call_spread", "credit_put_spread"):
        width = strikes[-1] - strikes[0]
        loss = width - credit
    elif structure in ("iron_condor", "iron_butterfly"):
        puts = sorted(leg.strike for leg in legs if leg.kind == "P")
        calls = sorted(leg.strike for leg in legs if leg.kind == "C")
        loss = max(puts[-1] - puts[0], calls[-1] - calls[0]) - credit
    elif structure in ("short_put", "cash_secured_put"):
        loss = strikes[0] - credit
    elif structure == "covered_call":
        if stock_price is None or stock_price <= 0:
            raise SizingError("bad_price", "a covered call needs the price of the shares")
        loss = stock_price - credit
    else:
        raise SizingError("bad_structure", f"unknown structure {str(structure)[:30]!r}")
    if loss <= 0:
        raise SizingError("bad_premium", "the credit is as large as the most the structure can lose")
    return loss * multiplier
