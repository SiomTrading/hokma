"""Event study of the stats module (AH-57, AH-58, AH-59, AH-60, FT-089) and the two event skills built on it (AFS-027, CTS-037).

One single event study function serves every desk (PEAD after results, macro announcements, opening events): `event_study`.
The statistics (`event_stats`), the result types and the single file HTML chart live here too.

Timing rule: the reaction day is the first trading day that can hold the news. A report before the open (BMO) or during the
session reacts on its own date; a report after the close (AMC) reacts on the next trading day. An event without a timing is
skipped unless a default timing is given, because measuring from the wrong day misdates the whole window.
"""
from __future__ import annotations

import html
import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import limits, registry
from hokma.quant.errors import BadInput
from hokma.quant.stats import _common as c
from hokma.quant.stats import _econ as econ
from hokma.quant.stats import _lib
from hokma.quant.types import QuantResult

_SERIES = {"type": "series", "required": True}
_EVENTS = {"type": "table or list of {date, timing, event_id}", "required": True}
TIMINGS = ("BMO", "AMC", "intraday")
MODELS = ("market", "mean_adjusted", "market_adjusted")
MAX_EVENTS = 5000


# ---------------------------------------------------------------- types (AH-59)

@dataclass(frozen=True)
class EventRecord:
    """The abnormal returns of one event."""

    event_id: str
    event_date: str
    reaction_date: str
    timing: str
    alpha: float | None
    beta: float | None
    sigma: float
    n_estimation: int
    ar: tuple[float, ...]
    car: float
    t_car: float | None

    def to_dict(self) -> dict[str, Any]:
        return c.jsonable(asdict(self))


@dataclass(frozen=True)
class EventStudyResult:
    """Result of an event study: per event records and the cross sectional aggregates."""

    model: str
    estimation_window: tuple[int, int]
    event_window: tuple[int, int]
    relative_days: tuple[int, ...]
    n_events: int
    n_skipped: int
    records: tuple[EventRecord, ...]
    skipped: tuple[tuple[str, str], ...]
    aar: tuple[float, ...]
    aar_t: tuple[float | None, ...]
    caar: tuple[float, ...]
    car_mean: float
    car_std: float | None
    car_t: float | None
    car_p_value: float | None
    patell_z: float | None
    share_positive: float
    blocking_window: tuple[int, int] | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return c.jsonable(asdict(self))


@dataclass(frozen=True)
class EventStats:
    """t statistic and seeded bootstrap interval of a set of event abnormal returns."""

    n: int
    mean: float
    median: float
    std: float
    t_stat: float | None
    p_value: float | None
    ci_low: float
    ci_high: float
    ci_level: float
    n_boot: int
    seed: int
    share_positive: float
    threshold_t: float | None = None
    passes_threshold: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return c.jsonable(asdict(self))


# ---------------------------------------------------------------- event table

def _events_frame(events: Any) -> pd.DataFrame:
    if isinstance(events, pd.DataFrame):
        frame = events.copy()
    elif isinstance(events, (list, tuple)):
        if len(events) == 0:
            return pd.DataFrame({"date": [], "timing": [], "event_id": []})
        rows = []
        for item in events:
            if isinstance(item, dict):
                rows.append(dict(item))
            elif isinstance(item, (str, pd.Timestamp)) or hasattr(item, "isoformat"):
                rows.append({"date": item})
            else:
                raise BadInput("events: each item is a dict with a date, or a date")
        frame = pd.DataFrame(rows)
    else:
        raise BadInput(f"events: expected a DataFrame or a list, got {type(events).__name__}")
    if "date" not in frame.columns:
        raise BadInput("events: a date column is required")
    if len(frame) > MAX_EVENTS:
        raise BadInput(f"events: more than {MAX_EVENTS} rows")
    if "timing" not in frame.columns:
        frame["timing"] = None
    if "event_id" not in frame.columns:
        frame["event_id"] = [f"E{i + 1:03d}" for i in range(len(frame))]
    return frame


def _prepare_returns(returns: Any, market_returns: Any, model: str) -> tuple[pd.Series, pd.Series | None, tuple[str, ...]]:
    series = c.helpers.as_series(returns, name="returns")
    if not isinstance(series.index, pd.DatetimeIndex):
        raise BadInput("returns need a datetime index (the trading calendar of the event dates)")
    series = series[~series.index.duplicated(keep="last")].sort_index()
    warns: list[str] = []
    market = None
    if model != "mean_adjusted":
        if market_returns is None:
            raise c.Domain(f"model {model} needs market_returns")
        market = c.helpers.as_series(market_returns, name="market_returns")
        if not isinstance(market.index, pd.DatetimeIndex):
            raise BadInput("market_returns need a datetime index")
        market = market[~market.index.duplicated(keep="last")].sort_index()
        joined = pd.concat([series.rename("r"), market.rename("m")], axis=1, join="inner").replace([np.inf, -np.inf], np.nan).dropna()
        if len(joined) < len(series):
            warns.append(f"{len(series) - len(joined)} date(s) dropped: missing or not shared with the market series")
        series, market = joined["r"], joined["m"]
    else:
        clean = c.helpers.clean(series)
        if len(clean) != len(series):
            warns.append(f"dropped {len(series) - len(clean)} NaN or infinite value(s)")
        series = clean
    return series, market, tuple(warns)


