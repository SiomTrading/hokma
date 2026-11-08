"""T-5: any time interval is built from the right Yahoo base and anchored to the session of each class of instrument.

Two kinds of evidence. A synthetic source gives minute bars whose values follow a rule simple enough to calculate by
hand (minute `m` of a session has k = m % 50: open 100+k, high 100+k+2, low 100+k-1, close 100+k+1, volume 10), so the
first three bars of each case are written out as numbers. The recorded 1-minute answer for NQ=F is grouped with a plain
loop and compared bar by bar. Nothing touches the network."""
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import pytest

from hokma.market.errors import BAD_RESPONSE, UPSTREAM, MarketError
from hokma.market.models import Candle, Series
from hokma.market.service import build_chart, history_for
from hokma.market.intervals import parse_time
from hokma.market.yahoo import FRESH_FOR, UPSTREAM_PARAMS, YahooSource
from market_support import RECORDED_AT, FakeFetcher

SESSION_MINUTES = {"equity": 390, "future": 1380, "crypto": 1440, "currency": 1440}


def offset_hours(day: date) -> int:
    """New York offset in the weeks the tests use: daylight time ends on Sunday 2026-11-01 at 02:00."""
    return -4 if day < date(2026, 11, 1) else -5


def utc_at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def session_start(kind: str, label: date) -> int:
    """Epoch second at which the session of `label` opens, written out per class."""
    if kind == "equity":
        moment = utc_at(label, 9, 30) - timedelta(hours=offset_hours(label))
    elif kind == "future":
        eve = label - timedelta(days=1)
        moment = utc_at(eve, 18) - timedelta(hours=offset_hours(eve))
    elif kind == "currency":
        eve = label - timedelta(days=1)
        moment = utc_at(eve, 17) - timedelta(hours=offset_hours(eve))
    else:
        moment = utc_at(label, 0)
    return int(moment.timestamp())


def weekdays(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1) if (first + timedelta(days=i)).weekday() < 5]


def minute_bar(m: int) -> tuple[float, float, float, float, float]:
    k = m % 50
    return 100 + k, 100 + k + 2, 100 + k - 1, 100 + k + 1, 10.0


def fold(minutes: list[int]) -> tuple:
    bars = [minute_bar(m) for m in minutes]
    return (bars[0][0], max(b[1] for b in bars), min(b[2] for b in bars), bars[-1][3], sum(b[4] for b in bars))


class SyntheticSource:
    """Minute-rule bars for the intraday bases and one hand-made bar per session for the daily base."""
    name = "Synthetic"

    def __init__(self, kind: str, intraday: list[date], daily: list[date] | None = None, hourly_shift: int = 0,
                 missing: tuple[str, ...] = ()) -> None:
        self.kind, self.intraday, self.daily = kind, intraday, daily or []
        self.hourly_shift = hourly_shift                     # seconds added to every 1h stamp (a base off the grid)
        self.missing = missing                               # bases that answer with an upstream error
        self.asked: list[str] = []

    def search(self, query):
        return []

    def history(self, symbol: str, timeframe: str) -> Series:
        self.asked.append(timeframe)
        if timeframe in self.missing:
            raise MarketError(UPSTREAM, 502)
        candles = self._daily() if timeframe == "1D" else self._intraday({"1m": 1, "5m": 5, "15m": 15, "1h": 60}[timeframe])
        zone = "UTC" if self.kind == "crypto" else "America/New_York"
        return Series(symbol=symbol, name=symbol, kind=self.kind, exchange="X", currency="USD", timezone=zone,
                      utc_offset_seconds=0, candles=candles, previous_close=None, last_trade_at=None, session=None,
                      fetched_at=RECORDED_AT)

    def _intraday(self, minutes: int) -> list[Candle]:
        out = []
        for label in self.intraday:
            start, total = session_start(self.kind, label), SESSION_MINUTES[self.kind]
            for first in range(0, total, minutes):
                o, h, l, c, v = fold(list(range(first, min(first + minutes, total))))
                shift = self.hourly_shift if minutes == 60 else 0
                out.append(Candle(start + first * 60 + shift, o, h, l, c, v))
        return out

    def _daily(self) -> list[Candle]:
        inside = {"equity": 0, "future": 6 * 3600, "currency": 7 * 3600, "crypto": 0}[self.kind]
        return [Candle(session_start(self.kind, label) + inside, 100.0 + j, 110.0 + j, 90.0 + j, 105.0 + j, 1000.0)
                for j, label in enumerate(self.daily)]


