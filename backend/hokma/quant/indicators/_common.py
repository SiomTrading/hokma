"""Shared plumbing of the indicators module: bar validation, closed-bar gate, result building and the indicator decorator.

Bad data never raises: it returns a QuantResult with value None and the reason in warnings.
BadInput is raised only for a wrong type, a missing column or an unknown argument.
"""
from __future__ import annotations

import functools
import math
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from hokma.quant import helpers, limits, registry
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult

NEW_YORK = ZoneInfo("America/New_York")
SESSION_CLOSE_HOUR = 16          # a daily bar is closed once 16:00 New York of its date has passed (early closes wait longer: conservative)
LINES = ("DT", "SWC", "SWO", "LP")
LINE_SCALE = {"DT": 0.5, "SWC": 1.0, "SWO": 1.0, "LP": 2.0}   # DT short windows, SWC and SWO daily windows, LP long windows
DEFAULT_LAST_N = 60


# ---------------------------------------------------------------------------------------------- result building
def jsonable(value: Any) -> Any:
    """numpy and pandas containers to plain python; non finite floats to None."""
    if isinstance(value, (pd.Series, pd.Index)):
        return [jsonable(v) for v in value.tolist()]
    if isinstance(value, pd.DataFrame):
        return {str(c): jsonable(value[c]) for c in value.columns}
    if isinstance(value, np.ndarray):
        return jsonable(value.tolist())
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    return value


def empty(name: str, unit: str, method: str, warnings: tuple[str, ...] | str, *, params: dict | None = None,
          piece_ids: tuple[str, ...] = ()) -> QuantResult:
    if isinstance(warnings, str):
        warnings = (warnings,)
    return QuantResult(name=name, value=None, unit=unit, method=method, params=jsonable(dict(params or {})),
                       warnings=tuple(warnings), piece_ids=tuple(piece_ids))


def build(name: str, value: Any, *, unit: str, method: str, params: dict, piece_ids: tuple[str, ...], inputs: tuple[Any, ...],
          as_of_source: pd.Series | pd.DataFrame | None = None, warnings: tuple[str, ...] = ()) -> QuantResult:
    clean_value = jsonable(value)
    if clean_value is None or (isinstance(clean_value, dict) and not clean_value):
        return empty(name, unit, method, warnings + ("result is not finite",), params=params, piece_ids=piece_ids)
    as_of = None
    if as_of_source is not None and len(as_of_source) and isinstance(as_of_source.index, pd.DatetimeIndex):
        as_of = as_of_source.index[-1].to_pydatetime()
    return QuantResult(name=name, value=clean_value, unit=unit, method=method, params=jsonable(dict(params)),
                       inputs_hash=helpers.hash_inputs(*inputs, jsonable(dict(params))), as_of=as_of,
                       warnings=tuple(warnings), piece_ids=tuple(piece_ids))


# ---------------------------------------------------------------------------------------------- bar validation
PRICE_COLUMNS = ("open", "high", "low", "close")


def bars_frame(bars: Any, needed: tuple[str, ...], *, min_bars: int = 1, label: str = "bars") -> tuple[pd.DataFrame | None, tuple[str, ...]]:
    """The needed columns as floats, or (None, reason). A wrong type or a missing column raises BadInput; bad values do not."""
    if not isinstance(bars, pd.DataFrame):
        raise BadInput(f"{label}: expected a DataFrame, got {type(bars).__name__}")
    lower = {str(c).lower(): c for c in bars.columns}
    missing = [n for n in needed if n not in lower]
    if missing:
        raise BadInput(f"{label}: missing column(s) {missing}")
    try:
        frame = pd.DataFrame({n: bars[lower[n]].astype(float) for n in needed}, index=bars.index)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"{label}: values are not numeric") from exc
    if frame.empty:
        return None, (f"{label}: no bars",)
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        return None, (f"{label}: the index must be increasing and unique",)
    raw = frame.to_numpy()
    if not np.isfinite(raw).all():
        bad = [str(c) for c in frame.columns[~np.isfinite(frame.to_numpy()).all(axis=0)]]
        return None, (f"{label}: NaN or infinite values in {bad}",)
    prices = [c for c in PRICE_COLUMNS if c in needed]
    if prices and (frame[prices] <= 0).any().any():
        return None, (f"{label}: prices must be positive",)
    if "volume" in needed and (frame["volume"] < 0).any():
        return None, (f"{label}: volume cannot be negative",)
    if "high" in needed and "low" in needed:
        if (frame["high"] < frame["low"]).any():
            return None, (f"{label}: bars are inconsistent: high below low",)
        top = [c for c in ("open", "close") if c in needed]
        if top and ((frame["high"] < frame[top].max(axis=1)).any() or (frame["low"] > frame[top].min(axis=1)).any()):
            return None, (f"{label}: bars are inconsistent: high below open or close, or low above them",)
    if len(frame) < min_bars:
        return None, (f"{label}: need at least {min_bars} bar(s), got {len(frame)}",)
    return frame, ()


