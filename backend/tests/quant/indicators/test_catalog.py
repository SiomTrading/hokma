"""TA-74: the catalog of 13 technical indicators with their use and cautions."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators import catalog, momentum, overlap, volatility

from .helpers import latest, tiny

NAMES = ["close_50_sma", "close_200_sma", "close_10_ema", "macd", "macds", "macdh", "rsi", "boll", "boll_ub", "boll_lb", "atr", "vwma", "mfi"]


def test_the_catalog_has_13_indicators_each_with_use_and_caution():
    assert sorted(catalog.CATALOG) == sorted(NAMES) and len(catalog.CATALOG) == 13
    for entry in catalog.CATALOG.values():
        assert len(entry.use) > 20 and len(entry.caution) > 20


def test_quant_describe_carries_the_catalog():
    info = registry.describe("indicators.catalog_value").describe()
    choices = info["inputs"]["indicator"]["choices"]
    assert sorted(choices) == sorted(NAMES)
    assert all("Caution:" in text for text in choices.values())
    assert info["piece_ids"] == ["TA-74"]


def _value(result) -> float:
    assert result.ok, result.warnings
    return result.value["value"]


def test_values_by_hand_with_the_dt_short_windows():
    bars = tiny()
    closes = list(bars["close"])
    ema = sum(closes[:5]) / 5                                   # DT halves the 10 bar window to 5: seeded with the first 5 closes
    for price in closes[5:]:
        ema += (price - ema) / 3                                # alpha = 2 / (5 + 1)
    assert _value(catalog.catalog_value(bars, "close_10_ema", line="DT")) == pytest.approx(ema)
    assert _value(catalog.catalog_value(bars, "boll", line="DT")) == pytest.approx(13.0)         # mean of the ten closes
    assert _value(catalog.catalog_value(bars, "atr", line="DT")) == pytest.approx(3.0)
    assert catalog.catalog_value(bars, "boll", line="DT").params["window"] == 10


def test_a_window_longer_than_the_bars_gives_no_value():
    res = catalog.catalog_value(tiny(), "close_50_sma", line="DT")
    assert res.value is None and res.params["window"] == 25 and res.warnings


def test_each_name_equals_its_indicator_on_the_fixed_bars(spy):
    macd = latest(momentum.macd(spy))
    boll = latest(volatility.bollinger(spy))
    expected = {
        "close_50_sma": latest(overlap.sma(spy, window=50), "sma"), "close_200_sma": latest(overlap.sma(spy, window=200), "sma"),
        "close_10_ema": latest(overlap.ema(spy, window=10), "ema"), "macd": macd["macd"], "macds": macd["signal"], "macdh": macd["histogram"],
        "rsi": latest(momentum.rsi(spy), "rsi"), "boll": boll["middle"], "boll_ub": boll["upper"], "boll_lb": boll["lower"],
        "atr": latest(volatility.atr(spy), "atr"), "vwma": latest(overlap.vwma(spy), "vwma"), "mfi": latest(momentum.mfi(spy), "mfi"),
    }
    for name in NAMES:
        res = catalog.catalog_value(spy, name)
        assert _value(res) == pytest.approx(expected[name], abs=1e-8), name
        assert res.value["use"] and res.value["caution"] and res.piece_ids == ("TA-74",)


def test_lines_scale_the_catalog_windows(spy):
    assert catalog.catalog_value(spy, "close_50_sma", line="DT").params["window"] == 25
    assert catalog.catalog_value(spy, "close_200_sma", line="LP").params["window"] == 400
    assert catalog.catalog_value(spy, "close_50_sma", line="SWC").params["window"] == 50
    assert catalog.catalog_value(spy, "rsi", line="SWO").params["window"] == 14


def test_bad_name_empty_nan_and_out_of_domain(spy):
    assert catalog.catalog_value(spy, "stochastic").value is None
    empty = pd.DataFrame({c: [] for c in ("open", "high", "low", "close", "volume")}, dtype=float)
    assert catalog.catalog_value(empty, "rsi").value is None
    nan_bars = spy.copy()
    nan_bars.iloc[10, nan_bars.columns.get_loc("close")] = np.nan
    assert catalog.catalog_value(nan_bars, "macd").value is None
    with pytest.raises(BadInput):
        catalog.catalog_value(spy, "rsi", line="XX")
    with pytest.raises(BadInput):
        catalog.catalog_value([1, 2, 3], "rsi")


def test_call_through_the_registry(spy):
    res = registry.call("indicators.catalog_value", bars=spy, indicator="rsi")
    assert 0.0 < res.value["value"] < 100.0
    with pytest.raises(BadInput):
        registry.call("indicators.catalog_value", bars=spy)