NOW = lambda: float(session_start("crypto", date(2026, 11, 6)))             # noqa: E731
INTRADAY_DAYS = weekdays(date(2026, 11, 2), date(2026, 11, 5))
LAST = INTRADAY_DAYS[-1]
KINDS = ["equity", "future", "crypto", "currency"]


def chart(source, interval: str, symbol: str = "SYN"):
    return build_chart(source, symbol, interval, NOW)


def bars_of_last_session(kind: str, candles: list[Candle]) -> list[Candle]:
    start = session_start(kind, LAST)
    return [c for c in candles if c.t >= start]


def as_tuple(candle: Candle) -> tuple:
    return candle.o, candle.h, candle.l, candle.c, candle.v


# ---------------------------------------------------------------- 7m from 1m, 45m and 3h from 15m

@pytest.mark.parametrize("kind", KINDS)
def test_7m_is_built_from_1m_and_anchored_to_the_session(kind):
    source = SyntheticSource(kind, INTRADAY_DAYS)
    data = chart(source, "7m")
    assert source.asked[0] == "1m" and data.timeframe == "7m"
    first = bars_of_last_session(kind, data.candles)[:3]
    start = session_start(kind, LAST)
    assert [c.t for c in first] == [start, start + 420, start + 840]
    assert [as_tuple(c) for c in first] == [(100, 108, 99, 107, 70), (107, 115, 106, 114, 70), (114, 122, 113, 121, 70)]


@pytest.mark.parametrize("kind", KINDS)
def test_45m_is_built_from_15m(kind):
    source = SyntheticSource(kind, INTRADAY_DAYS)
    data = chart(source, "45m")
    assert source.asked[0] == "15m"
    first = bars_of_last_session(kind, data.candles)[:3]
    start = session_start(kind, LAST)
    assert [c.t for c in first] == [start, start + 2700, start + 5400]
    assert [as_tuple(c) for c in first] == [(100, 146, 99, 145, 450), (145, 151, 99, 140, 450), (140, 151, 99, 135, 450)]


@pytest.mark.parametrize("kind", ["future", "crypto", "currency"])
def test_3h_is_built_from_15m(kind):
    source = SyntheticSource(kind, INTRADAY_DAYS)
    data = chart(source, "3h")
    assert source.asked[0] == "15m"
    first = bars_of_last_session(kind, data.candles)[:3]
    start = session_start(kind, LAST)
    assert [c.t for c in first] == [start, start + 10800, start + 21600]
    assert [as_tuple(c) for c in first] == [(100, 151, 99, 130, 1800), (130, 151, 99, 110, 1800), (110, 151, 99, 140, 1800)]


def test_3h_of_a_stock_ends_with_the_short_bar_before_the_close():
    data = chart(SyntheticSource("equity", INTRADAY_DAYS), "3h")
    first = bars_of_last_session("equity", data.candles)[:3]
    assert [as_tuple(c) for c in first] == [(100, 151, 99, 130, 1800), (130, 151, 99, 110, 1800), (110, 141, 109, 140, 300)]


@pytest.mark.parametrize("kind", KINDS)
def test_4h_and_longer_use_hourly_bars_and_stay_on_the_anchor(kind):
    source = SyntheticSource(kind, INTRADAY_DAYS)
    data = chart(source, "4h")
    assert source.asked[0] == "1h"
    first = bars_of_last_session(kind, data.candles)[:2]
    start = session_start(kind, LAST)
    assert [c.t for c in first] == [start, start + 4 * 3600]
    assert as_tuple(first[0]) == (100, 151, 99, 140, 2400)           # minutes 0 to 239: k reaches 49 and the last is 239 % 50 = 39


def test_hourly_bars_that_are_off_the_session_grid_are_skipped_for_the_next_base():
    source = SyntheticSource("equity", INTRADAY_DAYS, hourly_shift=-1800)       # stamped on the hour, not at 09:30
    data = chart(source, "4h")
    assert source.asked[:2] == ["1h", "15m"]
    assert bars_of_last_session("equity", data.candles)[0].t == session_start("equity", LAST)


def test_a_finer_base_is_tried_when_the_coarser_one_is_missing():
    source = SyntheticSource("future", INTRADAY_DAYS, missing=("15m",))
    data = chart(source, "45m")
    assert source.asked[:2] == ["15m", "5m"]
    assert as_tuple(bars_of_last_session("future", data.candles)[0]) == (100, 146, 99, 145, 450)


