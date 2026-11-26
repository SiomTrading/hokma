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

Other ways to size, all in this module so the desk and the backtest share them: `fixed_risk` (a fixed amount at risk over a stop distance,
through `contracts`), `vol_target` (contracts that carry a target volatility, in seven steps) and `buffer` (a dead band around the target).
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


# ---- fixed risk, volatility target and the dead band -----------------------------------------------------------------------

def fixed_risk(*, risk_usd: Any, entry: Any, multiplier: Any, stop_distance: Any = None, stop_ticks: Any = None, tick_size: Any = None,
               side: Literal["long", "short"] = "long", currency: str = "USD", fx_rate: Any = None,
               max_contracts: int = HARD_MAX_CONTRACTS) -> SizeResult:
    """Contracts (or CFD units) that risk a fixed amount: the risk over the stop distance times the multiplier, rounded down.

    The stop is given as a distance in price units (`stop_distance`, the points of a CFD) or in ticks (`stop_ticks`, with `tick_size`).
    The stop price is built from the side and handed to `contracts`, so the exact floor, the grid checks and the refusals are the same."""
    if side not in ("long", "short"):
        raise SizingError("bad_side", "side is long or short")
    if (stop_distance is None) == (stop_ticks is None):
        raise SizingError("bad_stop", "give the stop as a distance or as a number of ticks, not both and not neither")
    entry_p = _price(entry, "entry")
    if stop_ticks is not None:
        if tick_size is None:
            raise SizingError("bad_tick", "a stop in ticks needs the tick size")
        if isinstance(stop_ticks, bool) or not isinstance(stop_ticks, int) or stop_ticks < 1:
            raise SizingError("bad_stop", "stop_ticks must be a whole number of one or more")
        distance = _amount(tick_size, "tick_size") * stop_ticks
    else:
        distance = _amount(stop_distance, "stop_distance")
    if distance <= 0:
        raise SizingError("bad_stop", "the stop distance must be above zero")
    stop_p = entry_p - distance if side == "long" else entry_p + distance
    return contracts(risk_budget_usd=risk_usd, entry=entry_p, stop=stop_p, multiplier=multiplier, tick_size=tick_size, currency=currency,
                     fx_rate=fx_rate, max_contracts=max_contracts)


@dataclass(frozen=True)
class VolSize:
    """The seven steps of a volatility target size, so the number of contracts can be audited line by line."""
    contracts: int                       # signed: a negative forecast is a short
    exact_position: Decimal              # step 6, before the rounding
    annual_cash_target: Decimal          # step 1: capital times the target volatility
    period_cash_target: Decimal          # step 2: the same target for one period
    price_volatility: Decimal            # step 3: volatility in price units
    contract_volatility: Decimal         # step 4: volatility of one contract in USD
    scalar: Decimal                      # step 5: contracts that match the target at the average forecast
    capped: bool = False                 # step 7: the cap (the prop firm limit) cut the size
    code: str = "ok"


MAX_TARGET_VOL = Decimal(5)
MAX_PERIODS_PER_YEAR = 366