def _reaction_position(index: pd.DatetimeIndex, date: pd.Timestamp, timing: str) -> tuple[int, bool]:
    pos = int(index.searchsorted(date.normalize()))
    rolled = pos < len(index) and index[pos].normalize() != date.normalize()
    if timing == "AMC":
        pos += 1
    return pos, rolled


def compute_event_study(returns: pd.Series, market: pd.Series | None, events: pd.DataFrame, *, model: str, estimation: tuple[int, int],
                        window: tuple[int, int], min_estimation: int, default_timing: str | None) -> EventStudyResult:
    """The one event study implementation."""
    es, ee = estimation
    ws, we = window
    if model not in MODELS:
        raise c.Domain(f"model must be one of {list(MODELS)}")
    if not (es < ee < ws <= we):
        raise c.Domain("need estimation_start < estimation_end < window_start <= window_end (days relative to the reaction day)")
    if default_timing is not None and default_timing not in TIMINGS:
        raise c.Domain(f"default_timing must be one of {list(TIMINGS)}")
    index = returns.index
    r_arr = returns.to_numpy(dtype=float)
    m_arr = market.to_numpy(dtype=float) if market is not None else None
    length = we - ws + 1
    records: list[EventRecord] = []
    skipped: list[tuple[str, str]] = []
    for _, row in events.iterrows():
        event_id = str(row["event_id"])
        try:
            date = pd.Timestamp(row["date"])
        except (ValueError, TypeError):
            skipped.append((event_id, "bad_date"))
            continue
        if pd.isna(date):
            skipped.append((event_id, "bad_date"))
            continue
        timing = row["timing"] if isinstance(row["timing"], str) and row["timing"] else default_timing
        if timing is None:
            skipped.append((event_id, "timing_missing"))
            continue
        timing = str(timing).upper() if str(timing).upper() in ("BMO", "AMC") else "intraday" if str(timing).lower() == "intraday" else None
        if timing is None:
            skipped.append((event_id, "timing_invalid"))
            continue
        pos0, _ = _reaction_position(index, date, timing)
        lo, hi = pos0 + es, pos0 + ee
        if pos0 >= len(index) or lo < 0 or pos0 + we >= len(index):
            skipped.append((event_id, "outside_data"))
            continue
        n_est = hi - lo + 1
        if n_est < min_estimation:
            skipped.append((event_id, "estimation_window_too_short"))
            continue
        est_r = r_arr[lo:hi + 1]
        win_slice = slice(pos0 + ws, pos0 + we + 1)
        alpha = beta = None
        if model == "market":
            est_m = m_arr[lo:hi + 1]
            if float(np.ptp(est_m)) == 0.0:
                skipped.append((event_id, "market_constant_in_estimation_window"))
                continue
            fit = econ.ols(est_r, np.column_stack([np.ones(n_est), est_m]))
            alpha, beta = float(fit.beta[0]), float(fit.beta[1])
            sigma = math.sqrt(fit.s2)
            ar = r_arr[win_slice] - (alpha + beta * m_arr[win_slice])
        elif model == "market_adjusted":
            diff = est_r - m_arr[lo:hi + 1]
            sigma = float(np.std(diff, ddof=1))
            ar = r_arr[win_slice] - m_arr[win_slice]
        else:
            sigma = float(np.std(est_r, ddof=1))
            ar = r_arr[win_slice] - float(est_r.mean())
        if not math.isfinite(sigma) or sigma <= 0:
            skipped.append((event_id, "zero_variance_in_estimation_window"))
            continue
        car = float(ar.sum())
        records.append(EventRecord(event_id=event_id, event_date=date.date().isoformat(), reaction_date=index[pos0].date().isoformat(), timing=timing,
                                   alpha=alpha, beta=beta, sigma=float(sigma), n_estimation=int(n_est), ar=tuple(float(v) for v in ar), car=car,
                                   t_car=car / (sigma * math.sqrt(length))))
    if not records:
        raise c.Domain("no event could be measured: " + (", ".join(sorted({r for _, r in skipped})) or "empty event list"))
    ar_mat = np.array([rec.ar for rec in records])
    n = len(records)
    days = tuple(range(ws, we + 1))
    aar = ar_mat.mean(axis=0)
    if n > 1:
        sd = ar_mat.std(axis=0, ddof=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            aar_t = tuple(float(a / (s / math.sqrt(n))) if s > 0 else None for a, s in zip(aar, sd))
    else:
        aar_t = tuple(None for _ in days)
    cars = np.array([rec.car for rec in records])
    car_std = float(cars.std(ddof=1)) if n > 1 else None
    car_t = float(cars.mean() / (car_std / math.sqrt(n))) if car_std and car_std > 0 else None
    car_p = float(2.0 * _lib.sp_stats.t.sf(abs(car_t), n - 1)) if car_t is not None else None
    scar = np.array([rec.t_car for rec in records], dtype=float)
    patell = None
    dof = min(rec.n_estimation for rec in records) - (2 if model == "market" else 1)
    if dof > 4:
        patell = float(scar.sum() / math.sqrt(n * dof / (dof - 2.0)))
    significant = [d for d, t in zip(days, aar_t) if t is not None and abs(t) >= 1.96]
    blocking = (min(significant), max(significant)) if significant else None
    notes = ["abnormal returns come from the estimation window before the event; the t of each event ignores the error of the fitted parameters"]
    if n < 10:
        notes.append(f"only {n} event(s): the cross sectional statistics are weak, read them as indicative")
    return EventStudyResult(model=model, estimation_window=(es, ee), event_window=(ws, we), relative_days=days, n_events=n, n_skipped=len(skipped),
                            records=tuple(records), skipped=tuple(skipped), aar=tuple(float(v) for v in aar), aar_t=aar_t,
                            caar=tuple(float(v) for v in np.cumsum(aar)), car_mean=float(cars.mean()), car_std=car_std, car_t=car_t,
                            car_p_value=car_p, patell_z=patell, share_positive=float(np.mean(cars > 0)), blocking_window=blocking, notes=tuple(notes))


@registry.register("stats.event_study", module="stats",
                   summary="Event study (cumulative abnormal return): abnormal returns around dated events with the market model, mean adjusted or market adjusted. Timing aware (BMO, AMC, intraday). Returns per event CAR, the average and cumulative average abnormal returns with t statistics, and the days with significant abnormal movement (blocking window).",
                   inputs={"returns": _SERIES, "events": _EVENTS, "market_returns": {"type": "series", "required": False, "default": None},
                           "model": {"type": "str", "required": False, "default": "market"},
                           "estimation_start": {"type": "int", "required": False, "default": -250}, "estimation_end": {"type": "int", "required": False, "default": -30},
                           "window_start": {"type": "int", "required": False, "default": -1}, "window_end": {"type": "int", "required": False, "default": 1},
                           "min_estimation": {"type": "int", "required": False, "default": 30}, "default_timing": {"type": "str", "required": False, "default": None}},
                   piece_ids=("AH-57", "FT-089"))
def event_study(returns: Any, events: Any, market_returns: Any = None, model: str = "market", estimation_start: int = -250, estimation_end: int = -30,
                window_start: int = -1, window_end: int = 1, min_estimation: int = 30, default_timing: str | None = None) -> QuantResult:
    name, pieces = "stats.event_study", ("AH-57", "FT-089")
    for label, v in (("estimation_start", estimation_start), ("estimation_end", estimation_end), ("window_start", window_start), ("window_end", window_end), ("min_estimation", min_estimation)):
        limits.as_int(label, v)
    params = {"model": model, "estimation_window": [int(estimation_start), int(estimation_end)], "event_window": [int(window_start), int(window_end)],
              "min_estimation": int(min_estimation), "default_timing": default_timing}

    def compute() -> Any:
        frame = _events_frame(events)
        series, market, warns = _prepare_returns(returns, market_returns, model)
        if len(series) < 60:
            raise c.Domain(f"need at least 60 return observations, got {len(series)}")
        result = compute_event_study(series, market, frame, model=model, estimation=(int(estimation_start), int(estimation_end)),
                                     window=(int(window_start), int(window_end)), min_estimation=int(min_estimation), default_timing=default_timing)
        extra = tuple(f"event {i} skipped: {reason}" for i, reason in result.skipped[:10])
        return result.to_dict(), warns + extra + result.notes

    return c.run(name, unit="return", method=f"event_study_{model}", params=params, piece_ids=pieces, compute=compute,
                 inputs=(returns,) if isinstance(returns, pd.Series) else ())


# ---------------------------------------------------------------- statistics (AH-58)

def compute_event_stats(values: np.ndarray, *, seed: int, n_boot: int, ci: float, threshold_t: float | None) -> EventStats:
    n = len(values)
    mean = float(values.mean())
    std = float(values.std(ddof=1))
    t = mean / (std / math.sqrt(n)) if std > 0 else None
    p = float(2.0 * _lib.sp_stats.t.sf(abs(t), n - 1)) if t is not None else None
    rng = np.random.default_rng(seed)
    draws = values[rng.integers(0, n, (n_boot, n))].mean(axis=1)
    lo, hi = np.quantile(draws, [(1.0 - ci) / 2.0, 1.0 - (1.0 - ci) / 2.0])
    passes = None if threshold_t is None or t is None else bool(abs(t) >= threshold_t)
    return EventStats(n=n, mean=mean, median=float(np.median(values)), std=std, t_stat=t, p_value=p, ci_low=float(lo), ci_high=float(hi), ci_level=ci,
                      n_boot=n_boot, seed=seed, share_positive=float(np.mean(values > 0)), threshold_t=threshold_t, passes_threshold=passes)


@registry.register("stats.event_stats", module="stats",
                   summary="Statistics of event abnormal returns: t statistic of the mean, seeded bootstrap interval, share positive. Each desk sets its own threshold_t; the function never decides for it.",
                   inputs={"values": _SERIES, "seed": {"type": "int", "required": False, "default": 0}, "n_boot": {"type": "int", "required": False, "default": 2000},
                           "ci": {"type": "float", "required": False, "default": 0.95}, "threshold_t": {"type": "float", "required": False, "default": None}},
                   piece_ids=("AH-58",))
def event_stats(values: Any, seed: int = 0, n_boot: int = 2000, ci: float = 0.95, threshold_t: float | None = None) -> QuantResult:
    name, pieces = "stats.event_stats", ("AH-58",)
    limits.check_param("n_boot", n_boot)
    limits.as_int("n_boot", n_boot)
    limits.as_int("seed", seed)
    params = {"seed": int(seed), "n_boot": int(n_boot), "ci": ci, "threshold_t": threshold_t}

    def compute() -> Any:
        level = c.number("ci", ci)
        if not 0.5 <= level < 1.0:
            raise c.Domain("ci must be in [0.5, 1)")
        if n_boot < 100:
            raise c.Domain("n_boot must be at least 100")
        thr = None if threshold_t is None else c.number("threshold_t", threshold_t, low=0.0)
        clean, warns = c.clean_series(values, name="values", min_obs=3)
        if c.is_constant(clean):
            raise c.Domain("constant values: dispersion is zero")
        stats = compute_event_stats(clean.to_numpy(), seed=int(seed), n_boot=int(n_boot), ci=level, threshold_t=thr)
        return stats.to_dict(), warns

    return c.run(name, unit="return", method="t_and_bootstrap", params=params, piece_ids=pieces, compute=compute,
                 inputs=(values,) if isinstance(values, pd.Series) else ())


# ---------------------------------------------------------------- chart (AH-60)

def _svg_polyline(xs: list[float], ys: list[float], color: str, width: float = 2.0, dash: str = "") -> str:
    points = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, ys))
    extra = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<polyline fill="none" stroke="{color}" stroke-width="{width}"{extra} points="{points}"/>'


