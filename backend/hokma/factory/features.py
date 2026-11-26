"""Features for strategy research without leakage (SPEC-020: ECT-069). Pure functions over numpy arrays.

The rules, each one enforced by code or by a test:

  * a statistic at row t uses rows up to t and nothing later (rolling and expanding z-scores, fractional differences);
  * a feature that reads a future row fails `check_causal` with LeakError: its value at a row changes when later rows are removed;
  * missing values are carried forward from the last real value and never interpolated, because interpolation reads the next one;
  * the target is the return from the open of the next bar (the fill of the house), the same function as the IC study;
  * a feature is selected with the training rows of a fold only: the test rows are never read;
  * illiquidity is measured per dollar traded (price times volume times the multiplier), never per unit.

Bars are minute bars for the intraday line and daily bars for every other: the windows below are counted in bars.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from hokma.factory.studies import _h3
from hokma.factory.studies.ic_decay import IcFn, forward_returns as target_next_open       # one definition of the target
from hokma.quant.errors import BadInput
from hokma.quant.limits import PARAM_CAPS, as_int

KINDS = ("price", "volume", "fundamental", "microstructure", "cross")
MAX_FEATURES = 256


class LeakError(ValueError):
    """A feature uses information from a row after the one it describes. `code` is stable: leak."""

    code = "leak"


def _vector(name: str, values: Any) -> np.ndarray:
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError):
        raise BadInput(f"{name} must be numbers") from None
    if array.ndim != 1:
        raise BadInput(f"{name} must be a one dimensional list")
    return array


def _window(name: str, value: Any, minimum: int = 2) -> int:
    n = as_int(name, value)
    if n < minimum or n > PARAM_CAPS["window"]:
        raise BadInput(f"{name} must be from {minimum} to {PARAM_CAPS['window']}, got {n}")
    return n


# ---- the causality check --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Feature:
    name: str
    kind: Literal["price", "volume", "fundamental", "microstructure", "cross"]
    fn: Callable[[Mapping[str, np.ndarray]], np.ndarray]       # data fields in, one value per row out

    def __post_init__(self) -> None:
        if not self.name or self.kind not in KINDS or not callable(self.fn):
            raise BadInput(f"a feature needs a name, a callable and a kind from {', '.join(KINDS)}")


def _rows(data: Mapping[str, Any]) -> tuple[dict[str, np.ndarray], int]:
    arrays = {k: np.asarray(v, dtype=float) for k, v in data.items()}
    sizes = {len(a) for a in arrays.values()}
    if not arrays or len(sizes) != 1:
        raise BadInput("every data field must have the same number of rows")
    return arrays, sizes.pop()


def check_causal(feature: Feature, data: Mapping[str, Any], *, cuts: Sequence[int] | None = None) -> None:
    """LeakError when the value of the feature at a row changes after the rows below the cut are the only ones left.

    A causal feature gives at row t the same value whether or not later rows exist. The check runs the feature on the full data and
    on prefixes of it (the middle, one row short of the end and any cut the caller gives) and compares the common rows.
    """
    arrays, n = _rows(data)
    full = np.asarray(feature.fn(arrays), dtype=float)
    if full.shape != (n,):
        raise BadInput(f"feature {feature.name!r} returned shape {full.shape} for {n} rows")
    points = sorted({c for c in (*(cuts or ()), n // 2, n - 1) if 1 <= c < n})
    for cut in points:
        part = np.asarray(feature.fn({k: v[:cut] for k, v in arrays.items()}), dtype=float)
        if part.shape != (cut,) or not np.allclose(part, full[:cut], equal_nan=True):
            bad = int(np.flatnonzero(~np.isclose(part, full[:cut], equal_nan=True))[0]) if part.shape == (cut,) else cut
            raise LeakError(f"feature {feature.name!r} changes at row {bad} when the rows after row {cut - 1} are removed: it uses a future row")


def build(features: Sequence[Feature], data: Mapping[str, Any], *, check: bool = True) -> dict[str, np.ndarray]:
    """Every feature by name. With `check` (the default) each one passes `check_causal` first."""
    if not 1 <= len(features) <= MAX_FEATURES:
        raise BadInput(f"features is a list of 1 to {MAX_FEATURES}")
    names = [f.name for f in features]
    if len(set(names)) != len(names):
        raise BadInput("a feature name appears twice")
    arrays, _ = _rows(data)
    out = {}
    for feature in features:
        if check:
            check_causal(feature, arrays)
        out[feature.name] = np.asarray(feature.fn(arrays), dtype=float)
    return out


# ---- transformations that read the past only ------------------------------------------------------------------------

def returns(close: Any, *, log: bool = False) -> np.ndarray:
    """Simple return, or log return, of each bar against the one before. The first row is NaN."""
    prices = _vector("close", close)
    out = np.full(len(prices), np.nan)
    if len(prices) > 1:
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(prices[:-1] > 0, prices[1:] / prices[:-1], np.nan)
        out[1:] = np.log(ratio) if log else ratio - 1.0
    return out


def forward_fill(values: Any, max_gap: int | None = None) -> np.ndarray:
    """Each missing value takes the last real one. Never interpolated. After `max_gap` missing rows in a row the value is missing again."""
    x = _vector("values", values)
    limit = None if max_gap is None else as_int("max_gap", max_gap)
    if limit is not None and limit < 0:
        raise BadInput("max_gap is zero or more")
    out = x.copy()
    last, gap = np.nan, 0
    for i, v in enumerate(x):
        if np.isfinite(v):
            last, gap = v, 0
        else:
            gap += 1
            out[i] = last if (limit is None or gap <= limit) else np.nan
    return out


def rolling_zscore(values: Any, window: int) -> np.ndarray:
    """(x - mean) / std over the last `window` rows including the current one (ddof 1). NaN until the window is full or when the std is zero."""
    x, w = _vector("values", values), _window("window", window)
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        view = np.lib.stride_tricks.sliding_window_view(x, w)
        mean, std = view.mean(axis=1), view.std(axis=1, ddof=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[w - 1:] = np.where(std > 0, (x[w - 1:] - mean) / std, np.nan)
    return out


def expanding_zscore(values: Any, min_periods: int) -> np.ndarray:
    """(x - mean) / std over every row up to the current one. NaN before `min_periods` rows or when the std is zero."""
    x, m = _vector("values", values), _window("min_periods", min_periods)
    out = np.full(len(x), np.nan)
    for i in range(m - 1, len(x)):
        past = x[:i + 1]
        std = past.std(ddof=1)
        out[i] = (x[i] - past.mean()) / std if std > 0 else np.nan
    return out


def fractional_weights(d: float, threshold: float) -> np.ndarray:
    """Weights of the fractional difference of order d: w0 = 1, w_k = -w_(k-1) * (d - k + 1) / k, stopped when |w_k| falls below `threshold`."""
    if isinstance(d, bool) or not isinstance(d, (int, float)) or not 0.0 < float(d) <= 2.0:
        raise BadInput(f"d must be a number above 0 and at most 2, got {d!r}")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not threshold > 0:
        raise BadInput("threshold must be a positive number")
    weights = [1.0]
    k = 1
    while k <= PARAM_CAPS["window"]:
        w = -weights[-1] * (float(d) - k + 1) / k
        if abs(w) < threshold:
            break
        weights.append(w)
        k += 1
    return np.array(weights)


def fractional_diff(values: Any, d: float, threshold: float = 1e-5) -> np.ndarray:
    """Fractional difference of order d with a fixed width: the value at row t is the sum of w_k * x[t - k]. NaN until the width is filled."""
    x = _vector("values", values)
    w = fractional_weights(d, threshold)
    width = len(w)
    out = np.full(len(x), np.nan)
    if len(x) >= width:
        view = np.lib.stride_tricks.sliding_window_view(x, width)
        out[width - 1:] = view @ w[::-1]
    return out


def amihud(returns_: Any, close: Any, volume: Any, *, multiplier: float = 1.0, scale: float = 1_000_000.0) -> np.ndarray:
    """|return| per scaled dollar traded: |r| / (close * volume * multiplier) * scale. NaN where there was no volume.

    The volume is in units (shares or contracts): it is turned into dollars first. Dividing by units mixes a cheap stock with an expensive one.
    """
    r, c, v = _vector("returns", returns_), _vector("close", close), _vector("volume", volume)
    if not (len(r) == len(c) == len(v)):
        raise BadInput("returns, close and volume must have the same length")
    if not (multiplier > 0 and scale > 0):
        raise BadInput("multiplier and scale must be positive")
    dollars = c * v * float(multiplier)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(dollars > 0, np.abs(r) / dollars * float(scale), np.nan)


def _average_ranks(row: np.ndarray) -> np.ndarray:
    order = np.argsort(row, kind="stable")
    sorted_values = row[order]
    ranks = np.empty(len(row))
    start = 0
    while start < len(row):
        end = start
        while end + 1 < len(row) and sorted_values[end + 1] == sorted_values[start]:
            end += 1
        ranks[order[start:end + 1]] = (start + end) / 2.0 + 1.0
        start = end + 1
    return ranks


def _panel(name: str, values: Any) -> np.ndarray:
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError):
        raise BadInput(f"{name} must be numbers") from None
    if array.ndim != 2:
        raise BadInput(f"{name} must be a table: one row per date, one column per asset")
    return array


def cross_section_zscore(panel: Any) -> np.ndarray:
    """Each row is standardized against the assets of the same date only (ddof 1). A row with fewer than 2 assets or no spread is NaN."""
    x = _panel("panel", panel)
    out = np.full(x.shape, np.nan)
    for i, row in enumerate(x):
        ok = np.isfinite(row)
        if ok.sum() >= 2 and row[ok].std(ddof=1) > 0:
            out[i, ok] = (row[ok] - row[ok].mean()) / row[ok].std(ddof=1)
    return out


def cross_section_rank(panel: Any) -> np.ndarray:
    """Each row ranked among the assets of the same date and scaled to 0 (lowest) to 1 (highest); ties share the average rank."""
    x = _panel("panel", panel)
    out = np.full(x.shape, np.nan)
    for i, row in enumerate(x):
        ok = np.isfinite(row)
        if ok.sum() >= 2:
            out[i, ok] = (_average_ranks(row[ok]) - 1.0) / (ok.sum() - 1.0)
    return out


# ---- information coefficient and selection inside the training rows -------------------------------------------------

def ic_t_stat(icir: float, n_periods: int) -> float:
    """The t statistic of a mean IC is ICIR times the square root of the number of periods: two thresholds on both are one threshold."""
    return float(icir) * math.sqrt(as_int("n_periods", n_periods))


def ic_series(x: Any, y: Any, rows: Sequence[int], ic_fn: IcFn, *, block: int | None = None, min_obs: int = 3) -> np.ndarray:
    """The IC of a feature against the target at each period of the given rows only.

    A table (date by asset) gives one IC per date across the assets. A single series needs `block`: the rows are cut in consecutive blocks
    of that size and each full block gives one IC. Periods with fewer than `min_obs` complete pairs are skipped.
    """
    fx, fy = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if fx.shape != fy.shape or fx.ndim not in (1, 2):
        raise BadInput("the feature and the target must have the same shape: a series or a table")
    index = np.asarray(sorted(set(as_int("row", r) for r in rows)), dtype=int)
    if index.size and (index[0] < 0 or index[-1] >= len(fx)):
        raise BadInput("rows holds a row that does not exist")
    values: list[float] = []

    def one(a: np.ndarray, b: np.ndarray) -> None:
        keep = np.isfinite(a) & np.isfinite(b)
        if keep.sum() >= min_obs:
            ic = _h3.scalar(ic_fn(a[keep], b[keep]))
            if ic is not None:
                values.append(ic)

    if fx.ndim == 2:
        for r in index:
            one(fx[r], fy[r])
    else:
        size = _window("block", block if block is not None else 0, minimum=min_obs)
        for start in range(0, len(index) - size + 1, size):
            chunk = index[start:start + size]
            one(fx[chunk], fy[chunk])
    return np.array(values)


@dataclass(frozen=True)
class SelectionRule:
    """Every limit is stated by the caller: there is no default."""

    min_abs_ic: float
    min_icir: float
    max_abs_corr: float
    vif_limit: float
    min_periods: int


@dataclass(frozen=True)
class FeatureStat:
    ic_mean: float | None
    icir: float | None
    n_periods: int
    t_stat: float | None


@dataclass(frozen=True)
class Selection:
    kept: tuple[str, ...]
    dropped: Mapping[str, str]         # name -> reason: weak_ic, weak_icir, few_periods, correlated_with:<name>, vif
    stats: Mapping[str, FeatureStat]
    train_rows: int


def _stack(arrays: Sequence[np.ndarray], rows: np.ndarray) -> np.ndarray:
    return np.column_stack([a[rows].reshape(-1) for a in arrays])


def select_in_train(features: Mapping[str, Any], target: Any, train_rows: Sequence[int], rule: SelectionRule, ic_fn: IcFn | None = None, *,
                    block: int | None = None) -> Selection:
    """The features worth keeping, judged on `train_rows` alone: IC and ICIR, then correlation between features, then VIF.

    Nothing outside `train_rows` is read, so the test rows of a fold cannot change the answer. The first step needs the IC function of H3
    (`hokma.quant.portfolio.ic`); a caller can inject one.
    """
    if not features or len(features) > MAX_FEATURES:
        raise BadInput(f"features is a mapping of 1 to {MAX_FEATURES} arrays")
    if not (rule.min_abs_ic >= 0 and rule.min_icir >= 0 and 0 < rule.max_abs_corr <= 1 and rule.vif_limit > 1 and as_int("min_periods", rule.min_periods) >= 2):
        raise BadInput("the selection rule has a value outside its range")
    fn = ic_fn or _h3.information_coefficient
    arrays = {k: np.asarray(v, dtype=float) for k, v in features.items()}
    y = np.asarray(target, dtype=float)
    rows = np.asarray(sorted(set(as_int("row", r) for r in train_rows)), dtype=int)
    if rows.size == 0 or rows[0] < 0 or rows[-1] >= len(y):
        raise BadInput("train_rows is empty or holds a row that does not exist")
    stats: dict[str, FeatureStat] = {}
    dropped: dict[str, str] = {}
    alive: list[str] = []
    for name in sorted(arrays):
        series = ic_series(arrays[name], y, rows, fn, block=block)
        n = len(series)
        if n < rule.min_periods:
            stats[name] = FeatureStat(None, None, n, None)
            dropped[name] = "few_periods"
            continue
        mean, std = float(series.mean()), float(series.std(ddof=1))
        icir = mean / std if std > 0 else None
        stats[name] = FeatureStat(mean, icir, n, None if icir is None else ic_t_stat(icir, n))
        if abs(mean) < rule.min_abs_ic:
            dropped[name] = "weak_ic"
        elif icir is None or abs(icir) < rule.min_icir:
            dropped[name] = "weak_icir"
        else:
            alive.append(name)
    kept: list[str] = []
    for name in sorted(alive, key=lambda k: (-abs(stats[k].icir or 0.0), k)):     # the stronger of two correlated features stays
        clash = next((k for k in kept if _corr(arrays[name][rows], arrays[k][rows]) > rule.max_abs_corr), None)
        if clash is None:
            kept.append(name)
        else:
            dropped[name] = f"correlated_with:{clash}"
    while len(kept) > 1:
        from hokma.quant.validation.stat_tests import vif        # imported here: the validation package loads its libraries on import

        matrix = _stack([arrays[n] for n in kept], rows)
        matrix = matrix[np.all(np.isfinite(matrix), axis=1)]
        if len(matrix) <= len(kept):
            break
        scores = vif(matrix, kept)["statistic"]
        worst = max(kept, key=lambda n: (scores[n], n))
        if scores[worst] <= rule.vif_limit:
            break
        dropped[worst] = "vif"
        kept.remove(worst)
    return Selection(tuple(sorted(kept)), dict(sorted(dropped.items())), stats, int(rows.size))


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    keep = np.isfinite(a.reshape(-1)) & np.isfinite(b.reshape(-1))
    if keep.sum() < 3:
        return 0.0
    x, y = a.reshape(-1)[keep], b.reshape(-1)[keep]
    if x.std() == 0 or y.std() == 0:
        return 0.0
    return abs(float(np.corrcoef(x, y)[0, 1]))