def panel_frame(panel: Any, *, label: str = "closes", min_rows: int = 2, min_cols: int = 1) -> tuple[pd.DataFrame | None, tuple[str, ...]]:
    """A numeric panel (rows = dates, columns = symbols). NaN is allowed only as missing history, not as an infinite value."""
    if not isinstance(panel, pd.DataFrame):
        raise BadInput(f"{label}: expected a DataFrame, got {type(panel).__name__}")
    try:
        frame = panel.astype(float)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"{label}: values are not numeric") from exc
    if frame.empty:
        return None, (f"{label}: no data",)
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        return None, (f"{label}: the index must be increasing and unique",)
    if np.isinf(frame.to_numpy()).any() or (frame <= 0).any().any():
        return None, (f"{label}: values must be positive and finite",)
    if frame.shape[1] < min_cols:
        return None, (f"{label}: need at least {min_cols} column(s), got {frame.shape[1]}",)
    if frame.isna().all(axis=None):
        return None, (f"{label}: all values are NaN",)
    if len(frame) < min_rows:
        return None, (f"{label}: need at least {min_rows} row(s), got {len(frame)}",)
    return frame, ()


# ---------------------------------------------------------------------------------------------- closed-bar gate
def _as_of_stamp(as_of: Any) -> pd.Timestamp:
    """A date or a date string means the start of that day in New York: that day's bar is not closed yet."""
    stamp = pd.Timestamp(as_of)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(NEW_YORK)
    return stamp.tz_convert(NEW_YORK)


def session_close(label: Any) -> pd.Timestamp:
    """The instant a daily bar with this date label is closed."""
    day = pd.Timestamp(label).normalize().tz_localize(None)
    return (day + pd.Timedelta(hours=SESSION_CLOSE_HOUR)).tz_localize(NEW_YORK)


def closed_bars(frame: pd.DataFrame, as_of: Any = None, *, closed_flag: pd.Series | None = None) -> tuple[pd.DataFrame, int, tuple[str, ...]]:
    """Daily bars that are closed at as_of: bar date at 16:00 New York has passed. Returns (bars, dropped, warnings).

    Without as_of the bars are taken as closed and a warning says so; a boolean closed column (when given) always applies.
    """
    dropped = 0
    work = frame
    if closed_flag is not None:
        keep = closed_flag.reindex(work.index).fillna(False).astype(bool)
        dropped += int((~keep).sum())
        work = work[keep]
    if as_of is None:
        return work, dropped, ("as_of not given: every bar was taken as closed",)
    limit = _as_of_stamp(as_of)
    keep = np.array([session_close(i) <= limit for i in work.index], dtype=bool)
    dropped += int((~keep).sum())
    return work[keep], dropped, ()