@registry.register("stats.event_plot", module="stats",
                   summary="Chart of an event study as one self contained HTML file (inline SVG, no external resources): cumulative average abnormal return by day relative to the event, with the average abnormal return as bars and an optional interval.",
                   inputs={"relative_days": _SERIES, "caar": _SERIES, "aar": {"type": "series", "required": False, "default": None},
                           "ci_low": {"type": "series", "required": False, "default": None}, "ci_high": {"type": "series", "required": False, "default": None},
                           "title": {"type": "str", "required": False, "default": "Event study"}}, piece_ids=("AH-60",))
def plot(relative_days: Any, caar: Any, aar: Any = None, ci_low: Any = None, ci_high: Any = None, title: str = "Event study") -> QuantResult:
    name, pieces = "stats.event_plot", ("AH-60",)
    if not isinstance(title, str):
        raise BadInput("title must be text")

    def compute() -> Any:
        days = c.helpers.as_series(relative_days, name="relative_days")
        cum = c.helpers.as_series(caar, name="caar")
        if len(days) != len(cum):
            raise BadInput("relative_days and caar must have the same length")
        if len(days) < 2:
            raise c.Domain("need at least 2 days to draw a chart")
        if not (np.all(np.isfinite(days)) and np.all(np.isfinite(cum))):
            raise c.Domain("relative_days and caar must be finite")
        bars = None if aar is None else c.helpers.as_series(aar, name="aar")
        if bars is not None and (len(bars) != len(days) or not np.all(np.isfinite(bars))):
            raise c.Domain("aar must be finite and have one value per day")
        band = None
        if ci_low is not None and ci_high is not None:
            lo_s, hi_s = c.helpers.as_series(ci_low, name="ci_low"), c.helpers.as_series(ci_high, name="ci_high")
            if len(lo_s) != len(days) or len(hi_s) != len(days) or not (np.all(np.isfinite(lo_s)) and np.all(np.isfinite(hi_s))):
                raise c.Domain("ci_low and ci_high must be finite and have one value per day")
            band = (lo_s.to_numpy(), hi_s.to_numpy())
        width, height, left, right, top, bottom = 760.0, 380.0, 70.0, 20.0, 40.0, 50.0
        d_arr = days.to_numpy(dtype=float)
        pools = [cum.to_numpy(), np.zeros(1)] + ([bars.to_numpy()] if bars is not None else []) + ([band[0], band[1]] if band else [])
        y_min, y_max = float(min(p.min() for p in pools)), float(max(p.max() for p in pools))
        if y_max == y_min:
            y_max, y_min = y_max + 1e-6, y_min - 1e-6
        pad = (y_max - y_min) * 0.08
        y_min, y_max = y_min - pad, y_max + pad
        x0, x1 = float(d_arr.min()), float(d_arr.max())

        def sx(v: float) -> float:
            return left + (v - x0) / (x1 - x0) * (width - left - right) if x1 > x0 else left

        def sy(v: float) -> float:
            return top + (y_max - v) / (y_max - y_min) * (height - top - bottom)

        parts = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" height="{height:.0f}" role="img" aria-label="{html.escape(title, quote=True)}">',
                 f'<rect x="0" y="0" width="{width:.0f}" height="{height:.0f}" fill="#ffffff"/>',
                 f'<line x1="{left}" y1="{sy(0.0):.2f}" x2="{width - right}" y2="{sy(0.0):.2f}" stroke="#888888" stroke-width="1"/>',
                 f'<line x1="{sx(0.0):.2f}" y1="{top}" x2="{sx(0.0):.2f}" y2="{height - bottom}" stroke="#bbbbbb" stroke-dasharray="4 4"/>']
        if bars is not None:
            bw = max((width - left - right) / len(d_arr) * 0.5, 2.0)
            for dv, bv in zip(d_arr, bars.to_numpy()):
                top_y, base_y = sorted((sy(float(bv)), sy(0.0)))
                parts.append(f'<rect x="{sx(float(dv)) - bw / 2:.2f}" y="{top_y:.2f}" width="{bw:.2f}" height="{max(base_y - top_y, 0.5):.2f}" fill="#9db7d5"/>')
        if band is not None:
            up = [sy(float(v)) for v in band[1]]
            dn = [sy(float(v)) for v in band[0]][::-1]
            xs_up = [sx(float(v)) for v in d_arr]
            pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs_up + xs_up[::-1], up + dn))
            parts.append(f'<polygon points="{pts}" fill="#f2c9a0" fill-opacity="0.45" stroke="none"/>')
        parts.append(_svg_polyline([sx(float(v)) for v in d_arr], [sy(float(v)) for v in cum.to_numpy()], "#1f4e8c", 2.5))
        for tick in np.linspace(y_min, y_max, 5):
            parts.append(f'<text x="{left - 8}" y="{sy(float(tick)) + 4:.2f}" font-size="11" text-anchor="end" fill="#333333">{tick * 100:.2f}%</text>')
        for dv in d_arr:
            parts.append(f'<text x="{sx(float(dv)):.2f}" y="{height - bottom + 18}" font-size="11" text-anchor="middle" fill="#333333">{int(dv) if float(dv).is_integer() else dv}</text>')
        parts.append(f'<text x="{(left + width - right) / 2:.2f}" y="{height - 8}" font-size="12" text-anchor="middle" fill="#333333">days relative to the reaction day</text>')
        parts.append(f'<text x="{(left + width - right) / 2:.2f}" y="24" font-size="16" text-anchor="middle" fill="#111111">{html.escape(title)}</text>')
        parts.append("</svg>")
        doc = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>' + html.escape(title) + '</title>'
               '<style>body{font-family:sans-serif;margin:16px;color:#111}</style></head><body>' + "".join(parts) + "</body></html>")
        return {"html": doc, "n_days": int(len(d_arr))}

    return c.run(name, unit="html", method="inline_svg", params={"title": title}, piece_ids=pieces, compute=compute)


