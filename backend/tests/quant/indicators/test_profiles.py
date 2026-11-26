"""L-57: volume profile, market profile and time profile on small hand computed bars, causal, each registered with its lag."""
from __future__ import annotations

import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators import catalog, profile

PIECE = ("L-57",)
NAMES = ("indicators.volume_profile", "indicators.market_profile", "indicators.time_profile")


def two_bars() -> pd.DataFrame:
    """Bar 1 trades 100 to 102 on 200 contracts, bar 2 trades 101 to 103 on 100 contracts."""
    return pd.DataFrame({"high": [102.0, 103.0], "low": [100.0, 101.0], "volume": [200.0, 100.0]}, index=pd.bdate_range("2024-01-02", periods=2))


def test_volume_profile_by_hand():
    res = profile.registered_volume_profile(two_bars(), window=2, bins=3)
    v = res.value
    # three bins of width 1 between 100 and 103: bar 1 puts 100 in each of the first two, bar 2 puts 50 in each of the last two
    assert v["edges"] == [100.0, 101.0, 102.0, 103.0] and v["volume"] == [100.0, 150.0, 50.0] and v["bars"] == 2
    assert v["poc"] == 101.5                                      # the bin with 150
    # 70 percent of 300 is 210: the POC holds 150; the pair above holds 50 (one bin), the pair below holds 100: the lower bin is added (250 >= 210)
    assert (v["val"], v["vah"]) == (100.0, 102.0)
    assert res.piece_ids == PIECE and sum(v["volume"]) == pytest.approx(300.0)


def test_volume_profile_value_area_widens_to_the_wanted_share():
    v = profile.registered_volume_profile(two_bars(), window=2, bins=3, value_area_share=1.0).value
    assert (v["val"], v["vah"]) == (100.0, 103.0)                 # all the volume needs every bin
    v = profile.registered_volume_profile(two_bars(), window=2, bins=3, value_area_share=0.4).value
    assert (v["val"], v["vah"]) == (101.0, 102.0)                 # 120 is below the 150 of the POC alone


def test_a_flat_bar_puts_all_its_volume_in_its_bin():
    bars = pd.DataFrame({"high": [102.0, 103.0, 101.0], "low": [100.0, 101.0, 101.0], "volume": [200.0, 100.0, 60.0]}, index=pd.bdate_range("2024-01-02", periods=3))
    v = profile.registered_volume_profile(bars, window=3, bins=3).value
    assert v["volume"] == [100.0, 210.0, 50.0]                    # the 60 at 101 goes to the middle bin [101, 102)


def test_only_the_last_window_bars_are_profiled():
    bars = pd.DataFrame({"high": [200.0, 102.0, 103.0], "low": [190.0, 100.0, 101.0], "volume": [999.0, 200.0, 100.0]}, index=pd.bdate_range("2024-01-02", periods=3))
    v = profile.registered_volume_profile(bars, window=2, bins=3).value
    assert v["edges"][0] == 100.0 and v["volume"] == [100.0, 150.0, 50.0]
    assert profile.registered_volume_profile(bars, window=10, bins=3).value["bars"] == 3         # fewer bars than the window: all of them


def test_a_candle_marked_open_is_not_in_the_profile():
    bars = two_bars()
    extra = pd.DataFrame({"high": [150.0], "low": [140.0], "volume": [500.0]}, index=[bars.index[-1] + pd.Timedelta(days=1)])
    stacked = pd.concat([bars, extra]).assign(closed=[True, True, False])
    res = profile.registered_volume_profile(stacked, window=3, bins=3)
    assert res.value["volume"] == [100.0, 150.0, 50.0] and res.params["dropped_open_bars"] == 1


