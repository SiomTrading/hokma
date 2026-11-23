"""Tick processing: cleaning, bars, trade classification (SPEC-017: ECT-013).

Trade ticks arrive as a DataFrame indexed by UTC time with at least `price` and `size`; quotes have `bid` and `ask`.

Design choices that differ from equity-only practice:
- no session hours and no equity condition codes are assumed: futures trade almost around the clock, so a session filter and
  the excluded conditions are explicit arguments and default to none;
- the outlier filter looks only at PAST ticks by default, so it is safe live; the centered variant also uses future ticks
  and is allowed only offline (`offline=True`);
- duplicates are rows that repeat in every column, not just the timestamp: several real trades can share one timestamp;
- trade direction uses no artificial delay by default (modern feeds timestamp trades and quotes together);
- nothing is filled: a bar interval without ticks is not produced; the last bar that is still forming is marked incomplete.
Corporate-action adjustments do not apply to futures and are not included.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Collection

import numpy as np
import pandas as pd

from hokma.data import sentinels
from hokma.data.sentinels import DataResult, parse_time, utcnow
from hokma.quant.errors import BadInput


def _require(ticks: pd.DataFrame, *columns: str) -> None:
    if not isinstance(ticks, pd.DataFrame) or not isinstance(ticks.index, pd.DatetimeIndex):
        raise BadInput("ticks must be a DataFrame indexed by time")
    missing = [c for c in columns if c not in ticks.columns]
    if missing:
        raise BadInput(f"ticks need the columns {missing}")


# ---------------------------------------------------------------------------------------------------- cleaning
@dataclass(frozen=True)
class TickCleaning:
    received: int
    non_positive: int
    excluded_condition: int
    outside_session: int
    duplicates: int
    outliers: int

    @property
    def kept(self) -> int:
        return self.received - self.non_positive - self.excluded_condition - self.outside_session - self.duplicates - self.outliers


def _rolling_mad(values: pd.Series, window: int, min_periods: int, center: bool = False) -> pd.Series:
    return values.rolling(window, min_periods=min_periods, center=center).apply(
        lambda x: np.median(np.abs(x - np.median(x))), raw=True)


def clean_ticks(ticks: pd.DataFrame, *, excluded_conditions: Collection[str] = (), session: tuple[str, str] | None = None,
                window: int = 50, threshold: float = 5.0, min_mad: float | None = None, centered: bool = False,
                offline: bool = False) -> tuple[pd.DataFrame, TickCleaning]:
    """Remove bad ticks in a fixed order and report how many each filter removed.

    1. non-positive price or size; 2. excluded condition codes (column `condition`); 3. outside `session` ("HH:MM",
    "HH:MM" in the index time zone); 4. exact duplicate rows; 5. price outliers: more than `threshold` rolling median
    absolute deviations from the rolling median of the previous `window` ticks. `min_mad` keeps a flat market from flagging
    every move; by default it is the smallest price step seen. `centered=True` also uses future ticks and needs `offline=True`.
    """
    _require(ticks, "price", "size")
    if centered and not offline:
        raise BadInput("the centered outlier filter uses future ticks: it is allowed only offline (offline=True)")
    frame = ticks.sort_index(kind="stable")
    received = len(frame)
    positive = (frame["price"] > 0) & (frame["size"] > 0)
    non_positive = int((~positive).sum())
    frame = frame[positive]
    excluded = 0
    if excluded_conditions and "condition" in frame.columns:
        mask = frame["condition"].isin(set(excluded_conditions))
        excluded, frame = int(mask.sum()), frame[~mask]
    outside = 0
    if session is not None:
        inside = frame.index.indexer_between_time(session[0], session[1])
        keep = np.zeros(len(frame), dtype=bool)
        keep[inside] = True
        outside, frame = int((~keep).sum()), frame[keep]
    repeated = frame.reset_index().duplicated().to_numpy()
    duplicates, frame = int(repeated.sum()), frame[~repeated]
    outliers = 0
    if len(frame) > 3:
        price = frame["price"].astype(float)
        steps = price.diff().abs()
        floor = min_mad if min_mad is not None else (float(steps[steps > 0].min()) if (steps > 0).any() else 1e-8)
        periods = max(3, window // 5)
        if centered:
            median = price.rolling(window, center=True, min_periods=periods).median()
            mad = _rolling_mad(price, window, periods, center=True)
        else:
            past = price.shift(1)
            median = past.rolling(window, min_periods=periods).median()
            mad = _rolling_mad(past, window, periods)
        deviation = (price - median).abs() / mad.clip(lower=floor)
        flagged = (deviation >= threshold).to_numpy()   # a tick with no history to compare against is kept
        outliers, frame = int(flagged.sum()), frame[~flagged]
    return frame, TickCleaning(received, non_positive, excluded, outside, duplicates, outliers)


def cut_ticks(ticks: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    """Ticks stamped on or before `as_of`."""
    _require(ticks)
    index = ticks.index if ticks.index.tz is not None else ticks.index.tz_localize("UTC")
    return ticks[index <= pd.Timestamp(parse_time(as_of))]


# ---------------------------------------------------------------------------------------------------- bars
def _group_bars(ticks: pd.DataFrame, bar_id: np.ndarray) -> pd.DataFrame:
    data = ticks.assign(_bar=bar_id, _dollars=ticks["price"] * ticks["size"])
    grouped = data.groupby("_bar", sort=True)
    bars = pd.DataFrame({
        "open": grouped["price"].first(), "high": grouped["price"].max(), "low": grouped["price"].min(),
        "close": grouped["price"].last(), "volume": grouped["size"].sum(), "dollar_volume": grouped["_dollars"].sum(),
        "n_ticks": grouped["price"].size(),
    })
    first = pd.Series(np.arange(len(ticks))).groupby(np.asarray(bar_id)).first()
    bars.index = pd.DatetimeIndex(ticks.index[first.reindex(bars.index).to_numpy()], name="time")
    return bars


def time_bars(ticks: pd.DataFrame, rule: str = "1min", *, as_of: datetime | None = None) -> pd.DataFrame:
    """OHLCV bars of fixed duration. An interval with no ticks has no bar. A bar is `complete` when its interval ended by
    `as_of`; with no `as_of` only the last bar is marked incomplete."""
    _require(ticks, "price", "size")
    grouped = ticks["price"].resample(rule, label="left", closed="left")
    out = pd.DataFrame({"open": grouped.first(), "high": grouped.max(), "low": grouped.min(), "close": grouped.last(),
                        "volume": ticks["size"].resample(rule, label="left", closed="left").sum(), "n_ticks": grouped.count()})
    out = out[out["n_ticks"] > 0]
    out.index.name = "time"
    if len(out) == 0:
        return out.assign(complete=pd.Series(dtype=bool))
    ends = out.index + pd.tseries.frequencies.to_offset(rule)
    complete = (ends <= pd.Timestamp(parse_time(as_of))) if as_of is not None else np.arange(len(out)) < len(out) - 1
    return out.assign(complete=np.asarray(complete, dtype=bool))


def tick_bars(ticks: pd.DataFrame, n_ticks: int) -> pd.DataFrame:
    """One bar per `n_ticks` trades. Only the last bar can be incomplete."""
    _require(ticks, "price", "size")
    if n_ticks < 1:
        raise BadInput("n_ticks must be at least 1")
    bars = _group_bars(ticks, np.arange(len(ticks)) // n_ticks)
    return bars.assign(complete=bars["n_ticks"] >= n_ticks)


def volume_bars(ticks: pd.DataFrame, volume: float) -> pd.DataFrame:
    """One bar per `volume` traded. A trade belongs to the bar in which it starts, so a large trade can overfill a bar.
    Only the last bar can be incomplete."""
    _require(ticks, "price", "size")
    if volume <= 0:
        raise BadInput("volume must be positive")
    cumulative = ticks["size"].astype(float).cumsum().to_numpy()
    bars = _group_bars(ticks, np.floor((cumulative - ticks["size"].astype(float).to_numpy()) / volume).astype(int))
    return bars.assign(complete=bars["volume"] >= volume)


def dollar_bars(ticks: pd.DataFrame, dollars: float) -> pd.DataFrame:
    """One bar per `dollars` traded (price times size). Only the last bar can be incomplete."""
    _require(ticks, "price", "size")
    if dollars <= 0:
        raise BadInput("dollars must be positive")
    value = (ticks["price"] * ticks["size"]).astype(float)
    bars = _group_bars(ticks, np.floor((value.cumsum() - value).to_numpy() / dollars).astype(int))
    return bars.assign(complete=bars["dollar_volume"] >= dollars)


def tick_rule(price: pd.Series) -> pd.Series:
    """Direction of each trade from the price change: +1 up, -1 down, an unchanged price keeps the previous direction.
    The first trade has no direction (0); nothing is assumed."""
    sign = np.sign(price.astype(float).diff())
    return sign.replace(0, np.nan).ffill().fillna(0).astype(int)


def imbalance_bars(ticks: pd.DataFrame, *, kind: str = "tick", initial_ticks: int = 100, initial_imbalance: float = 0.1,
                   span: int = 10) -> pd.DataFrame:
    """Bars that close when the signed order flow is larger than expected (tick or volume imbalance).

    A bar closes when |cumulative signed flow| reaches E[ticks per bar] times |E[signed flow per tick]|. Both expectations
    start at `initial_ticks` and `initial_imbalance` and are updated after every bar as exponential averages over the last
    `span` bars. This is a Python loop over the ticks: use it offline.
    """
    _require(ticks, "price", "size")
    if kind not in ("tick", "volume"):
        raise BadInput("kind must be 'tick' or 'volume'")
    direction = tick_rule(ticks["price"]).to_numpy()
    flow = direction * (ticks["size"].astype(float).to_numpy() if kind == "volume" else 1.0)
    alpha = 2.0 / (span + 1.0)
    expected_ticks, expected_flow = float(initial_ticks), float(initial_imbalance) * (1.0 if kind == "tick" else float(ticks["size"].mean()))
    bar_id, current, theta, count, flow_sum = np.zeros(len(ticks), dtype=int), 0, 0.0, 0, 0.0
    for i, x in enumerate(flow):
        bar_id[i] = current
        theta, count, flow_sum = theta + x, count + 1, flow_sum + x
        if abs(theta) >= expected_ticks * abs(expected_flow):
            expected_ticks = alpha * count + (1 - alpha) * expected_ticks
            expected_flow = alpha * (flow_sum / count) + (1 - alpha) * expected_flow
            current, theta, count, flow_sum = current + 1, 0.0, 0, 0.0
    bars = _group_bars(ticks, bar_id)
    return bars.assign(complete=np.arange(len(bars)) < len(bars) - (1 if count else 0))   # ticks still accumulating: open bar


def lee_ready(trades: pd.DataFrame, quotes: pd.DataFrame, *, delay: str | pd.Timedelta = "0s") -> pd.DataFrame:
    """Classify trades as buyer (+1) or seller (-1) initiated.

    Quote rule against the midpoint of the latest quote known `delay` before the trade; a trade at the midpoint, with no
    earlier quote, or against a crossed quote (bid above ask) falls back to the tick rule. Returns the trades with `bid`,
    `ask`, `side` and the signed volumes `buy_volume` and `sell_volume`.
    """
    _require(trades, "price", "size")
    _require(quotes, "bid", "ask")
    delayed = quotes[["bid", "ask"]].sort_index().copy()
    delayed.index = delayed.index + pd.Timedelta(delay)
    merged = pd.merge_asof(trades.sort_index(), delayed, left_index=True, right_index=True, direction="backward")
    valid = merged["bid"].notna() & merged["ask"].notna() & (merged["bid"] <= merged["ask"])
    midpoint = (merged["bid"] + merged["ask"]) / 2
    quote_side = np.where(valid & (merged["price"] > midpoint), 1, np.where(valid & (merged["price"] < midpoint), -1, 0))
    side = np.where(quote_side != 0, quote_side, tick_rule(merged["price"]).to_numpy())
    merged["side"] = side.astype(int)
    merged["buy_volume"] = merged["size"] * (merged["side"] == 1)
    merged["sell_volume"] = merged["size"] * (merged["side"] == -1)
    return merged


def roll_spread(price: pd.Series) -> dict[str, float | None]:
    """Bid-ask bounce: first-order autocovariance of price changes. The effective spread is 2 * sqrt(-cov) when the
    covariance is negative and None when it is not (the estimator does not exist then)."""
    changes = price.astype(float).diff().dropna()
    if len(changes) < 3:
        return {"autocovariance": None, "autocorrelation": None, "spread": None}
    cov = float(np.cov(changes.to_numpy()[1:], changes.to_numpy()[:-1])[0, 1])
    corr = changes.autocorr(lag=1)
    return {"autocovariance": cov, "autocorrelation": None if pd.isna(corr) else float(corr),
            "spread": 2.0 * float(np.sqrt(-cov)) if cov < 0 else None}


# ---------------------------------------------------------------------------------------------------- storage
def write_ticks(ticks: pd.DataFrame, path: str | Path) -> Path:
    """Store ticks as compressed CSV with ISO UTC timestamps, exactly as they are (no rounding)."""
    _require(ticks)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = ticks.copy()
    frame.index = (frame.index.tz_localize("UTC") if frame.index.tz is None else frame.index.tz_convert("UTC")).rename("time")
    frame.to_csv(target, compression="gzip")
    return target


def read_ticks(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path, compression="gzip", index_col="time")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True, format="ISO8601"), name="time")
    return frame


# ---------------------------------------------------------------------------------------------------- the call
def bars_from_ticks(ticks: pd.DataFrame, kind: str, size: float | str, *, as_of: datetime, source: str = "tick_file",
                    clean: bool = True, clock: Callable[[], datetime] = utcnow) -> DataResult:
    """Cut at `as_of`, clean with the live-safe filter and build `time`, `tick`, `volume`, `dollar` or `imbalance` bars.

    Empty after the cut or after cleaning: `NO_DATA`. The result declares its source and when it was received.
    """
    cut, now = parse_time(as_of), clock()
    data = cut_ticks(ticks, cut)
    if len(data) == 0:
        return sentinels.no_data(source, "no_ticks_by_as_of", as_of=cut, received_at=now)
    report = None
    if clean:
        data, report = clean_ticks(data)
        if len(data) == 0:
            return sentinels.no_data(source, "no_ticks_after_cleaning", as_of=cut, received_at=now)
    builders: dict[str, Callable[[], pd.DataFrame]] = {
        "time": lambda: time_bars(data, str(size), as_of=cut), "tick": lambda: tick_bars(data, int(size)),
        "volume": lambda: volume_bars(data, float(size)), "dollar": lambda: dollar_bars(data, float(size)),
        "imbalance": lambda: imbalance_bars(data, kind=str(size)),
    }
    if kind not in builders:
        raise BadInput(f"kind must be one of {sorted(builders)}")
    bars = builders[kind]()
    meta: dict[str, Any] = {"bar_kind": kind, "cleaning": None if report is None else report.__dict__}
    return sentinels.ok(bars, source=source, as_of=cut, received_at=now, meta=meta)