# ---------------------------------------------------------------- earnings preview (AFS-027)

def _mid(row: dict) -> float:
    bid, ask = float(row["bid"]), float(row["ask"])
    if not (math.isfinite(bid) and math.isfinite(ask)) or bid < 0 or ask <= 0:
        raise c.Domain("option quotes need bid >= 0 and ask > 0")
    if ask < bid:
        raise c.Domain("crossed quote: ask below bid")
    return (bid + ask) / 2.0


def implied_move_from_chain(chain: Any, reaction_date: pd.Timestamp | None, expiration: str | None) -> dict:
    """Move implied by the at the money straddle of the first expiration that includes the reaction day."""
    if not isinstance(chain, dict) or "spot" not in chain or "options" not in chain:
        raise BadInput("chain: expected a dict with spot and options")
    spot = c.number("spot", chain["spot"], low=0.0)
    if spot <= 0:
        raise c.Domain("spot must be positive")
    rows = chain["options"]
    if not isinstance(rows, (list, tuple)) or not rows:
        raise c.Domain("the option chain is empty")
    by_exp: dict[str, dict[str, dict[float, dict]]] = {}
    for row in rows:
        if not isinstance(row, dict) or not {"expiration", "type", "strike", "bid", "ask"} <= set(row):
            raise BadInput("chain: each option needs expiration, type, strike, bid and ask")
        kind = str(row["type"]).lower()
        if kind not in ("call", "put"):
            raise BadInput("chain: type must be call or put")
        by_exp.setdefault(str(row["expiration"])[:10], {"call": {}, "put": {}})[kind][float(row["strike"])] = row
    expirations = sorted(by_exp)
    if expiration is not None:
        chosen = str(expiration)[:10]
        if chosen not in by_exp:
            raise c.Domain(f"expiration {chosen} is not in the chain")
    else:
        if reaction_date is None:
            raise c.Domain("give the next event (date and timing) or an expiration")
        later = [e for e in expirations if pd.Timestamp(e) >= reaction_date.normalize()]
        if not later:
            raise c.Domain("no expiration in the chain covers the reaction day")
        chosen = later[0]
    calls, puts = by_exp[chosen]["call"], by_exp[chosen]["put"]
    common = sorted(set(calls) & set(puts))
    if not common:
        raise c.Domain("no strike has both a call and a put")
    strike = min(common, key=lambda k: (abs(k - spot), k))
    call_mid, put_mid = _mid(calls[strike]), _mid(puts[strike])
    straddle = call_mid + put_mid
    out = {"expiration": chosen, "atm_strike": strike, "spot": spot, "call_mid": call_mid, "put_mid": put_mid, "straddle": straddle,
           "implied_move": straddle / spot, "upper_breakeven": strike + straddle, "lower_breakeven": strike - straddle}
    if reaction_date is not None:
        out["calendar_days_after_reaction"] = int((pd.Timestamp(chosen) - reaction_date.normalize()).days)
    return out


