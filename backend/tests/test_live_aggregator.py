"""M8: the bars in progress are built right from ticks (checked against a plain reference calculation), from 1m bar
updates, and with the time zone and the session of the symbol."""
import random
from datetime import datetime, timedelta, timezone

import pytest

from hokma.market import opening
from hokma.market.aggregator import BarAggregator
from hokma.market.intervals import IntervalError, parse_time
from hokma.market.models import OPENING_RANGE_MINUTES, TIMEFRAME_SECONDS
from hokma.market.stream import BarUpdate
from hokma.market.tz import NEW_YORK

OPEN = int(datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc).timestamp())      # 09:30 in New York (EST)


def ticks(seconds: int, seed: int = 7):
    rng = random.Random(seed)
    price = 28000.0
    for i in range(seconds):
        price += rng.choice((-0.25, 0.25, 0.5, -0.5, 0.0))
        yield (OPEN + i) * 1000, price, float(rng.randint(1, 5))


def reference(items, seconds: int, anchor: int = 0) -> dict[int, tuple]:
    """Plain calculation: bars of `seconds` counted from `anchor`."""
    bars: dict[int, list] = {}
    for ts_ms, price, size in items:
        start = anchor + (ts_ms // 1000 - anchor) // seconds * seconds
        bar = bars.setdefault(start, [price, price, price, price, 0.0])
        bar[1], bar[2], bar[3] = max(bar[1], price), min(bar[2], price), price
        bar[4] += size
    return {t: tuple(b) for t, b in bars.items()}


def test_1m_5m_15m_and_1h_bars_from_one_second_ticks_match_the_reference():
    items = list(ticks(3600))
    aggregator = BarAggregator(NEW_YORK, "future")
    last: dict[tuple[str, int], tuple] = {}
    for ts_ms, price, size in items:
        for timeframe, candle, final in aggregator.on_tick(ts_ms, price, size):
            last[(timeframe, candle.t)] = (candle.o, candle.h, candle.l, candle.c, candle.v)
    for timeframe in ("1m", "5m", "15m", "1h"):
        expected = reference(items, TIMEFRAME_SECONDS[timeframe])
        got = {t: v for (tf, t), v in last.items() if tf == timeframe}
        assert got == expected, timeframe


def test_each_bar_is_final_once_and_only_when_the_next_one_starts():
    aggregator = BarAggregator(NEW_YORK, "future")
    finals = []
    for ts_ms, price, size in ticks(900):
        finals += [(tf, c.t) for tf, c, final in aggregator.on_tick(ts_ms, price, size) if final]
    five = [t for tf, t in finals if tf == "5m"]
    assert five == [OPEN, OPEN + 300]                         # the third bar is still running
    assert len(set(finals)) == len(finals)


def test_the_opening_high_matches_the_reference_after_the_window():
    items = list(ticks(45 * 60))
    aggregator = BarAggregator(NEW_YORK, "future")
    candles = {}
    for ts_ms, price, size in items:
        for timeframe, candle, final in aggregator.on_tick(ts_ms, price, size):
            if timeframe == "5m":
                candles[candle.t] = candle
    found = opening.opening_range(sorted(candles.values(), key=lambda c: c.t), 300, "future", NEW_YORK)
    window = [p for ts_ms, p, _ in items if OPEN <= ts_ms // 1000 < OPEN + OPENING_RANGE_MINUTES * 60]
    assert found is not None and found.high == max(window) and found.complete


def test_the_opening_high_is_recomputed_while_the_window_runs():
    items = list(ticks(20 * 60))
    aggregator = BarAggregator(NEW_YORK, "future")
    candles = {}
    seen = []
    for index, (ts_ms, price, size) in enumerate(items):
        for timeframe, candle, final in aggregator.on_tick(ts_ms, price, size):
            if timeframe == "5m":
                candles[candle.t] = candle
        if index % 120 == 119:
            found = opening.opening_range(sorted(candles.values(), key=lambda c: c.t), 300, "future", NEW_YORK)
            seen.append((found.high, max(p for _, p, _ in items[:index + 1]), found.complete))
    assert all(high == expected and not complete for high, expected, complete in seen)


def test_a_bar_update_replaces_the_minute_and_higher_bars_fold_their_minutes():
    aggregator = BarAggregator(NEW_YORK, "future")
    for i in range(5):
        aggregator.on_bar_update(BarUpdate("NQ", (OPEN + 60 * i) * 1000, 100 + i, 110 + i, 90 + i, 105 + i, 10,
                                           final=True))
    five = aggregator.current("5m")
    assert (five.t, five.o, five.h, five.l, five.c, five.v) == (OPEN, 100, 114, 90, 109, 50)
    again = aggregator.on_bar_update(BarUpdate("NQ", (OPEN + 240) * 1000, 104, 130, 94, 120, 12, final=True))
    assert aggregator.current("5m").h == 130 and aggregator.current("5m").c == 120
    assert all(not final for tf, c, final in again if tf == "5m")


def test_a_late_tick_for_a_bar_that_is_behind_is_ignored():
    aggregator = BarAggregator(NEW_YORK, "future")
    aggregator.on_tick((OPEN + 400) * 1000, 10.0, 1)
    assert [c for tf, c, f in aggregator.on_tick((OPEN + 10) * 1000, 99.0, 1) if tf == "5m"] == []
    assert aggregator.current("5m").h == 10.0


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def test_daily_bars_follow_the_session_of_the_symbol():
    future = BarAggregator(NEW_YORK, "future")
    stock = BarAggregator(NEW_YORK, "equity")
    crypto = BarAggregator(timezone.utc, "crypto")
    forex = BarAggregator(NEW_YORK, "currency")
    # 2026-11-05 14:30 UTC (a Thursday): the futures day started at 18:00 New York the evening before (23:00 UTC), a
    # forex day at 17:00 New York (22:00 UTC), a stock day at the open 09:30 New York (14:30 UTC), a crypto day at
    # midnight UTC.
    assert future.bounds(OPEN, "1D") == (utc(2026, 11, 4, 23), utc(2026, 11, 5, 23))
    assert forex.bounds(OPEN, "1D") == (utc(2026, 11, 4, 22), utc(2026, 11, 5, 22))
    assert stock.bounds(OPEN, "1D") == (utc(2026, 11, 5, 14, 30), utc(2026, 11, 6, 14, 30))
    assert crypto.bounds(OPEN, "1D") == (utc(2026, 11, 5), utc(2026, 11, 6))


def test_the_session_changes_on_the_anchor_not_on_midnight():
    stock = BarAggregator(NEW_YORK, "equity")
    assert stock.bounds(OPEN - 1, "1D")[0] == utc(2026, 11, 4, 14, 30)       # 09:29 is still yesterday's session
    future = BarAggregator(NEW_YORK, "future")
    assert future.bounds(utc(2026, 11, 4, 22, 59), "1D")[0] == utc(2026, 11, 3, 23)   # 17:59 New York
    assert future.bounds(utc(2026, 11, 4, 23), "1D")[0] == utc(2026, 11, 4, 23)       # 18:00 opens the next one


def test_the_session_follows_daylight_saving_time_on_both_sides_of_the_change():
    stock = BarAggregator(NEW_YORK, "equity")
    friday = utc(2027, 3, 12, 15)                              # still EST: the open is 14:30 UTC
    monday = utc(2027, 3, 15, 15)                              # EDT: the open is 13:30 UTC
    assert stock.bounds(friday, "1D")[0] == utc(2027, 3, 12, 14, 30)
    assert stock.bounds(monday, "1D")[0] == utc(2027, 3, 15, 13, 30)
    # the Friday bar holds the weekend and ends where the Monday session opens, one hour earlier on the clock
    assert stock.bounds(friday, "1D")[1] == utc(2027, 3, 15, 13, 30)


# ---- the anchor of each class, for the five original timeframes and any other

ANCHORS = {                                                    # first session of 2026-11-05 as UTC instants
    "future": (NEW_YORK, utc(2026, 11, 4, 23)),
    "currency": (NEW_YORK, utc(2026, 11, 4, 22)),
    "equity": (NEW_YORK, utc(2026, 11, 5, 14, 30)),
    "crypto": (timezone.utc, utc(2026, 11, 5)),
}


@pytest.mark.parametrize("kind", sorted(ANCHORS))
@pytest.mark.parametrize("interval", ["1m", "5m", "15m", "1h", "7m", "45m", "3h", "90s", "4h"])
def test_intraday_bars_count_from_the_anchor_of_the_session(kind, interval):
    tz, anchor = ANCHORS[kind]
    size = parse_time(interval).seconds
    aggregator = BarAggregator(tz, kind, [])
    for offset in (0, 1, size - 1, size, 5 * size + 17, 11 * size + size // 2):
        start, end = aggregator.bounds(anchor + offset, interval)
        assert start == anchor + offset // size * size and end == start + size, (kind, interval, offset)


def test_a_bar_that_does_not_divide_the_session_is_cut_at_the_next_anchor():
    aggregator = BarAggregator(NEW_YORK, "future", [])
    next_anchor = utc(2026, 11, 5, 23)
    start, end = aggregator.bounds(next_anchor - 60, "7m")      # 1440 minutes: the last 7m bar has 5 minutes left
    assert start == ANCHORS["future"][1] + 205 * 420 and end == next_anchor
    assert aggregator.bounds(next_anchor, "7m") == (next_anchor, next_anchor + 420)


def test_stock_hourly_bars_start_at_half_past_the_hour_like_the_open():
    stock = BarAggregator(NEW_YORK, "equity", [])
    assert stock.bounds(OPEN + 3700, "1h")[0] == OPEN + 3600     # 10:30 New York, no longer 10:00


def test_sessions_group_days_and_weeks_counting_from_a_fixed_origin():
    stock = BarAggregator(NEW_YORK, "equity", [])
    monday = utc(2026, 11, 2, 15)
    week = stock.bounds(monday, "1W")
    assert week == (utc(2026, 11, 2, 14, 30), utc(2026, 11, 9, 14, 30))
    assert stock.bounds(utc(2026, 11, 8, 12), "1W") == week            # Sunday belongs to the week that ended
    assert BarAggregator(NEW_YORK, "future", []).bounds(utc(2026, 11, 5, 15), "1W") == (utc(2026, 11, 1, 23),
                                                                                       utc(2026, 11, 8, 23))
    assert BarAggregator(timezone.utc, "crypto", []).bounds(utc(2026, 11, 8, 12), "1W") == (utc(2026, 11, 2),
                                                                                           utc(2026, 11, 9))
    start, end = stock.bounds(monday, "2W")
    assert end - start in (14 * 86400, 14 * 86400 + 3600) and start <= monday < end    # +1 hour when clocks go back


def weekdays_between(start: int, end: int) -> int:
    first, last = datetime.fromtimestamp(start, NEW_YORK).date(), datetime.fromtimestamp(end, NEW_YORK).date()
    return sum(1 for k in range((last - first).days) if (first + timedelta(days=k)).weekday() < 5)


def test_two_session_bars_pair_consecutive_weekday_sessions_and_hold_the_weekend():
    stock = BarAggregator(NEW_YORK, "equity", [])
    days = [datetime(2026, 11, 2, 15, tzinfo=timezone.utc) + timedelta(days=i) for i in range(21)]
    seen: dict[int, list] = {}
    for moment in days:
        start, end = stock.bounds(int(moment.timestamp()), "2D")
        assert start <= int(moment.timestamp()) < end
        seen.setdefault(start, []).append((moment.weekday(), end))
    sessions = sorted(seen)
    ends = [seen[start][0][1] for start in sessions]
    assert sessions[1:] == ends[:-1]                           # bars touch: each one ends where the next opens
    for start, end in zip(sessions, ends):
        first = datetime.fromtimestamp(start, NEW_YORK)
        assert first.weekday() < 5 and (first.hour, first.minute) == (9, 30)
        weekdays = weekdays_between(start, end)
        assert weekdays == 2                                   # two sessions, whatever the weekend adds
    crypto = BarAggregator(timezone.utc, "crypto", [])
    assert all(crypto.bounds(utc(2026, 11, 2, 12) + i * 86400, "2D")[1] - crypto.bounds(utc(2026, 11, 2, 12) + i * 86400, "2D")[0]
               == 2 * 86400 for i in range(10))                # crypto trades every day


# ---- the list of intervals

def test_only_the_intervals_with_a_subscription_are_built():
    aggregator = BarAggregator(NEW_YORK, "future", ["7m"])
    assert aggregator.intervals == ("7m",)
    changed = {tf for tf, _, _ in aggregator.on_tick(OPEN * 1000, 100.0, 1)}
    assert changed == {"7m"} and aggregator.current("5m") is None and aggregator.current("7m").o == 100.0


def test_by_default_the_five_original_timeframes_are_built():
    assert BarAggregator(NEW_YORK, "future").intervals == ("1m", "5m", "15m", "1h", "1D")


def test_an_interval_can_be_added_and_removed_while_ticks_arrive():
    aggregator = BarAggregator(NEW_YORK, "future", ["1m"])
    items = list(ticks(900))
    for ts_ms, price, size in items[:400]:
        aggregator.on_tick(ts_ms, price, size)
    assert aggregator.add_interval("5m") and not aggregator.add_interval("5m")
    seeded = aggregator.current("5m")                          # starts from the minutes already seen, not from scratch
    expected = reference(items[:400], 300)[seeded.t]
    assert (seeded.o, seeded.h, seeded.l, seeded.c, seeded.v) == expected
    for ts_ms, price, size in items[400:]:
        aggregator.on_tick(ts_ms, price, size)
    assert aggregator.current("5m").v == reference(items, 300)[aggregator.current("5m").t][4]
    assert aggregator.remove_interval("5m") and not aggregator.remove_interval("5m")
    assert aggregator.current("5m") is None and aggregator.intervals == ("1m",)
    assert {tf for tf, _, _ in aggregator.on_tick(items[-1][0] + 1000, 1.0, 1)} == {"1m"}


def test_only_canonical_time_intervals_can_be_added():
    aggregator = BarAggregator(NEW_YORK, "future", [])
    for bad in ("60s", "r:10t", "abc", "07m"):
        with pytest.raises(IntervalError):
            aggregator.add_interval(bad)


def test_any_interval_from_ticks_matches_the_reference_counted_from_the_anchor():
    items = list(ticks(3 * 3600))
    aggregator = BarAggregator(NEW_YORK, "equity", ["15s", "7m", "45m"])
    last: dict[tuple[str, int], tuple] = {}
    for ts_ms, price, size in items:
        for name, candle, final in aggregator.on_tick(ts_ms, price, size):
            last[(name, candle.t)] = (candle.o, candle.h, candle.l, candle.c, candle.v)
    for name, seconds in (("15s", 15), ("7m", 420), ("45m", 2700)):
        expected = reference(items, seconds, anchor=OPEN)
        assert {t: v for (tf, t), v in last.items() if tf == name} == expected, name


def test_bar_updates_build_whole_minute_intervals_and_leave_seconds_to_ticks():
    aggregator = BarAggregator(NEW_YORK, "equity", ["1m", "7m", "90s", "30s"])
    for i in range(15):
        changes = aggregator.on_bar_update(BarUpdate("AAPL", (OPEN + 60 * i) * 1000, 100 + i, 110 + i, 90 + i, 105 + i,
                                                     10, final=True))
        assert {tf for tf, _, _ in changes} <= {"1m", "7m"}
    second = aggregator.current("7m")
    assert (second.t, second.o, second.h, second.l, second.c, second.v) == (OPEN + 840, 114, 124, 104, 119, 10)
    first = aggregator.bounds(OPEN + 60 * 6, "7m")[0]
    assert first == OPEN and aggregator.current("90s") is None and aggregator.current("30s") is None


def test_an_interval_added_late_is_seeded_from_the_minutes_of_bar_updates():
    aggregator = BarAggregator(NEW_YORK, "equity", ["1m"])
    for i in range(9):
        aggregator.on_bar_update(BarUpdate("AAPL", (OPEN + 60 * i) * 1000, 100 + i, 110 + i, 90 + i, 105 + i, 10, final=True))
    aggregator.add_interval("7m")
    bar = aggregator.current("7m")
    assert (bar.t, bar.o, bar.h, bar.l, bar.c, bar.v) == (OPEN + 420, 107, 118, 97, 113, 20)