def to_weekly(frame: pd.DataFrame, *, last_friday_close: bool = True) -> pd.DataFrame:
    """Daily bars to weekly bars labelled by the Friday of their week (open first, high max, low min, close last, volume sum)."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    use = {k: v for k, v in agg.items() if k in frame.columns}
    work = frame.copy()
    work.index = pd.DatetimeIndex(work.index).tz_localize(None).normalize()
    weekly = work.resample("W-FRI").agg(use)
    weekly = weekly[work["close"].resample("W-FRI").count() > 0]
    return weekly


def closed_weekly(weekly: pd.DataFrame, as_of: Any) -> tuple[pd.DataFrame, int]:
    """Weekly bars whose Friday 16:00 New York has passed at as_of (the label is the Friday of the week)."""
    if as_of is None:
        return weekly, 0
    limit = _as_of_stamp(as_of)
    keep = np.array([session_close(i) <= limit for i in weekly.index], dtype=bool)
    return weekly[keep], int((~keep).sum())


def sessions_between(start: Any, end: Any) -> int:
    """Business days from start to end (0 when they are the same day)."""
    a, b = pd.Timestamp(start).normalize().tz_localize(None), pd.Timestamp(end).normalize().tz_localize(None)
    if b < a:
        return -sessions_between(end, start)
    return int(np.busday_count(a.date(), b.date()))


# ---------------------------------------------------------------------------------------------- fills
def entry_fill(next_open: float, tick: float) -> float:
    """Entry price of a signal read at a close: the next open plus one tick (exact decimal arithmetic)."""
    if not (math.isfinite(next_open) and math.isfinite(tick)) or next_open <= 0 or tick < 0:
        raise ValueError("next_open must be positive and tick non negative")
    total = Decimal(str(next_open)) + Decimal(str(tick))
    return float(total.quantize(Decimal("0.0000001"), rounding=ROUND_HALF_UP).normalize())


def stop_first(next_low: float, stop: float, entry: float) -> bool:
    """When a bar both fills the entry and touches the stop the stop is taken first (pessimistic order inside one bar)."""
    return next_low <= stop <= entry


# ---------------------------------------------------------------------------------------------- the indicator decorator
def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "list"


def resolve_params(line: str | None, defaults: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    """Defaults, scaled by the line when the caller did not pass the window, then the caller's values (type checked)."""
    key = "SWO" if line is None else str(line).upper()
    if key not in LINE_SCALE:
        raise BadInput(f"line must be one of {list(LINE_SCALE)}, got {line!r}")
    unknown = set(given) - set(defaults)
    if unknown:
        raise BadInput(f"unknown argument(s) {sorted(unknown)}")
    resolved: dict[str, Any] = {}
    for name, default in defaults.items():
        if name in given and given[name] is not None:
            value = given[name]
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    raise BadInput(f"{name} must be true or false")
            elif isinstance(value, bool):
                raise BadInput(f"{name} must not be a boolean")
            elif isinstance(default, int):
                value = limits.as_int(name, value)
            elif isinstance(default, float):
                if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
                    raise BadInput(f"{name} must be a number, got {type(value).__name__}")
                value = float(value)
            elif isinstance(default, str) and not isinstance(value, str):
                raise BadInput(f"{name} must be a string")
            resolved[name] = value
        elif isinstance(default, int) and not isinstance(default, bool) and "window" in name:
            resolved[name] = max(2, int(math.floor(default * LINE_SCALE[key] + 0.5))) if default >= 2 else default
        else:
            resolved[name] = default
    return resolved


def scale_window(base: int, line: str | None) -> int:
    """A base window of the SWO line scaled for another line (DT short, LP long), never below 2."""
    key = "SWO" if line is None else str(line).upper()
    if key not in LINE_SCALE:
        raise BadInput(f"line must be one of {list(LINE_SCALE)}, got {line!r}")
    return max(2, int(math.floor(base * LINE_SCALE[key] + 0.5)))


def domain_problem(resolved: dict[str, Any]) -> str | None:
    """A reason when a numeric parameter is not a positive finite number."""
    for name, value in resolved.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and (not math.isfinite(float(value)) or value <= 0):
            return f"{name} must be a positive number, got {value}"
    return None


def series_value(out: pd.Series | pd.DataFrame, name: str, last_n: int | None) -> dict | None:
    """The latest row and the last_n rows of a Series or DataFrame as plain python. None when the latest row is undefined."""
    frame = out.to_frame(name) if isinstance(out, pd.Series) else out.copy()
    frame.columns = [str(c) for c in frame.columns]
    frame = frame.replace([np.inf, -np.inf], np.nan)
    if frame.empty or frame.iloc[-1].isna().all():
        return None
    count = len(frame) if last_n is None else max(1, min(len(frame), last_n))
    tail = frame.iloc[-count:]
    return {"latest": {c: frame.iloc[-1][c] for c in frame.columns}, "index": [str(i) for i in tail.index],
            "values": {c: tail[c].to_numpy() for c in frame.columns}}


def check_last_n(last_n: Any) -> int | None:
    if last_n is None:
        return None
    n = limits.as_int("last_n", last_n)
    limits.check_param("last_n", n)
    if n < 1:
        raise BadInput("last_n must be at least 1")
    return n


