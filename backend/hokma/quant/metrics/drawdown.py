"""Drawdown series, maximum drawdown and drawdown episodes (SPEC-017, metrics module)."""
from __future__ import annotations

import pandas as pd

from hokma.quant.metrics import _lib
from hokma.quant import registry
from hokma.quant.metrics.returns import metric
from hokma.quant.metrics.summary import drawdown_episodes, empty, finish, prepare_series, underwater
from hokma.quant.types import QuantResult


def _episodes(frame: pd.DataFrame) -> list[dict]:
    return [{str(k): v for k, v in row.items()} for row in frame.to_dict("records")]


max_drawdown = metric(
    "metrics.max_drawdown", piece_ids=("QS-030", "FF-005"), unit="ratio", method="minimum of wealth / running peak - 1",
    summary="Largest peak-to-trough loss of the compounded return path (a negative number).",
    compute=lambda s, b, p: _lib.qstats.max_drawdown(s))

drawdown_series = metric(
    "metrics.drawdown_series", piece_ids=("QS-030",), unit="ratio", method="wealth / running peak - 1 at every date",
    summary="Drawdown at every date of the compounded return path.",
    compute=lambda s, b, p: _lib.qstats.to_drawdown_series(s))

drawdown_details = metric(
    "metrics.drawdown_details", piece_ids=("QS-036",), unit="table", method="every episode from peak to recovery: start, valley, end, days, depth",
    summary="Drawdown episodes with start, valley, end, length in days and depth.",
    compute=lambda s, b, p: _episodes(_lib.qstats.drawdown_details(_lib.qstats.to_drawdown_series(s))))

drawdown_series_from_prices = metric(
    "metrics.drawdown_series_from_prices", input_label="prices", piece_ids=("FF-005",), unit="ratio", method="price / running peak - 1",
    summary="Drawdown at every date of a price series.",
    compute=lambda s, b, p: _lib.ffn.to_drawdown_series(s))

drawdown_details_from_prices = metric(
    "metrics.drawdown_details_from_prices", input_label="prices", piece_ids=("FF-005",), unit="table", method="every episode of the price drawdown: start, end, length, depth",
    summary="Drawdown episodes of a price series with start, end, length and depth.",
    compute=lambda s, b, p: _episodes(_lib.ffn.drawdown_details(_lib.ffn.to_drawdown_series(s))))

max_drawdown_from_prices = metric(
    "metrics.max_drawdown_from_prices", input_label="prices", piece_ids=("FF-005",), unit="ratio", method="minimum of price / running peak - 1",
    summary="Largest peak-to-trough loss of a price series (a negative number).",
    compute=lambda s, b, p: _lib.ffn.calc_max_drawdown(s))


# --------------------------------------------------------------------------------------------------------------
# B-20, V-20, L-31: drawdown episodes as objects and drawdown duration. One drawdown formula: the episodes come from
# summary.drawdown_episodes (the compounded path of QS-030); this file only adds the dates of the peak and of the recovery.
# --------------------------------------------------------------------------------------------------------------
def episode_objects(r: pd.Series) -> list[dict]:
    """Every underwater episode with its peak, first underwater date, valley, recovery date and depth.

    The peak is the date before the first underwater date (the first date when the series starts underwater). An episode that has not
    recovered by the last date is open: `recovery` is None and its days run to the last date. Days are calendar days from the peak.
    """
    dd = underwater(r)
    index, last = dd.index, len(dd) - 1
    out = []
    for row in drawdown_episodes(r).to_dict("records"):
        first, end = index.get_loc(row["start"]), index.get_loc(row["end"])
        is_open = end == last and dd.iloc[end] < 0
        peak = index[max(first - 1, 0)]
        recovery = None if is_open else index[end + 1]
        out.append({"peak": peak, "start": row["start"], "valley": row["valley"], "recovery": recovery, "open": bool(is_open),
                    "days": int(((index[last] if is_open else recovery) - peak).days), "depth": float(row["depth"])})
    return out


@registry.register("metrics.drawdown_episodes", module="metrics",
                   summary="Drawdown episodes as objects: peak, start, valley, recovery date (None while open), days from the peak and depth.",
                   inputs={"returns": {"type": "series", "required": True}}, piece_ids=("V-20", "L-31"))
def drawdown_episode_objects(returns) -> QuantResult:
    name, pieces, method = "metrics.drawdown_episodes", ("V-20", "L-31"), "episodes of wealth / running peak - 1; an episode with no recovery stays open"
    r, warns = prepare_series(returns, min_obs=1, allow_constant=True)
    if r is None:
        return empty(name, "table", method, warns, pieces)
    return finish(name, episode_objects(r), unit="table", method=method, series=r, pieces=pieces, warnings=warns)


@registry.register("metrics.drawdown_duration", module="metrics",
                   summary="Drawdown duration in calendar days: longest recovery (peak to recovery), longest time under water including an open episode, current time under water and the peak dates.",
                   inputs={"returns": {"type": "series", "required": True}}, piece_ids=("B-20", "L-31"))
def drawdown_duration(returns) -> QuantResult:
    name, pieces, method = "metrics.drawdown_duration", ("B-20", "L-31"), "calendar days from each peak to its recovery date, or to the last date while open"
    r, warns = prepare_series(returns, min_obs=1, allow_constant=True)
    if r is None:
        return empty(name, "days", method, warns, pieces)
    episodes = episode_objects(r)
    recovered = [e["days"] for e in episodes if not e["open"]]
    current = next((e["days"] for e in episodes if e["open"]), 0)
    value = {"max_recovery_days": max(recovered, default=0), "max_underwater_days": max((e["days"] for e in episodes), default=0),
             "current_underwater_days": current, "n_episodes": len(episodes), "peaks": [e["peak"] for e in episodes]}
    return finish(name, value, unit="days", method=method, series=r, pieces=pieces, warnings=warns)
