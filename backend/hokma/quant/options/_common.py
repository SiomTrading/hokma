"""Shared plumbing of the options module: input coercion, domain checks and result building.

Bad data never raises: it returns a QuantResult with value None and the reason in warnings.
BadInput is raised only for a wrong type or shape (a string where a number belongs, a list where a table belongs).
"""
from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import numpy as np

from hokma.quant import helpers
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult

NEW_YORK = ZoneInfo("America/New_York")
MINUTES_PER_YEAR = 525_600
EXPIRATION_HOUR = 16                    # standard equity options stop trading at 16:00 New York
MAX_VOL = 20.0                          # 2,000 percent: above this the input is a unit mistake, not a market
MAX_YEARS = 100.0
SQRT_2PI = math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def jsonable(value: Any) -> Any:
    """numpy, Decimal and date containers to plain python; non finite floats to None."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return jsonable(value.tolist())
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def num(label: str, value: Any, *, allow_none: bool = False) -> float | None:
    """A float from an int, float, Decimal or numpy number. Anything else (a string, a list, a bool) is BadInput."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, np.integer, np.floating)):
        raise BadInput(f"{label} must be a number, got {type(value).__name__}")
    return float(value)


def empty(name: str, unit: str, method: str, warnings: Iterable[str] | str, *, params: dict | None = None,
          piece_ids: tuple[str, ...] = ()) -> QuantResult:
    if isinstance(warnings, str):
        warnings = (warnings,)
    return QuantResult(name=name, value=None, unit=unit, method=method, params=jsonable(dict(params or {})),
                       warnings=tuple(warnings), piece_ids=tuple(piece_ids))


def build(name: str, value: Any, *, unit: str, method: str, params: dict, piece_ids: tuple[str, ...],
          warnings: Iterable[str] = (), as_of: datetime | None = None) -> QuantResult:
    """Final QuantResult. A scalar that is not finite, or an all-None dict, becomes an empty result."""
    clean = jsonable(value)
    warns = tuple(warnings)
    if clean is None or (isinstance(clean, dict) and clean and all(v is None for v in clean.values())):
        return empty(name, unit, method, warns + ("result is not finite",), params=params, piece_ids=piece_ids)
    clean_params = jsonable(dict(params))
    return QuantResult(name=name, value=clean, unit=unit, method=method, params=clean_params,
                       inputs_hash=helpers.hash_inputs(clean_params), as_of=as_of, warnings=warns,
                       piece_ids=tuple(piece_ids))


