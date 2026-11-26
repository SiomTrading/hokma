"""The only module of Hokma that generated code can import (SPEC-031 section 5.3, the list of 5.2 point 2).

A rule written by the strategy coder runs in an isolated process that is not the server of Hokma and must not reach the database, a broker or the
desks. So this module is self contained: it re-exports the parameter schema (a pure module), and it defines light copies of the few value types a rule
reads or returns (a context, a view, a criterion, an indicator request, the four units). The parent (hokma.factory.usercode.proxies) turns what comes
back through the pipe into the real types of the factory and validates it again. Nothing here imports hokma.desks, hokma.db or anything that does:
the runtime guard of the process refuses those, and a test loads this module in a clean process to prove it.

What a rule can use is exactly `__all__`. The attribute names of numpy and math that a rule may touch are fixed in static_check.py.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from hokma.factory.params import Param, ParamSchema, validate

__all__ = ["dataclass", "ClassVar", "Param", "ParamSchema", "Criterion", "Signal", "SignalContext", "RuleBarContext", "IndicatorRequest", "fact", "view",
           "clip", "Price", "Ticks", "ATR", "Pct", "INDICATORS", "ind",
           "Decision", "long", "short", "flat", "hold", "replay", "bars_from_closes", "build_strategy"]

# The kinds of hokma.factory.indicator_library.LIBRARY (a test keeps both lists equal). The rule only names them; Hokma computes them in the parent.
INDICATORS: tuple[str, ...] = (
    "accumulation_distribution", "adx", "adx_minus_di", "adx_plus_di", "aroon_down", "aroon_oscillator", "aroon_up", "atr", "awesome_oscillator",
    "balance_of_power", "bollinger_bandwidth", "bollinger_lower", "bollinger_middle", "bollinger_percent_b", "bollinger_upper", "cci",
    "chaikin_money_flow", "chaikin_oscillator", "chande_momentum", "chandelier_exit_long_exit", "chandelier_exit_short_exit", "choppiness",
    "donchian_lower", "donchian_middle", "donchian_upper", "ease_of_movement", "ema", "ewma_vol", "fibonacci_level_0_0", "fibonacci_level_100_0",
    "fibonacci_level_23_6", "fibonacci_level_38_2", "fibonacci_level_50_0", "fibonacci_level_61_8", "fibonacci_level_78_6", "force_index",
    "half_life", "hull", "ichimoku_base", "ichimoku_conversion", "ichimoku_lagging_close", "ichimoku_span_a", "ichimoku_span_b", "kama",
    "keltner_lower", "keltner_middle", "keltner_upper", "macd_histogram", "macd_line", "macd_signal", "mfi", "negative_volume_index", "obv",
    "parabolic_sar_sar", "parabolic_sar_trend", "pivot_points_pivot", "pivot_points_r1", "pivot_points_r2", "pivot_points_r3", "pivot_points_s1",
    "pivot_points_s2", "pivot_points_s3", "positive_volume_index", "ppo", "price_volume_trend", "roc", "rsi", "sma", "stochastic_d", "stochastic_k",
    "stochastic_rsi_d", "stochastic_rsi_k", "supertrend", "supertrend_direction", "support_resistance_resistance", "support_resistance_support",
    "true_range", "true_strength_index_signal", "true_strength_index_tsi", "ultimate_oscillator", "volatility_cone_realised", "vortex_minus",
    "vortex_plus", "vwma", "williams_r", "wma",
)
MAX_REASON = 1000
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
# A copy of hokma.desks.core.signal.EXECUTION_TERMS (this module cannot import the desks): a view speaks of what it sees, never of sizing, exits or timing.
# tests/usercode/test_user_api_contract.py keeps both patterns equal, so a rule learns of a refusal of the real Signal in its own test and not in the parent.
_EXECUTION_TERMS = re.compile(
    r"\b(?:position|trade|order|lot|bet)\s+siz\w*|\bsizing\b|\bsize\s+(?:the|this|your)\s+(?:position|trade|bet)"
    r"|\bstops?\b|\bstop[- ]?loss\b|\btake[- ]profit\b|\bprofit\s+target\b|\b(?:price\s+target|target\s+price)\b"
    r"|\bentry\s+(?:point|price|timing|level|zone|trigger)\b|\benter\s+(?:now|at|on|when)\b"
    r"|\b(?:buy|sell)\s+(?:now|at)\b|\bhow\s+many\s+(?:shares|contracts|lots)\b"
    r"|tama(?:n|\u00f1)o\s+de\s+(?:la\s+)?posici(?:o|\u00f3)n|\bprecio\s+objetivo\b|\bobjetivo\s+de\s+precio\b"
    r"|\bentrar\s+(?:ahora|en|a)\b",
    re.IGNORECASE,
)


def _no_execution_terms(label: str, text: str) -> None:
    found = _EXECUTION_TERMS.search((text or "").replace("_", " "))
    if found:
        raise ValueError(f"{label} speaks of sizing, exits or timing ({found.group(0)!r}); a rule gives a view only")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


def _finite(label: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    return float(value)


def clip(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


@dataclass(frozen=True)
class Criterion:
    """One number the rule computed, with the threshold it was compared to."""

    name: str
    value: float | None
    threshold: float | None
    passed: bool | None
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or len(self.name) > 80:
            raise ValueError("a criterion needs a name of up to 80 characters")
        if self.value is not None:
            _finite(f"criterion {self.name}: value", self.value)
        if self.threshold is not None:
            _finite(f"criterion {self.name}: threshold", self.threshold)
        if self.value is None and self.passed is not None:
            raise ValueError(f"criterion {self.name}: passed must be None when value is None")
        _no_execution_terms(f"criterion {self.name}", f"{self.name} {self.source}")
        for field_name in ("value", "threshold"):                  # numpy numbers travel as plain floats: the pipe is JSON
            number = getattr(self, field_name)
            if number is not None:
                object.__setattr__(self, field_name, float(number))

    def to_data(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "threshold": self.threshold, "passed": self.passed, "source": self.source}


@dataclass(frozen=True)
class Signal:
    """The answer of an alpha as data: a view with its value, or an abstention with its reason. The parent builds the real Signal."""

    source: str
    value: float | None
    abstained: bool
    reason: str
    criteria: tuple[Criterion, ...] = ()
    missing: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, text in (("reason", self.reason), ("source", self.source), ("missing", " ".join(self.missing))):
            _no_execution_terms(label, text)


@dataclass(frozen=True)
class SignalContext:
    """What an alpha receives: who asks, as of when, and the facts as lists of numbers (copies: a rule cannot reach anything else)."""

    source: str
    symbol: str
    as_of: str
    facts: Mapping[str, Any]

    def abstain(self, reason: str, missing: Iterable[str] = ()) -> Signal:
        return Signal(source=self.source, value=None, abstained=True, reason=_reason(reason), missing=tuple(str(m) for m in missing))


@dataclass(frozen=True)
class RuleBarContext:
    """What a filter or an exit receives: the series it asked for (the tail the factory already revealed), the position side and the equity."""

    as_of: str
    symbol: str
    data: Mapping[str, Any]
    position_side: str | None = None
    equity: float | None = None

    def series(self, name: str) -> np.ndarray:
        if not isinstance(name, str) or name not in self.data:
            raise ValueError(f"series {name!r} was not requested; available: {', '.join(sorted(self.data)) or 'none'}")
        return np.asarray(self.data[name], dtype=float)


def _reason(reason: Any) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a reason is a sentence of text")
    return reason[:MAX_REASON]


def fact(ctx: SignalContext, name: str, minimum: int = 1) -> np.ndarray | None:
    """A numeric array of facts, or None when it is missing, too short or holds a NaN in the part that is read."""
    raw = ctx.facts.get(name)
    if raw is None:
        return None
    try:
        array = np.asarray(raw, dtype=float)
    except (TypeError, ValueError):
        return None
    if array.ndim != 1 or len(array) < minimum or not np.all(np.isfinite(array[-minimum:])):
        return None
    return array


def view(ctx: SignalContext, source: str, value: float, criteria: Sequence[Criterion], reason: str) -> Signal:
    """A view with the value clipped to [-1, +1]. A value that is not a finite number is an error of the rule, never a silent zero."""
    number = _finite("the value of a view", value)
    if not all(isinstance(c, Criterion) for c in criteria):
        raise ValueError("the criteria of a view are Criterion objects")
    return Signal(source=str(source)[:80], value=clip(number), abstained=False, reason=_reason(reason), criteria=tuple(criteria))


@dataclass(frozen=True)
class _Unit:
    value: float

    TYPE: ClassVar[str] = ""

    def __post_init__(self) -> None:
        number = _finite(f"a {self.TYPE} distance", self.value)
        if number <= 0:
            raise ValueError(f"a {self.TYPE} distance must be above zero, got {self.value!r}")
        if self.TYPE == "pct" and number >= 100:                   # the real Pct: 1 means 1 percent and a distance of 100 percent or more is no stop
            raise ValueError(f"a pct distance must be below 100, got {self.value!r}")
        object.__setattr__(self, "value", number)                    # a plain float: the pipe is JSON and the real unit takes plain numbers

    def to_data(self) -> dict[str, Any]:
        return {"type": self.TYPE, "value": self.value}


@dataclass(frozen=True)
class Price(_Unit):
    """An absolute price level."""

    TYPE: ClassVar[str] = "price"


@dataclass(frozen=True)
class Ticks(_Unit):
    """A distance in ticks of the contract."""

    TYPE: ClassVar[str] = "ticks"


@dataclass(frozen=True)
class ATR(_Unit):
    """A distance in multiples of the average true range."""

    TYPE: ClassVar[str] = "atr"


@dataclass(frozen=True)
class Pct(_Unit):
    """A distance in percent of the entry price."""

    TYPE: ClassVar[str] = "pct"


@dataclass(frozen=True)
class IndicatorRequest:
    """An indicator the rule reads. Hokma computes it in the parent from the library and hands the numbers over as a fact."""

    name: str
    kind: str
    source: str = "close"
    params: tuple[tuple[str, int], ...] = ()
    warmup: int = 1

    def __post_init__(self) -> None:
        if self.kind not in INDICATORS:
            raise ValueError(f"indicator {self.kind!r} is not in the library; allowed: {', '.join(INDICATORS)}")
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            raise ValueError("an indicator name is lower case letters, digits and underscores")
        if not isinstance(self.source, str) or not _FIELD.match(self.source):
            raise ValueError("an indicator source is the name of a bar field")
        if isinstance(self.warmup, bool) or not isinstance(self.warmup, int) or not 1 <= self.warmup <= 5000:
            raise ValueError("the warmup of an indicator is a whole number from 1 to 5000")
        if len(self.params) > 8 or not all(isinstance(p, tuple) and len(p) == 2 and isinstance(p[0], str) and isinstance(p[1], int)
                                           and not isinstance(p[1], bool) for p in self.params):
            raise ValueError("the params of an indicator are up to 8 pairs of a name and a whole number")

    def to_data(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "source": self.source, "params": [[k, v] for k, v in self.params], "warmup": self.warmup}


def _window(x: Any, length: int) -> tuple[np.ndarray, int]:
    array = np.asarray(x, dtype=float)
    if array.ndim != 1:
        raise ValueError("a series is a one dimensional array")
    if isinstance(length, bool) or not isinstance(length, int) or length < 1:
        raise ValueError("a window is a whole number of at least 1")
    return array, length


class Indicators:
    """Small causal helpers for the tail of data a rule already received. They are not the library of Hokma: a rule that needs one of those names it
    in `requests`. Each value uses only the bars up to its own, and the first `length - 1` values are NaN."""

    @staticmethod
    def sma(x: Any, length: int) -> np.ndarray:
        array, n = _window(x, length)
        out = np.full(len(array), np.nan)
        if len(array) >= n:
            out[n - 1:] = np.lib.stride_tricks.sliding_window_view(array, n).mean(axis=1)
        return out

    @staticmethod
    def ema(x: Any, length: int) -> np.ndarray:
        array, n = _window(x, length)
        out = np.full(len(array), np.nan)
        if len(array) >= n:
            alpha = 2.0 / (n + 1.0)
            out[n - 1] = float(np.mean(array[:n]))
            for i in range(n, len(array)):
                out[i] = alpha * array[i] + (1.0 - alpha) * out[i - 1]
        return out

    @staticmethod
    def highest(x: Any, length: int) -> np.ndarray:
        array, n = _window(x, length)
        out = np.full(len(array), np.nan)
        if len(array) >= n:
            out[n - 1:] = np.lib.stride_tricks.sliding_window_view(array, n).max(axis=1)
        return out

    @staticmethod
    def lowest(x: Any, length: int) -> np.ndarray:
        array, n = _window(x, length)
        out = np.full(len(array), np.nan)
        if len(array) >= n:
            out[n - 1:] = np.lib.stride_tricks.sliding_window_view(array, n).min(axis=1)
        return out


ind = Indicators()


# ---------------------------------------------------------------------------------------------------- the strategy as code (SPEC-031 A2)
ACTIONS = ("long", "short", "flat", "hold")
BAR_FIELDS = ("open", "high", "low", "close", "volume")
BAR_KINDS = (
    "accumulation_distribution", "adx", "adx_minus_di", "adx_plus_di", "aroon_down", "aroon_oscillator", "aroon_up", "atr", "awesome_oscillator",
    "balance_of_power", "cci", "chaikin_money_flow", "chaikin_oscillator", "chandelier_exit_long_exit", "chandelier_exit_short_exit", "choppiness",
    "donchian_lower", "donchian_middle", "donchian_upper", "ease_of_movement", "fibonacci_level_0_0", "fibonacci_level_100_0",
    "fibonacci_level_23_6", "fibonacci_level_38_2", "fibonacci_level_50_0", "fibonacci_level_61_8", "fibonacci_level_78_6", "force_index",
    "ichimoku_base", "ichimoku_conversion", "ichimoku_lagging_close", "ichimoku_span_a", "ichimoku_span_b", "keltner_lower", "keltner_middle",
    "keltner_upper", "mfi", "negative_volume_index", "obv", "parabolic_sar_sar", "parabolic_sar_trend", "pivot_points_pivot", "pivot_points_r1",
    "pivot_points_r2", "pivot_points_r3", "pivot_points_s1", "pivot_points_s2", "pivot_points_s3", "positive_volume_index", "price_volume_trend",
    "stochastic_d", "stochastic_k", "supertrend", "supertrend_direction", "true_range", "ultimate_oscillator", "vortex_minus", "vortex_plus", "vwma",
    "williams_r",
)          # the kinds that read several bar fields (a test keeps this equal to the library)
MAX_INDICATORS = 8
MAX_WARMUP = 5000


@dataclass(frozen=True)
class Decision:
    """What a strategy answers on a closed bar: enter long or short (with a stop and a target if it wants them), leave, or do nothing."""

    action: str
    stop: Any = None
    target: Any = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"a decision is one of {', '.join(ACTIONS)}, got {self.action!r}")
        for label, unit in (("stop", self.stop), ("target", self.target)):
            if unit is not None and not isinstance(unit, (Price, Ticks, ATR, Pct)):
                raise ValueError(f"the {label} needs an explicit unit (Price, Ticks, ATR or Pct), got {unit!r}")
        if self.action in ("flat", "hold") and (self.stop is not None or self.target is not None):
            raise ValueError("only an entry carries a stop or a target")
        if not isinstance(self.reason, str) or len(self.reason) > MAX_REASON:
            raise ValueError(f"the reason is text of up to {MAX_REASON} characters")


def long(stop: Any = None, target: Any = None, reason: str = "") -> Decision:
    """Be long: enter when flat, reverse when short, nothing when already long."""
    return Decision("long", stop, target, reason)


def short(stop: Any = None, target: Any = None, reason: str = "") -> Decision:
    """Be short: enter when flat, reverse when long, nothing when already short."""
    return Decision("short", stop, target, reason)


def flat(reason: str = "") -> Decision:
    """Leave the position, if there is one."""
    return Decision("flat", None, None, reason)


def hold() -> Decision:
    """Do nothing on this bar."""
    return Decision("hold")


class StrategyInit:
    """What init receives: it asks for indicators of the library and for a longer warm-up. It cannot see a bar."""

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        self.params = dict(params or {})
        self._requests: list[IndicatorRequest] = []
        self._extra = 0

    def indicator(self, name: str, kind: str, source: str = "close", warmup: int | None = None, **params: int) -> None:
        if not isinstance(name, str) or name in BAR_FIELDS or name.startswith("sizer") or any(r.name == name for r in self._requests):
            raise ValueError(f"the indicator name {name!r} is a bar field, starts with sizer or is used twice")
        if len(self._requests) >= MAX_INDICATORS:
            raise ValueError(f"a strategy asks for at most {MAX_INDICATORS} indicators")
        if kind in BAR_KINDS:
            source = "bars"
        ints = [v for v in params.values() if isinstance(v, int) and not isinstance(v, bool)]
        bars_needed = warmup if warmup is not None else max(ints or [1]) + 1
        self._requests.append(IndicatorRequest(name, kind, source, tuple(sorted(params.items())), bars_needed))

    def set_warmup(self, bars: int) -> None:
        if isinstance(bars, bool) or not isinstance(bars, int) or not 0 <= bars <= MAX_WARMUP:
            raise ValueError(f"a warm-up is a whole number of bars from 0 to {MAX_WARMUP}")
        self._extra = bars

    @property
    def requests(self) -> tuple[IndicatorRequest, ...]:
        return tuple(self._requests)

    @property
    def extra_warmup(self) -> int:
        return self._extra

    @property
    def warmup(self) -> int:
        return max([self._extra, *(r.warmup for r in self._requests)])


class StrategyContext:
    """What on_bar receives: the closed bars up to now as read only arrays (index 0 the oldest, -1 the last closed one), the indicators it asked for,
    its parameters and the side it holds. Nothing in it reaches a bar that has not closed, a broker or the account."""

    __slots__ = ("index", "as_of", "symbol", "params", "position_side", "equity", "_series")

    def __init__(self, *, index: int, as_of: str, symbol: str, params: Mapping[str, Any], position_side: str | None, equity: float,
                 series: Mapping[str, Any]) -> None:
        self.index, self.as_of, self.symbol, self.params = index, as_of, symbol, dict(params)
        self.position_side, self.equity, self._series = position_side, float(equity), dict(series)

    def series(self, name: str) -> np.ndarray:
        try:
            return self._series[name]
        except KeyError:
            raise ValueError(f"series {name!r} is not a bar field or an indicator of this strategy; available: {', '.join(sorted(self._series))}") from None

    @property
    def open(self) -> np.ndarray:
        return self._series["open"]

    @property
    def high(self) -> np.ndarray:
        return self._series["high"]

    @property
    def low(self) -> np.ndarray:
        return self._series["low"]

    @property
    def close(self) -> np.ndarray:
        return self._series["close"]

    @property
    def volume(self) -> np.ndarray:
        return self._series["volume"]


def bars_from_closes(closes: Sequence[float], pad: float = 0.5, volume: float = 1000.0) -> dict[str, list[float]]:
    """Bars for a test from a list of closes: each bar opens at the previous close and its high and low are the pad beyond the open and the close."""
    values = [float(x) for x in closes]
    if not values:
        raise ValueError("a test needs at least one close")
    opens = [values[0]] + values[:-1]
    return {"open": opens, "high": [max(o, c) + pad for o, c in zip(opens, values)], "low": [min(o, c) - pad for o, c in zip(opens, values)],
            "close": values, "volume": [float(volume)] * len(values)}


def build_strategy(cls: Any, params: Mapping[str, Any] | None = None) -> Any:
    """An instance of the strategy class with its parameters checked against its SCHEMA (the defaults fill what is not given)."""
    schema = getattr(cls, "SCHEMA", None)
    if not isinstance(schema, ParamSchema):
        raise ValueError("the strategy class declares no SCHEMA")
    return cls(**validate(schema, params))


def replay(strategy: Any, bars: Mapping[str, Any], params: Mapping[str, Any] | None = None, position_side: str | None = None) -> list[Decision]:
    """The decision of a strategy on every bar, as the backtest asks for it: init once, then on_bar on each closed bar after the warm-up (before it the answer
    is hold). `strategy` is a class (built with `params`) or an instance. `position_side` is the side the strategy is told it holds. The work is done by
    `hokma.factory.strategy_replay`, which the process that runs the tests loads; this module itself names nothing of Hokma but its parameter schema."""
    import sys

    engine = sys.modules.get("hokma.factory.strategy_replay")
    if engine is None:
        raise ValueError("replay is available where the tests of a strategy run")
    return engine.replay(strategy, bars, params, position_side)
