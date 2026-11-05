"""D9: the opening high, the line the chart draws from real bars."""
from datetime import datetime, timezone

import pytest

from hokma.market.models import Candle
from hokma.market.opening import opening_range, supports
from hokma.market.tz import NEW_YORK, zone
from hokma.market.yahoo import parse_chart
from market_support import fixture_json


def bars(start: int, step: int, highs: list[float]) -> list[Candle]:
    return [Candle(t=start + i * step, o=h - 1, h=h, l=h - 2, c=h - 0.5, v=1) for i, h in enumerate(highs)]


def ny(year, month, day, hour, minute) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=NEW_YORK).timestamp())


def test_new_york_offsets_follow_us_daylight_saving():
    assert datetime(2026, 3, 8, 1, 59, tzinfo=NEW_YORK).utcoffset().total_seconds() == -5 * 3600
    assert datetime(2026, 3, 8, 3, 0, tzinfo=NEW_YORK).utcoffset().total_seconds() == -4 * 3600
    assert datetime(2026, 11, 1, 1, 0, tzinfo=NEW_YORK).utcoffset().total_seconds() == -4 * 3600
    assert datetime(2026, 11, 1, 3, 0, tzinfo=NEW_YORK).utcoffset().total_seconds() == -5 * 3600
    # 09:30 New York is 14:30 UTC in winter and 13:30 UTC in summer
    assert datetime.fromtimestamp(ny(2026, 1, 15, 9, 30), timezone.utc).hour == 14
    assert datetime.fromtimestamp(ny(2026, 7, 15, 9, 30), timezone.utc).hour == 13


def test_zone_falls_back_to_the_reported_offset_for_unknown_names():
    assert zone("Nowhere/Land", 3600).utcoffset(None).total_seconds() == 3600


@pytest.mark.parametrize("seconds,ok", [(60, True), (300, True), (900, True), (1800, True), (3600, False), (86400, False), (420, False)])
def test_only_timeframes_that_fit_the_window_are_supported(seconds, ok):
    assert supports(seconds) is ok


def test_equity_opening_high_is_the_max_of_the_first_30_minutes_of_the_last_session():
    start = ny(2026, 11, 4, 9, 30)
    day = bars(start, 300, [10, 11, 15, 12, 13, 14, 99, 98])  # 99 starts at 10:00: outside the window
    previous = bars(ny(2026, 11, 3, 9, 30), 300, [500, 600])
    result = opening_range(previous + day, 300, "equity", NEW_YORK)
    assert result is not None
    assert result.high == 15
    assert result.session_date == "2026-11-04"
    assert (result.start, result.end) == (start, start + 1800)
    assert result.complete is True


def test_opening_window_still_forming_is_marked_incomplete():
    start = ny(2026, 11, 4, 9, 30)
    result = opening_range(bars(start, 300, [10, 12, 11]), 300, "equity", NEW_YORK)
    assert result is not None and result.high == 12 and result.complete is False


def test_futures_use_the_0930_new_york_open_not_the_first_bar_of_the_day():
    overnight = bars(ny(2026, 11, 4, 0, 0), 300, [1000] * 12)  # high 1000 overnight, must be ignored
    cash = bars(ny(2026, 11, 4, 9, 30), 300, [20, 25, 22, 21, 23, 24, 70])
    result = opening_range(overnight + cash, 300, "future", NEW_YORK)
    assert result is not None and result.high == 25
    assert result.start == ny(2026, 11, 4, 9, 30)


def test_futures_before_the_open_use_the_previous_session():
    prev = bars(ny(2026, 11, 3, 9, 30), 300, [30, 31, 32, 33, 34, 35])
    overnight = bars(ny(2026, 11, 4, 2, 0), 300, [5, 6])
    result = opening_range(prev + overnight, 300, "future", NEW_YORK)
    assert result is not None and result.high == 35 and result.session_date == "2026-11-03"


def test_futures_weekend_looks_back_to_friday():
    friday = bars(ny(2026, 11, 6, 9, 30), 300, [40, 44, 41, 42, 43, 41])
    sunday = bars(ny(2026, 11, 8, 18, 0), 300, [1, 2])  # session reopens Sunday evening
    result = opening_range(friday + sunday, 300, "future", NEW_YORK)
    assert result is not None and result.high == 44 and result.session_date == "2026-11-06"


def test_crypto_session_starts_at_midnight_of_its_zone():
    utc = timezone.utc
    midnight = int(datetime(2026, 11, 4, tzinfo=utc).timestamp())
    day = bars(midnight - 3600, 900, [900, 800]) + bars(midnight, 900, [10, 12, 11, 5, 99])
    result = opening_range(day, 900, "crypto", utc)
    assert result is not None and result.high == 12 and result.start == midnight


def test_no_candles_or_unsupported_timeframe_gives_no_line():
    assert opening_range([], 300, "equity", NEW_YORK) is None
    assert opening_range(bars(ny(2026, 11, 4, 9, 30), 3600, [1, 2]), 3600, "equity", NEW_YORK) is None


def test_day_without_bars_in_the_window_gives_no_line():
    only_afternoon = bars(ny(2026, 11, 4, 15, 0), 300, [1, 2, 3])
    assert opening_range(only_afternoon, 300, "future", NEW_YORK) is None


def test_recorded_aapl_session_matches_a_hand_computation_from_the_raw_response():
    raw = fixture_json("aapl_5m.json")["chart"]["result"][0]
    quote = raw["indicators"]["quote"][0]
    start = 1793802600  # 2026-11-04 09:30 New York, written as epoch on purpose
    expected = max(h for t, h in zip(raw["timestamp"], quote["high"]) if start <= t < start + 1800 and h is not None)
    series = parse_chart(fixture_json("aapl_5m.json"), "AAPL", 0, False, "test")
    result = opening_range(series.candles, 300, series.kind, zone(series.timezone, series.utc_offset_seconds))
    assert result is not None
    assert result.high == expected
    assert result.start == start and result.session_date == "2026-11-04" and result.complete