def test_no_base_gives_interval_unavailable_with_the_numbers_for_the_message():
    source = SyntheticSource("future", INTRADAY_DAYS, missing=("1m",))
    with pytest.raises(MarketError) as error:
        chart(source, "7m")
    assert error.value.code == "interval_unavailable"
    assert error.value.params == {"interval": "7m", "base": "1m", "source": "Synthetic", "symbol": "SYN"}


def test_other_errors_are_not_hidden_by_trying_a_finer_base():
    class Down(SyntheticSource):
        def history(self, symbol, timeframe):
            raise MarketError("network", 502)

    with pytest.raises(MarketError) as error:
        chart(Down("future", INTRADAY_DAYS), "45m")
    assert error.value.code == "network"


def test_bad_response_also_moves_to_the_next_base():
    class Broken(SyntheticSource):
        def history(self, symbol, timeframe):
            if timeframe == "15m":
                raise MarketError(BAD_RESPONSE, 502)
            return super().history(symbol, timeframe)

    assert chart(Broken("future", INTRADAY_DAYS), "45m").candles


@pytest.mark.parametrize("text", ["30s", "90s", "1s", "3599s"])
def test_seconds_need_a_provider_of_ticks(text):
    with pytest.raises(MarketError) as error:
        chart(SyntheticSource("future", INTRADAY_DAYS), text)
    assert error.value.code == "seconds_need_ticks"


def test_the_five_original_timeframes_still_use_their_own_bars_untouched():
    for text, base in (("1m", "1m"), ("5m", "5m"), ("15m", "15m"), ("1h", "1h")):
        source = SyntheticSource("equity", INTRADAY_DAYS)
        data = chart(source, text)
        assert source.asked[0] == base
        assert data.candles == source.history("SYN", base).candles[-len(data.candles):]


def test_non_canonical_text_is_returned_canonical():
    assert chart(SyntheticSource("future", INTRADAY_DAYS), "07m").timeframe == "7m"
    assert chart(SyntheticSource("future", INTRADAY_DAYS), "60m").timeframe == "1h"


def test_other_intervals_show_up_to_300_bars_and_the_original_ones_keep_their_count():
    source = SyntheticSource("future", weekdays(date(2026, 10, 19), date(2026, 11, 5)))
    assert len(chart(source, "7m").candles) == 300
    assert len(chart(source, "5m").candles) == 180


# ---------------------------------------------------------------- 2D and 1W from daily bars

DAILY_DAYS = weekdays(date(2026, 10, 19), date(2026, 11, 6))          # 15 sessions, three full weeks


def weekday_number(day: date) -> int:
    """Weekdays since Monday 1970-01-05, counted one by one."""
    count, cursor = 0, date(1970, 1, 5)
    while cursor < day:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            count += 1
    return count


@pytest.mark.parametrize("kind", ["equity", "future", "currency"])
def test_2d_groups_two_sessions_from_daily_bars(kind):
    source = SyntheticSource(kind, [], DAILY_DAYS)
    data = chart(source, "2D")
    assert source.asked[0] == "1D"
    first_pair_starts_at = 0 if weekday_number(DAILY_DAYS[0]) % 2 == 0 else -1
    groups = []
    index = first_pair_starts_at
    while index < len(DAILY_DAYS):
        groups.append(list(range(max(index, 0), min(index + 2, len(DAILY_DAYS)))))
        index += 2
    expected = [(100 + g[0], 110 + g[-1], 90 + g[0], 105 + g[-1], 1000 * len(g)) for g in groups]
    starts = [session_start(kind, DAILY_DAYS[g[0]]) for g in groups]
    got = data.candles[-3:]
    assert [as_tuple(c) for c in got] == expected[-3:]
    assert [c.t for c in got] == starts[-3:]


def test_2d_in_crypto_counts_every_day():
    days = [date(2026, 11, 1) + timedelta(days=i) for i in range(8)]
    data = chart(SyntheticSource("crypto", [], days), "2D")
    assert all(c.v == 2000 for c in data.candles[:-1] if c.v)
    assert {c.t % 86400 for c in data.candles} == {0}


@pytest.mark.parametrize("kind", ["equity", "future", "currency", "crypto"])
def test_1w_starts_on_monday_and_on_sunday_evening_for_futures_and_forex(kind):
    source = SyntheticSource(kind, [], DAILY_DAYS)
    data = chart(source, "1W")
    assert source.asked[0] == "1D"
    mondays = [date(2026, 10, 19), date(2026, 10, 26), date(2026, 11, 2)]
    assert [c.t for c in data.candles] == [session_start(kind, monday) for monday in mondays]
    assert [as_tuple(c) for c in data.candles] == [(100 + 5 * w, 114 + 5 * w, 90 + 5 * w, 109 + 5 * w, 5000)
                                                   for w in range(3)]