def test_market_profile_by_hand():
    v = profile.registered_market_profile(two_bars(), window=2, bins=3).value
    # a bar counts one TPO in every bin its range touches: bar 1 touches all three bins (its high 102 starts the last), bar 2 the last two
    assert v["tpo"] == [1.0, 2.0, 2.0] and v["poc"] == 101.5      # a tie between the two upper bins goes to the one nearest the middle (101.5)
    # 70 percent of 5 is 3.5: the POC holds 2; the bin above holds 2 and the one below 1, so the upper bin is added
    assert (v["val"], v["vah"]) == (101.0, 103.0)


def test_a_profile_of_a_window_without_range_or_volume_gives_no_value():
    flat = pd.DataFrame({"high": [100.0, 100.0], "low": [100.0, 100.0], "volume": [10.0, 10.0]}, index=pd.bdate_range("2024-01-02", periods=2))
    assert profile.registered_volume_profile(flat, window=2, bins=3).value is None
    assert profile.registered_volume_profile(two_bars().assign(volume=0.0), window=2, bins=3).value is None
    assert profile.registered_volume_profile(two_bars(), window=2, bins=1).value is None
    assert profile.registered_volume_profile(two_bars(), window=2, bins=3, value_area_share=1.5).value is None
    with pytest.raises(BadInput):
        profile.registered_volume_profile(two_bars(), window=2, bins=profile.MAX_BINS + 1)


def intraday() -> pd.DataFrame:
    """Two sessions of bars at 09:30, 10:00 and 10:30 with the volumes written below."""
    times = ["2024-01-02 09:30", "2024-01-02 10:00", "2024-01-02 10:30", "2024-01-03 09:30", "2024-01-03 10:00", "2024-01-03 10:30"]
    return pd.DataFrame({"volume": [100.0, 200.0, 300.0, 150.0, 100.0, 100.0]}, index=pd.DatetimeIndex(times))


def test_time_profile_by_hand():
    res = profile.registered_time_profile(intraday(), window=2, slot_minutes=60)
    v = res.value
    # hour slots: 09:30 falls in 09:00 (100 + 150); 10:00 and 10:30 fall in 10:00 (200 + 300 + 100 + 100)
    assert [s["slot"] for s in v["slots"]] == ["09:00", "10:00"] and [s["volume"] for s in v["slots"]] == [250.0, 700.0]
    assert [s["bars"] for s in v["slots"]] == [2, 4] and v["peak_slot"] == "10:00" and v["sessions"] == 2 and v["bars"] == 6
    assert v["slots"][0]["share"] == pytest.approx(250 / 950) and v["peak_share"] == pytest.approx(700 / 950)
    last = profile.registered_time_profile(intraday(), window=1, slot_minutes=60).value          # the last session only
    assert [s["volume"] for s in last["slots"]] == [150.0, 200.0] and last["sessions"] == 1
    half = profile.registered_time_profile(intraday(), window=2, slot_minutes=30).value
    assert [s["slot"] for s in half["slots"]] == ["09:30", "10:00", "10:30"] and [s["volume"] for s in half["slots"]] == [250.0, 300.0, 400.0]
    assert half["peak_slot"] == "10:30"


def test_time_profile_refuses_daily_bars():
    res = profile.registered_time_profile(two_bars(), window=2, slot_minutes=30)
    assert res.value is None and "intraday" in " ".join(res.warnings)
    assert profile.registered_time_profile(intraday(), window=2, slot_minutes=0).value is None


def test_each_profile_is_registered_with_its_lag():
    registered = {s.name: s for s in registry.list("indicators")}
    for full in NAMES:
        assert full in registered and registered[full].piece_ids == PIECE and catalog.lag_of(full) == 0.0
        assert registered[full].summary.endswith("Lag: 0 bars.") and "last_n" not in registered[full].inputs
    res = registry.call("indicators.volume_profile", bars=two_bars(), window=2, bins=3)
    assert res.value["poc"] == 101.5
    dt = registry.call("indicators.volume_profile", bars=two_bars(), line="DT")
    assert dt.params["window"] == 10                                    # the line halves the default window of 20