def guarded(name: str, unit: str, method: str, pieces: tuple[str, ...], params: dict, compute: Callable[[], Any],
            warnings: Iterable[str] = ()) -> QuantResult:
    """Run `compute`. A numeric failure on odd data is a data problem: value None and the reason in warnings.

    `warnings` may be a list that `compute` fills while it runs: it is read after `compute` returns.
    """
    try:
        value = compute()
    except BadInput:
        raise
    except (ArithmeticError, ValueError, TypeError, KeyError, IndexError, np.linalg.LinAlgError) as exc:
        return empty(name, unit, method, tuple(warnings) + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    return build(name, value, unit=unit, method=method, params=params, piece_ids=pieces, warnings=tuple(warnings))   # a list that compute filled counts


def kind_flag(kind: Any) -> bool | None:
    """True for a put, False for a call, None when it is neither."""
    if not isinstance(kind, str):
        raise BadInput(f"kind must be 'call' or 'put', got {type(kind).__name__}")
    k = kind.strip().lower()
    if k in ("call", "c"):
        return False
    if k in ("put", "p"):
        return True
    return None


def bs_domain(spot: float, strike: float, rate: float, volatility: float, tte: float, dividend_yield: float,
              *, allow_zero_time: bool = False) -> str | None:
    """None when the Black-Scholes inputs are inside the domain; otherwise the reason."""
    values = (spot, strike, rate, volatility, tte, dividend_yield)
    if not all(math.isfinite(v) for v in values):
        return "inputs must be finite numbers (NaN or infinity found)"
    if spot <= 0 or strike <= 0:
        return "spot and strike must be positive"
    if volatility <= 0 or volatility > MAX_VOL:
        return f"volatility must be a decimal in (0, {MAX_VOL:g}], for example 0.25 for 25 percent"
    if tte < 0 or (tte == 0 and not allow_zero_time) or tte > MAX_YEARS:
        return "time_to_expiration must be positive years" if not allow_zero_time else "time_to_expiration must be zero or positive years"
    if abs(rate) > 1.0 or abs(dividend_yield) > 1.0:
        return "rate and dividend_yield must be decimals (0.04 for 4 percent)"
    return None


def d1_d2(spot: float, strike: float, rate: float, volatility: float, tte: float, q: float) -> tuple[float, float]:
    root = volatility * math.sqrt(tte)
    d1 = (math.log(spot / strike) + (rate - q) * tte + 0.5 * volatility * volatility * tte) / root
    return d1, d1 - root


def bs_price(spot: float, strike: float, rate: float, volatility: float, tte: float, q: float, put: bool) -> float:
    """Black-Scholes value; intrinsic at expiry."""
    if tte <= 0:
        return max(strike - spot, 0.0) if put else max(spot - strike, 0.0)
    d1, d2 = d1_d2(spot, strike, rate, volatility, tte, q)
    if put:
        return strike * math.exp(-rate * tte) * norm_cdf(-d2) - spot * math.exp(-q * tte) * norm_cdf(-d1)
    return spot * math.exp(-q * tte) * norm_cdf(d1) - strike * math.exp(-rate * tte) * norm_cdf(d2)


def bs_delta(spot: float, strike: float, rate: float, volatility: float, tte: float, q: float, put: bool) -> float:
    d1, _ = d1_d2(spot, strike, rate, volatility, tte, q)
    carry = math.exp(-q * tte)
    return -carry * norm_cdf(-d1) if put else carry * norm_cdf(d1)


def years_between(as_of: Any, expiration: Any) -> float | None:
    """Years from `as_of` to 16:00 New York of the expiration date (minutes over 525,600, the convention of the chain).

    `as_of` is a datetime or an ISO string (a naive one is read as New York time); `expiration` is a date or an ISO date.
    None when either cannot be read.
    """
    try:
        now = as_of if isinstance(as_of, datetime) else datetime.fromisoformat(str(as_of))
        if now.tzinfo is None:
            now = now.replace(tzinfo=NEW_YORK)
        day = expiration.date() if isinstance(expiration, datetime) else (expiration if isinstance(expiration, date) else date.fromisoformat(str(expiration)[:10]))
    except (TypeError, ValueError):
        return None
    end = datetime(day.year, day.month, day.day, EXPIRATION_HOUR, 0, tzinfo=NEW_YORK)
    return (end - now).total_seconds() / 60.0 / MINUTES_PER_YEAR


def positive_int(label: str, value: Any, *, low: int = 1) -> int:
    """A whole number at least `low`. A float with a fraction or a string is BadInput; the cap is checked by limits."""
    from hokma.quant import limits

    out = limits.as_int(label, value)
    limits.check_param(label, out)
    if out < low:
        raise BadInput(f"{label} must be at least {low}, got {out}")
    return out


CHAIN_FIELDS = ("symbol", "type", "strike", "expiration", "bid", "ask", "mid", "iv", "delta", "open_interest", "volume", "last")


def contracts_list(contracts: Any, *, label: str = "contracts") -> list[dict[str, Any]]:
    """The rows of a chain as plain dicts. A list of dicts, or the answer of the chain tool with a "contracts" list.

    A wrong container raises BadInput. Rows that are not dicts raise BadInput; rows with missing prices stay (they are
    dropped by the function that needs the field).
    """
    if isinstance(contracts, dict) and "contracts" in contracts:
        contracts = contracts["contracts"]
    if not isinstance(contracts, (list, tuple)):
        raise BadInput(f"{label} must be a list of contract rows, got {type(contracts).__name__}")
    rows: list[dict[str, Any]] = []
    for i, row in enumerate(contracts):
        if not isinstance(row, dict):
            raise BadInput(f"{label}[{i}] must be a dict, got {type(row).__name__}")
        rows.append(row)
    return rows


def row_float(row: dict[str, Any], key: str) -> float | None:
    """A finite float from a chain row field, None when missing, not numeric, NaN or the provider's no-data marker."""
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= -999.0:
        return None
    return f


def row_kind(row: dict[str, Any]) -> bool | None:
    """True for a put row, False for a call row, None otherwise (reads "type" or "kind")."""
    raw = row.get("type", row.get("kind"))
    if not isinstance(raw, str):
        return None
    return kind_flag(raw)


COMMON_INPUTS: dict[str, dict] = {
    "spot": {"type": "float", "required": True},
    "strike": {"type": "float", "required": True},
    "rate": {"type": "float", "required": True},
    "volatility": {"type": "float", "required": True},
    "time_to_expiration": {"type": "float", "required": True},
    "dividend_yield": {"type": "float", "required": False, "default": 0.0},
    "kind": {"type": "str", "required": False, "default": "call"},
}


def pick(*names: str, **extra: dict) -> dict[str, dict]:
    """The input description of a function: shared fields by name plus its own."""
    out = {n: dict(COMMON_INPUTS[n]) for n in names}
    out.update(extra)
    return out
