"""L-57: sequential count (setup to 9, countdown to 13). Hand computed counts on closes written below, both sides, causal."""
from __future__ import annotations

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.indicators import catalog, td_sequential

from .helpers import stamp


def bars_of(closes) -> pd.DataFrame:
    """open = close, high = close + 1, low = close - 1 (any jump stays inside the bar rules)."""
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": close, "high": close + 1.0, "low": close - 1.0, "close": close, "volume": 1000.0},
                        index=pd.bdate_range("2024-01-02", periods=len(close)))


def counts(closes, as_of_bar=None):
    bars = bars_of(closes)
    res = td_sequential.td_sequential(bars, last_n=None, as_of=stamp(bars, len(closes) - 1 if as_of_bar is None else as_of_bar))
    assert res.ok, res.warnings
    return {k: [0.0 if v is None else v for v in vals] for k, vals in res.value["values"].items()}


def test_a_straight_decline_by_hand():
    out = counts([100 - t for t in range(25)])
    # every close is below the close four bars earlier from bar 4 on: the setup runs 1 to 9 at bars 4 to 12, starts again at 1 on bar 13,
    # completes again at bar 21 and starts a third at bar 22
    assert out["buy_setup"] == [0.0] * 4 + list(map(float, range(1, 10))) * 2 + [1.0, 2.0, 3.0]
    assert out["sell_setup"] == [0.0] * 25 and out["sell_countdown"] == [0.0] * 25
    # perfected: the lowest low of bars 8 and 9 is at or below the lowest low of bars 6 and 7 (a falling market always satisfies it)
    assert [t for t, v in enumerate(out["buy_perfected"]) if v] == [12, 21]
    # the countdown starts on the bar that completes the setup (bar 12) and every bar qualifies: 1 to 13 at bars 12 to 24
    assert out["buy_countdown"] == [0.0] * 12 + list(map(float, range(1, 14)))


def test_a_not_perfected_setup():
    closes = [100, 98, 96, 94, 92, 90, 88, 86, 84, 82, 80, 81, 83]
    out = counts(closes)
    # the setup completes on bar 12 (83 is below the close of bar 8, 84). Lows are close - 1: bars 11 and 12 have lows 80 and 82, bars 9 and 10 have 81 and 79,
    # so the lowest low of bars 8 and 9 of the setup (80) is above the lowest of bars 6 and 7 (79): not perfected
    assert out["buy_setup"][12] == 9.0 and out["buy_perfected"][12] == 0.0 and out["buy_perfected"] == [0.0] * 13


def test_the_thirteenth_count_waits_for_its_qualifier():
    closes = [100 - t for t in range(20)] + [100, 110, 105, 98, 90, 85, 83, 83.5, 80.5]
    out = counts(closes)
    # bars 12 to 19 count 1 to 8 (the 8th close is 81). Bars 20 to 22 do not count (a rally above the lows of two bars before).
    # Bars 23 to 26 count 9 to 12. Bar 27 (close 83.5, low 82.5) is at or below the low of two bars before (84) but its low is above the close of the
    # 8th count bar (81): the count stays 12. Bar 28 (close 80.5, low 79.5) meets both: 13.
    expected = [0.0] * 12 + [1, 2, 3, 4, 5, 6, 7, 8] + [8, 8, 8] + [9, 10, 11, 12, 12, 13]
    assert out["buy_countdown"] == [float(x) for x in expected]
    assert max(out["sell_setup"]) < 9 and max(out["buy_setup"]) < 10


def test_the_setup_of_the_opposite_side_cancels_the_countdown():
    closes = [100 - t for t in range(13)] + [100 + 2 * (t - 13) for t in range(13, 22)]
    out = counts(closes)
    assert out["buy_setup"][12] == 9.0 and out["buy_countdown"][12] == 1.0
    assert out["sell_setup"][13:] == [float(k) for k in range(1, 10)] and out["sell_setup"][:13] == [0.0] * 13
    # the rise gives no new count (a close below the low of two bars before is needed): the buy count stays at 1 until the sell setup completes on bar 21
    assert out["buy_countdown"] == [0.0] * 12 + [1.0] * 9 + [0.0]
    assert out["sell_countdown"][21] == 1.0 and out["sell_perfected"][21] == 1.0 and out["buy_countdown"][21] == 0.0


def test_the_sell_side_is_the_mirror_of_the_buy_side():
    closes = [100 - t for t in range(20)] + [100, 110, 105, 98, 90, 85, 83, 83.5, 80.5]
    mirror = [200 - c for c in closes]
    buy, sell = counts(closes), counts(mirror)
    assert sell["sell_countdown"] == buy["buy_countdown"] and sell["sell_setup"] == buy["buy_setup"] and sell["sell_perfected"] == buy["buy_perfected"]
    assert sell["buy_countdown"] == buy["sell_countdown"] and sell["buy_setup"] == buy["sell_setup"]


def test_the_count_of_a_bar_never_depends_on_later_bars():
    closes = [100 - t for t in range(20)] + [100, 110, 105, 98, 90, 85, 83, 83.5, 80.5]
    full = counts(closes)
    for k in (5, 13, 21, 27):
        part = counts(closes[:k])
        assert all(part[key] == full[key][:k] for key in full), k


def test_the_open_bar_is_not_counted_and_the_indicator_is_registered_with_its_lag():
    closes = [100 - t for t in range(14)]
    out = counts(closes, as_of_bar=12)                                  # the bar at position 13 is not closed at that moment
    assert len(out["buy_setup"]) == 13 and out["buy_setup"][-1] == 9.0
    spec = registry.describe("indicators.td_sequential")
    assert spec.piece_ids == ("L-57",) and catalog.lag_of("indicators.td_sequential") == 0.0 and spec.summary.endswith("Lag: 0 bars.")
    assert td_sequential.td_sequential(bars_of(closes[:4]), as_of=stamp(bars_of(closes[:4]), 3)).value is None      # fewer than five bars