def indicator(name: str, pieces: tuple[str, ...], needed: tuple[str, ...], *, summary: str, unit: str = "value",
              defaults: dict[str, Any] | None = None, min_bars: int = 2, method: str | None = None) -> Callable:
    """Register "indicators.<name>" over a function frame -> Series or DataFrame; the wrapper validates bars, applies the line scale
    to window parameters and returns the latest row plus the last_n rows."""
    base = dict(defaults or {})
    full = f"indicators.{name}"
    inputs: dict[str, Any] = {"bars": {"type": "dataframe", "required": True, "columns": list(needed)},
                              "line": {"type": "str", "required": False, "default": "SWO"},
                              "last_n": {"type": "int", "required": False, "default": DEFAULT_LAST_N}}
    for key, value in base.items():
        inputs[key] = {"type": _type_name(value), "required": False, "default": value}

    def deco(fn: Callable) -> Callable:
        @registry.register(full, module="indicators", summary=summary, inputs=inputs, piece_ids=pieces)
        @functools.wraps(fn)
        def wrapper(bars: Any, line: str = "SWO", last_n: int | None = DEFAULT_LAST_N, **params: Any) -> QuantResult:
            how = method or name
            resolved = resolve_params(line, base, params)
            keep = check_last_n(last_n)
            report = {**resolved, "line": str(line).upper()}
            problem = domain_problem(resolved)
            if problem:
                return empty(full, unit, how, problem, params=report, piece_ids=pieces)
            frame, warns = bars_frame(bars, needed, min_bars=min_bars)
            if frame is None:
                return empty(full, unit, how, warns, params=report, piece_ids=pieces)
            try:
                out = fn(frame, **resolved)
            except BadInput:
                raise
            except Exception as exc:  # a library failure on odd data is a data problem, not a crash
                return empty(full, unit, how, (f"{type(exc).__name__}: {exc}",), params=report, piece_ids=pieces)
            value = series_value(out, name, keep)
            if value is None:
                return empty(full, unit, how, "latest value is undefined: not enough bars for the window or a zero denominator",
                             params=report, piece_ids=pieces)
            return build(full, value, unit=unit, method=how, params=report, piece_ids=pieces, inputs=(frame,), as_of_source=frame)

        return wrapper

    return deco


def panel_indicator(name: str, pieces: tuple[str, ...], *, summary: str, with_volumes: bool = False, unit: str = "value",
                    defaults: dict[str, Any] | None = None, min_rows: int = 2, method: str | None = None) -> Callable:
    """Same as indicator() for functions over a panel of closes (rows = dates, columns = symbols) and, optionally, a panel of volumes."""
    base = dict(defaults or {})
    full = f"indicators.{name}"
    inputs: dict[str, Any] = {"closes": {"type": "dataframe", "required": True}}
    if with_volumes:
        inputs["volumes"] = {"type": "dataframe", "required": True}
    inputs["line"] = {"type": "str", "required": False, "default": "SWO"}
    inputs["last_n"] = {"type": "int", "required": False, "default": DEFAULT_LAST_N}
    for key, value in base.items():
        inputs[key] = {"type": _type_name(value), "required": False, "default": value}

    def deco(fn: Callable) -> Callable:
        def run(closes: Any, volumes: Any, line: str, last_n: int | None, params: dict[str, Any]) -> QuantResult:
            how = method or name
            resolved = resolve_params(line, base, params)
            keep = check_last_n(last_n)
            report = {**resolved, "line": str(line).upper()}
            problem = domain_problem(resolved)
            if problem:
                return empty(full, unit, how, problem, params=report, piece_ids=pieces)
            frame, warns = panel_frame(closes, label="closes", min_rows=min_rows)
            if frame is None:
                return empty(full, unit, how, warns, params=report, piece_ids=pieces)
            vols = None
            if with_volumes:
                vols, vwarns = panel_frame(volumes, label="volumes", min_rows=min_rows)
                if vols is None:
                    return empty(full, unit, how, vwarns, params=report, piece_ids=pieces)
                if vols.shape != frame.shape or not vols.index.equals(frame.index) or list(vols.columns) != list(frame.columns):
                    raise BadInput("volumes must have the same index and columns as closes")
                if (vols < 0).any().any():
                    return empty(full, unit, how, "volumes cannot be negative", params=report, piece_ids=pieces)
            try:
                out = fn(frame, vols, **resolved) if with_volumes else fn(frame, **resolved)
            except BadInput:
                raise
            except Exception as exc:
                return empty(full, unit, how, (f"{type(exc).__name__}: {exc}",), params=report, piece_ids=pieces)
            value = series_value(out, name, keep)
            if value is None:
                return empty(full, unit, how, "latest value is undefined: not enough rows for the window or a zero denominator",
                             params=report, piece_ids=pieces)
            return build(full, value, unit=unit, method=how, params=report, piece_ids=pieces,
                         inputs=(frame,) if vols is None else (frame, vols), as_of_source=frame)

        if with_volumes:
            @registry.register(full, module="indicators", summary=summary, inputs=inputs, piece_ids=pieces)
            @functools.wraps(fn)
            def wrapper(closes: Any, volumes: Any, line: str = "SWO", last_n: int | None = DEFAULT_LAST_N, **params: Any) -> QuantResult:
                return run(closes, volumes, line, last_n, params)
        else:
            @registry.register(full, module="indicators", summary=summary, inputs=inputs, piece_ids=pieces)
            @functools.wraps(fn)
            def wrapper(closes: Any, line: str = "SWO", last_n: int | None = DEFAULT_LAST_N, **params: Any) -> QuantResult:
                return run(closes, None, line, last_n, params)
        return wrapper

    return deco