def historical_reactions(open_: pd.Series, close: pd.Series, events: pd.DataFrame, tick: float, side: int, default_timing: str | None) -> tuple[list[dict], list[tuple[str, str]]]:
    """Reaction of each past event: first tradable open (open plus one tick against the position) over the last close before the news."""
    index = close.index
    out, skipped = [], []
    for _, row in events.iterrows():
        event_id = str(row["event_id"])
        try:
            date = pd.Timestamp(row["date"])
        except (ValueError, TypeError):
            skipped.append((event_id, "bad_date"))
            continue
        timing = row["timing"] if isinstance(row["timing"], str) and row["timing"] else default_timing
        timing = str(timing).upper() if timing and str(timing).upper() in ("BMO", "AMC") else "intraday" if timing and str(timing).lower() == "intraday" else None
        if timing is None:
            skipped.append((event_id, "timing_missing"))
            continue
        pos0, _ = _reaction_position(index, date, timing)
        if pos0 < 1 or pos0 >= len(index):
            skipped.append((event_id, "outside_data"))
            continue
        ref, first_open, day_close = float(close.iloc[pos0 - 1]), float(open_.iloc[pos0]), float(close.iloc[pos0])
        fill = first_open + side * tick
        entry = {"event_id": event_id, "reaction_date": index[pos0].date().isoformat(), "timing": timing, "reference_close": ref,
                 "first_open": first_open, "fill": fill, "reaction": fill / ref - 1.0, "close_to_close": day_close / ref - 1.0}
        if "surprise_z" in events.columns and pd.notna(row.get("surprise_z")):
            entry["surprise_z"] = float(row["surprise_z"])
        out.append(entry)
    return out, skipped