def vol_target(*, capital: Any, target_vol: Any, price: Any, instrument_vol: Any, multiplier: Any, vol_basis: Literal["daily", "annual"] = "daily",
               periods_per_year: int = 252, forecast: Any = None, average_forecast: Any = 10, currency: str = "USD", fx_rate: Any = None,
               max_contracts: int = HARD_MAX_CONTRACTS) -> VolSize:
    """Contracts that make the position carry the target volatility of the capital, in seven steps.

    1. annual cash target = capital times `target_vol` (a fraction: 0.20 is 20 percent a year).
    2. the target of one period = the annual one over the square root of `periods_per_year` (only when `vol_basis` is "daily").
       DT passes the volatility of one session as "daily"; SWC passes the daily one with its annual objective.
    3. price volatility = `price` times `instrument_vol` (a fraction of the price over the same horizon as step 2).
    4. contract volatility = price volatility times the multiplier of the contract sheet times the exchange rate to USD.
    5. scalar = the period cash target over the contract volatility.
    6. position = scalar times `forecast` over `average_forecast` (no forecast: the full scalar, long).
    7. the position is cut toward zero to whole contracts and capped at `max_contracts` (the prop firm limit for DT).

    Rounding is toward zero so the volatility is never above the target. A position of less than one contract is 0."""
    cap_usd = _amount(capital, "capital")
    tv, px, iv, mult = (_amount(target_vol, "target_vol"), _amount(price, "price"), _amount(instrument_vol, "instrument_vol"),
                        _amount(multiplier, "multiplier"))
    if not Decimal(0) < cap_usd <= MAX_BUDGET:
        raise SizingError("absurd_budget", "the capital must be above zero and below 1e12")
    if not Decimal(0) < tv <= MAX_TARGET_VOL:
        raise SizingError("bad_vol", f"target_vol is a fraction above zero and at most {MAX_TARGET_VOL}")
    if px <= 0 or iv <= 0:
        raise SizingError("bad_vol", "the price and the volatility of the instrument must be above zero")
    if mult <= 0:
        raise SizingError("bad_multiplier", "the multiplier of the contract sheet must be above zero")
    if vol_basis not in ("daily", "annual"):
        raise SizingError("bad_vol", "vol_basis is daily or annual")
    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int) or not 1 <= periods_per_year <= MAX_PERIODS_PER_YEAR:
        raise SizingError("bad_periods", f"periods_per_year must be a whole number from 1 to {MAX_PERIODS_PER_YEAR}")
    if isinstance(max_contracts, bool) or not isinstance(max_contracts, int) or not 1 <= max_contracts <= HARD_MAX_CONTRACTS:
        raise SizingError("bad_cap", f"max_contracts must be a whole number from 1 to {HARD_MAX_CONTRACTS}")
    avg = _amount(average_forecast, "average_forecast")
    if avg <= 0:
        raise SizingError("bad_forecast", "the average forecast must be above zero")
    fc = avg if forecast is None else _amount(forecast, "forecast")
    if currency != "USD":
        if fx_rate is None:
            raise SizingError("fx_missing", f"the currency {str(currency)[:8]} needs an exchange rate to USD")
        fx = _amount(fx_rate, "fx_rate")
        if fx <= 0:
            raise SizingError("bad_fx", "the exchange rate must be above zero")
    else:
        fx = Decimal(1)
    annual = cap_usd * tv
    period = annual / Decimal(periods_per_year).sqrt() if vol_basis == "daily" else annual
    price_vol = px * iv
    contract_vol = price_vol * mult * fx
    scalar = period / contract_vol
    exact = scalar * fc / avg
    whole = int(abs(exact) // 1)
    capped = whole > max_contracts
    count = max_contracts if capped else whole
    return VolSize(-count if exact < 0 else count, exact, annual, period, price_vol, contract_vol, scalar, capped, "capped" if capped else "ok")


@dataclass(frozen=True)
class BufferResult:
    traded: bool
    position: Decimal                    # the position after the rebalance
    trade: Decimal                       # signed change: zero when the position sits inside the band
    lower: Decimal                       # the band around the target
    upper: Decimal


def buffer(*, position: Any, target: Any, band: Any = Decimal("0.10")) -> BufferResult:
    """Dead band: no trade while the position is inside `target` plus or minus `band` times the size of the target.

    Outside the band the position moves only to the nearest edge, not to the target, so a small drift never turns into a full trade.
    `band` is a fraction (0.10 is 10 percent). A target of zero has no band: any position is traded to zero."""
    pos, tgt, b = _amount(position, "position"), _amount(target, "target"), _amount(band, "band")
    if not Decimal(0) <= b <= 1:
        raise SizingError("bad_band", "the band is a fraction from 0 to 1")
    half = abs(tgt) * b
    lower, upper = tgt - half, tgt + half
    if lower <= pos <= upper:
        return BufferResult(False, pos, ZERO, lower, upper)
    edge = lower if pos < lower else upper
    return BufferResult(True, edge, edge - pos, lower, upper)