def test_future_weeks_start_on_sunday_at_18_new_york_and_stock_weeks_on_monday_at_the_open():
    assert session_start("future", date(2026, 11, 2)) == int(datetime(2026, 11, 1, 23, tzinfo=timezone.utc).timestamp())
    assert session_start("equity", date(2026, 11, 2)) == int(datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc).timestamp())


def test_1d_is_the_daily_series_as_the_source_gives_it():
    source = SyntheticSource("equity", [], DAILY_DAYS)
    assert chart(source, "1D").candles == source.history("SYN", "1D").candles[-len(chart(source, "1D").candles):]


def test_days_and_weeks_ask_only_for_daily_bars():
    for text in ("3D", "5D", "2W", "4W", "365D"):
        source = SyntheticSource("future", [], DAILY_DAYS)
        chart(source, text)
        assert source.asked[0] == "1D", text


def test_daily_bars_missing_gives_interval_unavailable():
    source = SyntheticSource("future", INTRADAY_DAYS, missing=("1D",))
    with pytest.raises(MarketError) as error:
        history_for(source, "SYN", parse_time("2D"))
    assert error.value.params["base"] == "1D"


# ---------------------------------------------------------------- real answers

def reference_start(ts: int) -> int:
    """Start of the futures session that holds `ts`: 18:00 New York, written as UTC hours per date."""
    anchors = []
    for day in range(28, 32):                                   # 2026-10-28 .. 31 (daylight time, 22:00 UTC)
        anchors.append(int(datetime(2026, 10, day, 22, tzinfo=timezone.utc).timestamp()))
    for day in range(1, 8):                                     # 2026-11-01 onwards (standard time, 23:00 UTC)
        anchors.append(int(datetime(2026, 11, day, 23, tzinfo=timezone.utc).timestamp()))
    return max(a for a in anchors if a <= ts)


def test_7m_from_the_recorded_nq_minutes_equals_a_plain_grouping(tmp_path):
    source = YahooSource(tmp_path, fetch=FakeFetcher(), clock=lambda: RECORDED_AT)
    minutes = source.history("NQ=F", "1m").candles
    expected: dict[int, list[Candle]] = {}
    for candle in minutes:
        anchor = reference_start(candle.t)
        expected.setdefault(anchor + (candle.t - anchor) // 420 * 420, []).append(candle)
    built = build_chart(source, "NQ=F", "7m", lambda: RECORDED_AT)
    got = {c.t: as_tuple(c) for c in built.candles}
    assert len(expected) > 40 and set(got) == set(expected)
    for start, parts in expected.items():
        assert got[start] == (parts[0].o, max(p.h for p in parts), min(p.l for p in parts), parts[-1].c,
                              sum(p.v for p in parts)), start
    stray = minutes[-1]
    assert stray.t % 60 != 0                                    # the recording ends with a partial bar: it joins its bar
    assert any(c.t <= stray.t < c.t + 420 and c.c == stray.c for c in built.candles)


# ---------------------------------------------------------------- the ranges asked of the source

def test_each_base_asks_the_longest_range_its_interval_allows():
    assert UPSTREAM_PARAMS == {"1m": ("1m", "7d"), "5m": ("5m", "60d"), "15m": ("15m", "60d"), "1h": ("1h", "2y"),
                               "1D": ("1d", "10y")}
    assert set(FRESH_FOR) == set(UPSTREAM_PARAMS)


@pytest.mark.parametrize("symbol,base,interval,span", [("NQ=F", "1m", "1m", "7d"), ("NQ=F", "5m", "5m", "60d"),
                                                      ("AAPL", "1D", "1d", "10y"), ("BTC-USD", "1h", "1h", "2y")])
def test_the_request_carries_the_interval_and_range_of_the_base(tmp_path, symbol, base, interval, span):
    fetcher = FakeFetcher()
    YahooSource(tmp_path, fetch=fetcher, clock=lambda: RECORDED_AT).history(symbol, base)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(fetcher.calls[0]).query)
    assert query["interval"] == [interval] and query["range"] == [span]


def test_a_15m_request_asks_60_days(tmp_path):
    fetcher = FakeFetcher()
    with pytest.raises(MarketError):                            # no recording for this pair: only the URL matters
        YahooSource(tmp_path, fetch=fetcher, clock=lambda: RECORDED_AT).history("AAPL", "15m")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(fetcher.calls[0]).query)
    assert query["interval"] == ["15m"] and query["range"] == ["60d"]