@registry.register("stats.earnings_reaction", module="stats",
                   summary="Earnings preview numbers: three scenarios (bull, base, bear) against the move implied by the at the money straddle and against the historical reaction of the stock, measured from the last close before the news to the first open after it plus one tick, respecting before open and after close reports. Lists 3 to 5 factors that will decide the reaction.",
                   inputs={"open_": _SERIES, "close": _SERIES, "events": _EVENTS, "chain": {"type": "dict {spot, options}", "required": False, "default": None},
                           "next_event": {"type": "dict {date, timing}", "required": False, "default": None}, "expiration": {"type": "str", "required": False, "default": None},
                           "tick_size": {"type": "float", "required": False, "default": 0.01}, "side": {"type": "int", "required": False, "default": 1},
                           "scenarios": {"type": "dict {bull, base, bear}", "required": False, "default": None}, "default_timing": {"type": "str", "required": False, "default": None}},
                   piece_ids=("AFS-027",))
def earnings_reaction(open_: Any, close: Any, events: Any, chain: Any = None, next_event: Any = None, expiration: str | None = None, tick_size: float = 0.01,
                      side: int = 1, scenarios: Any = None, default_timing: str | None = None) -> QuantResult:
    name, pieces = "stats.earnings_reaction", ("AFS-027",)
    limits.as_int("side", side)
    params = {"expiration": expiration, "tick_size": tick_size, "side": int(side), "default_timing": default_timing, "next_event": next_event}

    def compute() -> Any:
        tick = c.number("tick_size", tick_size, low=0.0)
        if side not in (-1, 1):
            raise c.Domain("side must be 1 (buyer) or -1 (seller)")
        so, sc = c.helpers.as_series(open_, name="open_"), c.helpers.as_series(close, name="close")
        if not (isinstance(so.index, pd.DatetimeIndex) and isinstance(sc.index, pd.DatetimeIndex)):
            raise BadInput("open_ and close need a datetime index")
        joined = pd.concat([so.rename("o"), sc.rename("c")], axis=1, join="inner").replace([np.inf, -np.inf], np.nan).dropna().sort_index()
        joined = joined[~joined.index.duplicated(keep="last")]
        if (joined <= 0).any().any():
            raise c.Domain("prices must be positive")
        if len(joined) < 30:
            raise c.Domain(f"need at least 30 complete daily bars, got {len(joined)}")
        frame = _events_frame(events)
        hist, skipped = historical_reactions(joined["o"], joined["c"], frame, tick, int(side), default_timing)
        if len(hist) < 4:
            raise c.Domain(f"need at least 4 measurable past events, got {len(hist)}")
        reactions = np.array([h["reaction"] for h in hist])
        warns: list[str] = [f"event {i} skipped: {r}" for i, r in skipped[:10]]
        implied = None
        if chain is not None:
            reaction_date = None
            if next_event is not None:
                if not isinstance(next_event, dict) or "date" not in next_event:
                    raise BadInput("next_event: expected a dict with date and timing")
                nt = str(next_event.get("timing") or default_timing or "").upper()
                if nt not in ("BMO", "AMC") and nt.lower() != "intraday":
                    raise c.Domain("next_event needs a timing: BMO, AMC or intraday")
                nd = pd.Timestamp(next_event["date"])
                reaction_date = nd + pd.offsets.BDay(1) if nt == "AMC" else nd
            implied = implied_move_from_chain(chain, reaction_date, expiration)
            if implied.get("calendar_days_after_reaction", 0) > 7:
                warns.append("the chosen expiration is more than 7 calendar days after the reaction: the straddle prices extra days and overstates the event move")
            warns.append("the straddle of an expiration that includes the event carries the event jump: do not compare it with a volatility without events")
        else:
            warns.append("no option chain: the implied move is not available")
        mean_abs = float(np.mean(np.abs(reactions)))
        if scenarios is None:
            q25, q50, q75 = (float(v) for v in np.quantile(reactions, [0.25, 0.5, 0.75]))
            scen = {"bear": q25, "base": q50, "bull": q75}
            scen_source = "historical quartiles of the reaction"
        else:
            if not isinstance(scenarios, dict) or not {"bull", "base", "bear"} <= set(scenarios):
                raise BadInput("scenarios: expected a dict with bull, base and bear")
            scen = {k: c.number(f"scenarios.{k}", scenarios[k]) for k in ("bull", "base", "bear")}
            scen_source = "given"
        if not (scen["bear"] <= scen["base"] <= scen["bull"]):
            raise c.Domain("scenarios must satisfy bear <= base <= bull")
        im = implied["implied_move"] if implied else None
        scen_out = {}
        for k, m in scen.items():
            if k == "bull":
                share = float(np.mean(reactions >= m))
            elif k == "bear":
                share = float(np.mean(reactions <= m))
            else:
                share = float(np.mean(np.sign(reactions) == np.sign(m))) if m != 0 else float(np.mean(reactions == 0))
            scen_out[k] = {"move": m, "multiple_of_implied": abs(m) / im if im else None, "historical_share_at_least_as_extreme": share}
        up = reactions[reactions > 0]
        down = reactions[reactions < 0]
        factors: list[str] = []
        if im is not None:
            factors.append(f"implied move {im:.2%} against a mean absolute historical reaction of {mean_abs:.2%} (ratio {im / mean_abs:.2f}); no calibrated threshold says rich or cheap")
        factors.append(f"direction: {len(up)} of {len(reactions)} past reactions were up (average {up.mean() if len(up) else 0.0:.2%}), {len(down)} down (average {down.mean() if len(down) else 0.0:.2%})")
        factors.append(f"dispersion: reactions run from {reactions.min():.2%} to {reactions.max():.2%} with a standard deviation of {reactions.std(ddof=1):.2%}")
        surprises = [(h["surprise_z"], h["reaction"]) for h in hist if "surprise_z" in h]
        if len(surprises) >= 5 and np.std([s for s, _ in surprises]) > 0 and np.std([r for _, r in surprises]) > 0:
            rho = float(np.corrcoef([s for s, _ in surprises], [r for _, r in surprises])[0, 1])
            factors.append(f"surprise: the size of the earnings surprise correlates {rho:.2f} with the reaction over {len(surprises)} events")
        n_amc = sum(1 for h in hist if h["timing"] == "AMC")
        factors.append(f"timing: {n_amc} reports after the close and {len(hist) - n_amc} before the open or in session; reactions are measured at the next open plus one tick")
        factors = factors[:5]
        if len(hist) < 12:
            warns.append(f"only {len(hist)} past events: the historical reaction is a small sample")
        out = {"history": {"n_events": len(hist), "mean": float(reactions.mean()), "median": float(np.median(reactions)), "mean_abs": mean_abs,
                           "median_abs": float(np.median(np.abs(reactions))), "std": float(reactions.std(ddof=1)), "share_up": float(np.mean(reactions > 0)),
                           "share_abs_above_implied": float(np.mean(np.abs(reactions) > im)) if im else None,
                           "implied_over_mean_abs": im / mean_abs if im and mean_abs > 0 else None},
               "events": hist, "scenarios": scen_out, "scenario_source": scen_source, "implied": implied, "factors": factors,
               "fill_rule": "next open plus one tick against the position", "tick_size": tick}
        return out, tuple(warns)

    return c.run(name, unit="return", method="earnings_preview", params=params, piece_ids=pieces, compute=compute)


