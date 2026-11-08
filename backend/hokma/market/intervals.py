"""The chart interval grammar: a time interval (`30s`, `7m`, `3h`, `2D`, `1W`) or a range (`10r` ticks, `2.5rp` price,
canonical `r:10t` / `r:2.5p`).

The semantic authority is the shared table `tests/market_fixtures/intervals_cases.json`, which the TypeScript
validator also loads. Rules:
- seconds 1..3600, minutes 1..1440, hours 1..168, days (sessions) 1..365, weeks 1..52;
- a whole number of the next unit is promoted: seconds to minutes, minutes to hours (`60s` is `1m`, `3600s` and `60m`
  are `1h`). Hours never become days and days never become weeks;
- the unit is exact: `s`, `m`, `h` lowercase; `D` and `W` are normalized to uppercase; a capital `M` is months,
  which are not available;
- spaces around the text are trimmed, spaces inside are invalid; leading zeros are fine.
"""
import re
from dataclasses import dataclass

from hokma.market.models import VISIBLE_BARS

DEFAULT_VISIBLE_BARS = 300
MONTHS = "months"
INVALID = "invalid"

_LIMITS = {"s": 3600, "m": 1440, "h": 168, "D": 365, "W": 52}
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "D": 86400, "W": 604800}
_MAX_TICKS = 10000

_TIME = re.compile(r"([0-9]+)([smhDdWw])")
_MONTHS = re.compile(r"[0-9]*M")
_TICKS_SHORT = re.compile(r"([0-9]+)r")
_TICKS_CANON = re.compile(r"r:([0-9]+)t")
_PRICE_SHORT = re.compile(r"([0-9]+(?:\.[0-9]+)?)rp")
_PRICE_CANON = re.compile(r"r:([0-9]+(?:\.[0-9]+)?)p")


class IntervalError(ValueError):
    """`code` is `months` or `invalid`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TimeInterval:
    n: int
    unit: str            # s | m | h | D | W
    seconds: int
    canonical: str

    def to_protocol(self, tick_size: float | None = None) -> dict:
        return {"kind": "time", "seconds": self.seconds}

    @property
    def is_session_based(self) -> bool:
        """Days and weeks count sessions, not seconds: `1D` is a session, not 24 hours."""
        return self.unit in ("D", "W")


@dataclass(frozen=True)
class RangeInterval:
    size: float | int
    unit: str            # t (ticks) | p (price)
    canonical: str

    def to_protocol(self, tick_size: float | None = None) -> dict:
        return {"kind": "range", "size": self.size, "unit": self.unit, "tick_size": tick_size}


Interval = TimeInterval | RangeInterval


def parse(text: str) -> Interval:
    """Parse and normalize. Raises `IntervalError` with code `months` or `invalid`."""
    if not isinstance(text, str):
        raise IntervalError(INVALID)
    value = text.strip()
    if _MONTHS.fullmatch(value):
        raise IntervalError(MONTHS)
    found = _TIME.fullmatch(value)
    if found:
        return _time(int(found.group(1)), found.group(2))
    for pattern, unit in ((_TICKS_SHORT, "t"), (_TICKS_CANON, "t"), (_PRICE_SHORT, "p"), (_PRICE_CANON, "p")):
        found = pattern.fullmatch(value)
        if found:
            return _range(found.group(1), unit)
    raise IntervalError(INVALID)


def _time(n: int, unit: str) -> TimeInterval:
    unit = unit.upper() if unit in ("d", "w") else unit
    if not 1 <= n <= _LIMITS[unit]:
        raise IntervalError(INVALID)
    if unit == "s" and n % 60 == 0:
        n, unit = n // 60, "m"
    if unit == "m" and n % 60 == 0:
        n, unit = n // 60, "h"
    return TimeInterval(n, unit, n * _UNIT_SECONDS[unit], f"{n}{unit}")


def _range(digits: str, unit: str) -> RangeInterval:
    if unit == "t":
        if "." in digits:
            raise IntervalError(INVALID)
        size = int(digits)
        if not 1 <= size <= _MAX_TICKS:
            raise IntervalError(INVALID)
        return RangeInterval(size, "t", f"r:{size}t")
    size = float(digits)
    if not size > 0 or size == float("inf"):
        raise IntervalError(INVALID)
    shown = int(size) if size == int(size) else size
    return RangeInterval(shown, "p", f"r:{shown}p")


def of_seconds(seconds: int) -> TimeInterval:
    """A plain length in seconds, without the limits or the promotion of `parse` (for bases and internal buckets)."""
    return TimeInterval(seconds, "s", seconds, f"{seconds}s")


def parse_time(text: str) -> TimeInterval:
    """Like `parse`, for places that only take time intervals (history, the candles endpoint)."""
    interval = parse(text)
    if not isinstance(interval, TimeInterval):
        raise IntervalError(INVALID)
    return interval


def timeframe_seconds(timeframe: str) -> int:
    """Length in seconds of a canonical time interval. Days and weeks give their nominal length."""
    return parse_time(timeframe).seconds


def visible_bars(timeframe: str) -> int:
    """Bars the snapshot carries: the five original timeframes keep their own count, any other interval gets 300."""
    return VISIBLE_BARS.get(timeframe, DEFAULT_VISIBLE_BARS)
