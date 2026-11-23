"""Earnings reaction score, 0 to 100, over daily closed bars (SPEC-019: CTS-002).

Five factors measured at the earnings reaction session (the first session that trades on the results, given by the caller):
the size of the gap, the trend of the 20 sessions before, the volume of 20 sessions against 60, and the position of the close
against the 200 and the 50 session averages. Each factor is scored 0 to 100 by a linear ramp between a floor and a cap, and the
score is the weighted mean. The result feeds the PEAD screen as a rank, not as a decision.

The weights are not validated: until a backtest measures them (H4) every result carries the warning weights_unvalidated. The
bars come from the data module of the desk (Trading Core or FinnHub); this module reads no provider.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.indicators._common import build, empty
from hokma.quant.indicators.screens import _base
from hokma.quant.types import QuantResult

_PIECE = ("CTS-002",)
_NAME = "indicators.screens.earnings_reaction"
WEIGHTS = {"gap": 0.25, "prior_trend": 0.25, "volume": 0.20, "ma200": 0.15, "ma50": 0.15}
WEIGHTS_VALIDATED = False
# (floor, cap): the factor scores 0 at the floor and 100 at the cap.
RAMPS = {"gap": (0.0, 0.10), "prior_trend": (0.0, 0.10), "volume": (0.8, 1.5), "ma200": (0.0, 0.20), "ma50": (0.0, 0.10)}
MIN_SESSIONS = 201


def ramp(value: float, floor: float, cap: float) -> float:
    return float(100.0 * min(max((value - floor) / (cap - floor), 0.0), 1.0))


def factors_at(frame: pd.DataFrame, r: int) -> dict[str, float]:
    """The five raw measures at the reaction position r (bars after r are never read)."""
    close, volume, op = frame["close"], frame["volume"], frame["open"]
    return {
        "gap": float(op.iloc[r] / close.iloc[r - 1] - 1.0),
        "prior_trend": float(close.iloc[r - 1] / close.iloc[r - 21] - 1.0),
        "volume": float(volume.iloc[r - 19:r + 1].mean() / volume.iloc[r - 59:r + 1].mean()),
        "ma200": float(close.iloc[r] / close.iloc[r - 199:r + 1].mean() - 1.0),
        "ma50": float(close.iloc[r] / close.iloc[r - 49:r + 1].mean() - 1.0),
    }


@registry.register(_NAME, module="indicators",
                   summary="Earnings reaction score 0 to 100 from five factors (gap size, prior trend, volume 20 against 60 sessions, close against the 200 and 50 session averages) at the reaction session. Weights are unvalidated and every result says so (weights_unvalidated). Feeds the PEAD screen.",
                   inputs={"bars": {"type": "dataframe", "required": True, "columns": ["open", "close", "volume"]},
                           "reaction_date": {"type": "str", "required": True}, "as_of": {"type": "str", "required": False},
                           "weights": {"type": "dict", "required": False}}, piece_ids=_PIECE)
def earnings_reaction(bars: Any, reaction_date: Any, as_of: Any = None, weights: dict | None = None) -> QuantResult:
    day = _base.as_date(reaction_date)
    if weights is not None and not isinstance(weights, dict):
        raise _base.BadInput("weights must be a dict")
    params = {"reaction_date": _base.day_text(day), "weights": dict(weights) if weights else dict(WEIGHTS)}
    use = dict(WEIGHTS)
    if weights is not None:
        unknown = set(weights) - set(WEIGHTS)
        if unknown:
            raise _base.BadInput(f"unknown weight(s) {sorted(unknown)}")
        use.update({k: v for k, v in weights.items()})
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not np.isfinite(v) or v < 0 for v in use.values()) or sum(use.values()) <= 0:
        return empty(_NAME, "score", "earnings_reaction", "weights must be non negative numbers with a positive sum", params=params, piece_ids=_PIECE)
    frame, dropped, warns = _base.daily_closed(bars, ("open", "high", "low", "close", "volume"), as_of=as_of, min_bars=MIN_SESSIONS)
    params["dropped_open_bars"] = dropped
    if frame is None:
        return empty(_NAME, "score", "earnings_reaction", warns + (f"not available: the score needs {MIN_SESSIONS} closed sessions of valid bars",),
                     params=params, piece_ids=_PIECE)
    idx = frame.index.tz_localize(None).normalize() if frame.index.tz is not None else frame.index.normalize()
    hits = np.flatnonzero(idx == day)
    if hits.size == 0:
        return empty(_NAME, "score", "earnings_reaction", warns + ("reaction_date is not a closed session of the bars",), params=params, piece_ids=_PIECE)
    r = int(hits[0])
    if r < MIN_SESSIONS - 1:
        return empty(_NAME, "score", "earnings_reaction", warns + (f"need {MIN_SESSIONS - 1} sessions before the reaction, got {r}",), params=params, piece_ids=_PIECE)
    raw = factors_at(frame, r)
    scores = {k: ramp(raw[k], *RAMPS[k]) for k in raw}
    total = sum(use.values())
    score = sum(scores[k] * use[k] for k in scores) / total
    value = {"reaction_date": _base.day_text(frame.index[r]), "score": float(score), "factors": scores, "measures": raw,
             "weights": {k: use[k] / total for k in use}, "weights_validated": WEIGHTS_VALIDATED, "feeds": "indicators.screens.pead"}
    return build(_NAME, value, unit="score", method="earnings_reaction", params=params, piece_ids=_PIECE, inputs=(frame,), as_of_source=frame,
                 warnings=warns + ("weights_unvalidated: the weights are not set by a backtest",))
