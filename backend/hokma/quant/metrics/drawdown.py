"""Drawdown series, maximum drawdown and drawdown episodes (SPEC-017, metrics module)."""
from __future__ import annotations

import pandas as pd

from hokma.quant.metrics import _lib
from hokma.quant.metrics.returns import metric


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
