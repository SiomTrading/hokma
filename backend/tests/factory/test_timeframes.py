"""Higher timeframes without future leakage (SPEC-020: B-30), hand built bars."""
import numpy as np
import pandas as pd
import pytest

from hokma.factory.timeframes import resample_closed
from hokma.quant.errors import BadInput


def minute_bars() -> pd.DataFrame:
    index = pd.date_range("2025-03-03 10:00", periods=51, freq="min", tz="America/New_York")     # 10:00 to 10:50
    close = 100.0 + np.arange(51)
    return pd.DataFrame({"open": close - 0.25, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 1.0}, index=index)


def at(frame: pd.DataFrame, hhmm: str) -> pd.Series:
    return frame.loc[pd.Timestamp(f"2025-03-03 {hhmm}", tz="America/New_York")]


def test_the_15_minute_bar_visible_at_10_37_is_the_one_from_10_15_to_10_30():
    out = resample_closed(minute_bars(), "15min")
    row = at(out, "10:37")
    assert row["htf_start"] == pd.Timestamp("2025-03-03 10:15") and row["htf_end"] == pd.Timestamp("2025-03-03 10:30")
    # minutes 15 to 29 of the hand built series: open of 10:15, highest high, lowest low, close of 10:29, 15 bars of volume
    assert (row["open"], row["high"], row["low"], row["close"], row["volume"]) == (114.75, 129.5, 114.5, 129.0, 15.0)
    assert at(out, "10:44")["htf_start"] == pd.Timestamp("2025-03-03 10:15")       # the 10:30 bar has not closed
    assert at(out, "10:44")["close"] == 129.0


def test_the_next_bar_appears_at_10_45_and_not_before():
    out = resample_closed(minute_bars(), "15min")
    row = at(out, "10:45")
    assert row["htf_start"] == pd.Timestamp("2025-03-03 10:30") and row["close"] == 144.0
    assert (row["open"], row["high"], row["low"], row["volume"]) == (129.75, 144.5, 129.5, 15.0)


def test_before_the_first_close_nothing_exists():
    out = resample_closed(minute_bars(), "15min")
    first = at(out, "10:14")
    assert np.isnan(first["close"]) and pd.isna(first["htf_end"])
    assert at(out, "10:15")["close"] == 114.0                                         # the 10:00 bar closed at 10:15


def test_a_change_after_the_row_never_changes_the_row():
    frame = minute_bars()
    altered = frame.copy()
    altered.iloc[40:, :] = 9999.0                                                     # every bar from 10:40 on is rewritten
    a, b = resample_closed(frame, "15min"), resample_closed(altered, "15min")
    pd.testing.assert_frame_equal(a.iloc[:40], b.iloc[:40])


def test_weekly_bars_start_on_monday():
    index = pd.date_range("2025-03-03", periods=14, freq="D")                           # Monday 3 March for two weeks
    daily = pd.DataFrame({"close": np.arange(1.0, 15.0), "high": np.arange(1.0, 15.0) + 1}, index=index)
    out = resample_closed(daily, "1W")
    assert np.isnan(out["close"].iloc[6])                                              # Sunday: week one is still open
    assert out["close"].iloc[7] == 7.0 and out["high"].iloc[7] == 8.0                 # next Monday sees week one
    assert out["htf_start"].iloc[7] == pd.Timestamp("2025-03-03")


def test_bad_inputs():
    frame = minute_bars()
    with pytest.raises(BadInput):
        resample_closed(frame, "15 minutes")
    with pytest.raises(BadInput):
        resample_closed(frame.iloc[::-1], "15min")
    with pytest.raises(BadInput):
        resample_closed(frame.drop(columns="close"), "15min")
    with pytest.raises(BadInput):
        resample_closed(frame.reset_index(), "15min")