# ---------------------------------------------------------------- news reaction failure (CTS-037)

VERDICTS = ("CONFIRMED", "NOT_CONFIRMED", "INSUFFICIENT_EVIDENCE")


def group_overlapping(positions: list[int], window: int) -> list[list[int]]:
    """Indexes of the events grouped so that events whose windows overlap fall in the same group (chained)."""
    order = sorted(range(len(positions)), key=lambda i: positions[i])
    groups: list[list[int]] = []
    group_end = -1
    for i in order:
        start = positions[i]
        if groups and start <= group_end:
            groups[-1].append(i)
            group_end = max(group_end, start + window - 1)
        else:
            groups.append([i])
            group_end = start + window - 1
    return groups


@registry.register("stats.reaction_failure", module="stats",
                   summary="Did the market fail to react to favourable news? Tests the drift of the abnormal return over the window across independent groups of events (overlapping windows are merged) instead of counting failures. Verdict CONFIRMED, NOT_CONFIRMED or INSUFFICIENT_EVIDENCE (fewer than min_groups groups).",
                   inputs={"returns": _SERIES, "events": _EVENTS, "market_returns": {"type": "series", "required": False, "default": None},
                           "direction": {"type": "int", "required": False, "default": 1}, "window": {"type": "int", "required": False, "default": 3},
                           "alpha": {"type": "float", "required": False, "default": 0.05}, "min_groups": {"type": "int", "required": False, "default": 3},
                           "model": {"type": "str", "required": False, "default": "market"}, "estimation_start": {"type": "int", "required": False, "default": -250},
                           "estimation_end": {"type": "int", "required": False, "default": -30}, "default_timing": {"type": "str", "required": False, "default": None}},
                   piece_ids=("CTS-037",))
def reaction_failure(returns: Any, events: Any, market_returns: Any = None, direction: int = 1, window: int = 3, alpha: float = 0.05, min_groups: int = 3,
                     model: str = "market", estimation_start: int = -250, estimation_end: int = -30, default_timing: str | None = None) -> QuantResult:
    name, pieces = "stats.reaction_failure", ("CTS-037",)
    limits.check_param("window", window)
    for label, v in (("direction", direction), ("window", window), ("min_groups", min_groups), ("estimation_start", estimation_start), ("estimation_end", estimation_end)):
        limits.as_int(label, v)
    params = {"direction": int(direction), "window": int(window), "alpha": alpha, "min_groups": int(min_groups), "model": model,
              "estimation_window": [int(estimation_start), int(estimation_end)], "default_timing": default_timing}

    def compute() -> Any:
        if direction not in (-1, 1):
            raise c.Domain("direction must be 1 (favourable news expects a rise) or -1")
        if not 1 <= window <= 30:
            raise c.Domain("window must be between 1 and 30 days")
        level = c.number("alpha", alpha, low=0.0, high=0.5)
        if level <= 0:
            raise c.Domain("alpha must be positive")
        if min_groups < 3:
            raise c.Domain("min_groups cannot be below 3: a test over fewer groups has no power")
        frame = _events_frame(events)
        series, market, warns = _prepare_returns(returns, market_returns, model)
        if len(series) < 60:
            raise c.Domain(f"need at least 60 return observations, got {len(series)}")
        study = compute_event_study(series, market, frame, model=model, estimation=(int(estimation_start), int(estimation_end)), window=(0, int(window) - 1),
                                    min_estimation=30, default_timing=default_timing)
        index = series.index
        positions = [int(index.get_loc(pd.Timestamp(rec.reaction_date))) for rec in study.records]
        directional = np.array([direction * rec.car for rec in study.records])
        groups = group_overlapping(positions, int(window))
        group_means = np.array([float(directional[g].mean()) for g in groups])
        n_groups = len(groups)
        naive = float(np.mean(directional <= 0.0))
        warns = warns + ("the share of events with no rise is shown for reference only: with pure noise it is near 50%, so it never confirms a failure",)
        base = {"n_events": study.n_events, "n_groups": n_groups, "alpha": level, "naive_failure_share": naive, "group_mean_car": group_means,
                "group_sizes": [len(g) for g in groups], "mean_directional_car": float(group_means.mean())}
        if n_groups < min_groups:
            return ({**base, "verdict": "INSUFFICIENT_EVIDENCE", "reason": f"{n_groups} independent group(s), {int(min_groups)} needed", "t_stat": None,
                     "p_failure": None, "p_reaction": None}, warns)
        sd = float(group_means.std(ddof=1))
        if sd <= 0:
            raise c.Domain("the group results are identical: the test is undefined")
        t_stat = float(group_means.mean() / (sd / math.sqrt(n_groups)))
        p_fail = float(_lib.sp_stats.t.cdf(t_stat, n_groups - 1))
        p_react = float(_lib.sp_stats.t.sf(t_stat, n_groups - 1))
        if p_fail < level:
            verdict, reason = "CONFIRMED", "the directional drift over the window is significantly below zero: favourable news was sold"
        elif p_react < level:
            verdict, reason = "NOT_CONFIRMED", "the market reacted in the expected direction"
        else:
            verdict, reason = "NOT_CONFIRMED", "no significant drift either way: a failure to react is not established"
        return ({**base, "verdict": verdict, "reason": reason, "t_stat": t_stat, "p_failure": p_fail, "p_reaction": p_react, "df": n_groups - 1,
                 "skipped": [list(s) for s in study.skipped]}, warns + ("event lists curated by a language model can contain invented events: check them against a primary source",))

    return c.run(name, unit="return", method="group_drift_t_test", params=params, piece_ids=pieces, compute=compute)
