"""D9: the Yahoo source and the chart service, against recorded answers. No network."""
import urllib.request

import pytest

from hokma.market.cache import DiskCache, RateLimiter
from hokma.market.errors import NETWORK, RATE_LIMITED, UNKNOWN_SYMBOL, UPSTREAM, MarketError
from hokma.market.replay import ReplayFetcher
from hokma.market.service import build_chart, market_is_open
from hokma.market.yahoo import YahooSource, parse_chart, valid_symbol
from market_support import FIXTURES, RECORDED_AT, FakeFetcher, fixture_json


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the market tests must not use the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


class Clock:
    def __init__(self, now: float = RECORDED_AT) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fetcher():
    return FakeFetcher()


@pytest.fixture
def source(tmp_path, fetcher, clock):
    return YahooSource(tmp_path / "cache", fetch=fetcher, clock=clock)


# ---------- parsing ----------

def test_parse_drops_null_bars_and_keeps_order():
    raw = fixture_json("nqf_5m.json")
    holes = sum(1 for c in raw["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c is None)
    assert holes > 0  # the recording really has holes
    series = parse_chart(raw, "NQ=F", 1, False, "test")
    assert len(series.candles) == len(raw["chart"]["result"][0]["timestamp"]) - holes
    assert [c.t for c in series.candles] == sorted(c.t for c in series.candles)
    assert all(c.l <= min(c.o, c.c) and c.h >= max(c.o, c.c) for c in series.candles)
    assert series.kind == "future" and series.timezone == "America/New_York"


def test_parse_reads_the_symbol_name_and_session():
    series = parse_chart(fixture_json("aapl_5m.json"), "AAPL", 1, False, "test")
    assert (series.symbol, series.name, series.kind, series.currency) == ("AAPL", "Apple Inc.", "equity", "USD")
    assert series.session is not None and series.session.start < series.session.end


def test_parse_maps_yahoo_errors():
    with pytest.raises(MarketError) as error:
        parse_chart(fixture_json("unknown_symbol.json")["body"], "ZZZZ", 1, False, "test")
    assert error.value.code == UNKNOWN_SYMBOL and error.value.status == 404
    with pytest.raises(MarketError) as error:
        parse_chart({"chart": {"result": None, "error": {"code": "Internal", "description": "x"}}}, "A", 1, False, "t")
    assert error.value.code == UPSTREAM
    with pytest.raises(MarketError):
        parse_chart({"nope": 1}, "A", 1, False, "t")


@pytest.mark.parametrize("symbol,ok", [("NQ=F", True), ("^GSPC", True), ("BTC-USD", True), ("BRK-B", True), ("7203.T", True),
                                        ("", False), ("A B", False), ("../etc", False), ("A" * 21, False), ("a;b", False)])
def test_symbol_validation(symbol, ok):
    assert valid_symbol(symbol) is ok


# ---------- search ----------

def test_search_returns_stocks_etfs_indices_and_continuous_futures(source):
    hits = source.search("nasdaq")
    by_symbol = {h.symbol: h for h in hits}
    assert by_symbol["NQ=F"].kind == "future"
    assert by_symbol["^IXIC"].kind == "index"
    assert by_symbol["QQQM"].kind == "etf"
    assert all(h.name for h in hits)


def test_search_with_no_match_is_an_empty_list(source):
    assert source.search("zzqqxxnotasymbol") == []


def test_search_is_cached_on_disk(source, fetcher):
    source.search("nasdaq")
    source.search("nasdaq")
    assert len(fetcher.calls) == 1


# ---------- transport: cache, stale, errors ----------

def test_second_read_inside_the_fresh_window_does_not_call_upstream(source, fetcher, clock):
    first = source.history("AAPL", "5m")
    clock.now += 10
    second = source.history("AAPL", "5m")
    assert len(fetcher.calls) == 1
    assert len(first.candles) == len(second.candles) and not second.stale


def test_expired_cache_is_fetched_again(source, fetcher, clock):
    source.history("AAPL", "5m")
    clock.now += 600
    source.history("AAPL", "5m")
    assert len(fetcher.calls) == 2


def test_cache_survives_a_new_source_instance(tmp_path, fetcher, clock):
    YahooSource(tmp_path / "cache", fetch=fetcher, clock=clock).history("AAPL", "5m")
    again = YahooSource(tmp_path / "cache", fetch=fetcher, clock=clock)
    again.history("AAPL", "5m")
    assert len(fetcher.calls) == 1


def test_network_failure_serves_the_old_cache_marked_stale(source, fetcher, clock):
    source.history("AAPL", "5m")
    clock.now += 3600
    fetcher.fail = "network"
    series = source.history("AAPL", "5m")
    assert series.stale is True and series.candles


def test_network_failure_without_cache_raises(source, fetcher):
    fetcher.fail = "network"
    with pytest.raises(MarketError) as error:
        source.history("AAPL", "5m")
    assert error.value.code == NETWORK


@pytest.mark.parametrize("mode,code", [("429", RATE_LIMITED), ("500", UPSTREAM)])
def test_upstream_errors_without_cache(source, fetcher, mode, code):
    fetcher.fail = mode
    with pytest.raises(MarketError) as error:
        source.history("AAPL", "5m")
    assert error.value.code == code


def test_rate_limited_upstream_with_cache_is_stale_not_an_error(source, fetcher, clock):
    source.history("AAPL", "5m")
    clock.now += 3600
    fetcher.fail = "429"
    assert source.history("AAPL", "5m").stale is True


def test_unknown_symbol_is_not_hidden_by_the_cache(source):
    with pytest.raises(MarketError) as error:
        source.history("ZZZZNOTREAL", "5m")
    assert error.value.code == UNKNOWN_SYMBOL


def test_our_own_limiter_protects_the_provider(tmp_path, fetcher, clock):
    limited = YahooSource(tmp_path / "c", fetch=fetcher, clock=clock, limiter=RateLimiter(limit=1, window=60))
    limited.history("AAPL", "5m")
    with pytest.raises(MarketError) as error:
        limited.history("NQ=F", "5m")
    assert error.value.code == RATE_LIMITED
    assert len(fetcher.calls) == 1


def test_rate_limiter_window_slides():
    now = [0.0]
    limiter = RateLimiter(limit=2, window=10, clock=lambda: now[0])
    assert limiter.allow() and limiter.allow() and not limiter.allow()
    now[0] = 10.0
    assert limiter.allow()


def test_disk_cache_prunes_old_entries(tmp_path, monkeypatch):
    import hokma.market.cache as cache_module

    monkeypatch.setattr(cache_module, "MAX_ENTRIES", 3)
    cache = DiskCache(tmp_path)
    for i in range(6):
        cache.put(f"k{i}", "x", i)
    assert len(list(tmp_path.glob("*.json"))) <= 3
    assert cache.get("missing") is None


def test_corrupt_cache_file_is_ignored(tmp_path):
    cache = DiskCache(tmp_path)
    cache.put("k", "body", 5)
    next(tmp_path.glob("*.json")).write_text("{not json", encoding="utf-8")
    assert cache.get("k") is None


# ---------- chart service ----------

def test_closed_market_for_stocks_after_the_close(source, clock):
    chart = build_chart(source, "AAPL", "5m", clock)
    assert chart.market_open is False
    assert chart.delayed is False  # a closed market is not "delayed"
    assert chart.last_price == chart.candles[-1].c
    assert chart.opening_range is not None and chart.opening_range.session_date == "2026-11-04"
    # the previous close is the close of the daily bar before the last one, read here from the raw daily recording
    daily_closes = fixture_json("aapl_1d.json")["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    assert chart.previous_close == pytest.approx(daily_closes[-2])
    assert chart.change == pytest.approx(chart.last_price - daily_closes[-2])
    assert chart.change_pct == pytest.approx(chart.change / daily_closes[-2] * 100)


def test_the_change_means_the_same_on_every_timeframe(source, clock):
    five = build_chart(source, "AAPL", "5m", clock)
    daily = build_chart(source, "AAPL", "1D", clock)
    assert five.change_pct == pytest.approx(daily.change_pct)


def test_without_daily_bars_the_change_falls_back_to_the_providers_previous_close(source, clock):
    # the recordings have no daily answer for the future, so the daily request fails
    chart = build_chart(source, "NQ=F", "5m", clock)
    assert chart.previous_close == pytest.approx(28566.75)  # meta.previousClose of the 5 minute recording


def test_open_futures_with_old_last_trade_are_flagged_delayed(source, clock):
    chart = build_chart(source, "NQ=F", "5m", clock)
    assert chart.market_open is True
    assert chart.data_age_seconds is not None and chart.data_age_seconds > 300
    assert chart.delayed is True


def test_open_futures_with_a_fresh_last_trade_are_not_delayed(source, clock):
    series = source.history("NQ=F", "5m")
    clock.now = series.last_trade_at + 20
    chart = build_chart(source, "NQ=F", "5m", clock)
    assert chart.market_open is True and chart.delayed is False


def test_market_closes_when_the_data_stops_even_inside_the_session(source, clock):
    series = source.history("NQ=F", "5m")
    assert market_is_open(series, "5m", series.last_trade_at + 60)
    assert not market_is_open(series, "5m", series.last_trade_at + 5 * 3600)


def test_daily_timeframe_has_no_opening_line_and_uses_the_previous_bar_for_change(source, clock):
    chart = build_chart(source, "AAPL", "1D", clock)
    assert chart.opening_range is None
    assert chart.previous_close == chart.candles[-2].c
    assert chart.change == pytest.approx(chart.candles[-1].c - chart.candles[-2].c)


def test_hourly_chart_gets_its_opening_line_from_5_minute_bars(source, fetcher, clock):
    # the recording has no 1h file for AAPL: the chart itself fails, but the fallback path is what matters
    fetcher.overrides["AAPL"] = (200, (FIXTURES / "aapl_5m.json").read_text())
    chart = build_chart(source, "AAPL", "1h", clock)
    assert chart.opening_range is not None
    direct = build_chart(source, "AAPL", "5m", clock).opening_range
    assert chart.opening_range.high == direct.high


def test_crypto_is_always_in_session(source, clock):
    series = source.history("BTC-USD", "1h")
    clock.now = series.last_trade_at + 30
    chart = build_chart(source, "BTC-USD", "1h", clock)
    assert chart.market_open is True and chart.kind == "crypto"


def test_visible_bars_are_the_newest_ones(source, clock):
    chart = build_chart(source, "AAPL", "5m", clock)
    full = source.history("AAPL", "5m").candles
    assert chart.candles == full[-len(chart.candles):] and len(chart.candles) == 180


def test_bad_timeframe_is_rejected(source, clock):
    with pytest.raises(MarketError) as error:
        build_chart(source, "AAPL", "2m", clock)
    assert error.value.code == "bad_timeframe"


def test_missing_finer_bars_only_hide_the_line(source, fetcher, clock):
    series_1h = source.history("BTC-USD", "1h")
    assert series_1h.candles
    chart = build_chart(source, "BTC-USD", "1h", clock)  # no 5m recording for BTC: line hidden, chart intact
    assert chart.opening_range is None and chart.candles


# ---------- replay (used by the end-to-end tests) ----------

def test_replay_fetcher_serves_files_and_reports_missing_ones(tmp_path):
    (tmp_path / "chart_AAPL_5m.json").write_text("{}", encoding="utf-8")
    (tmp_path / "search_nq.json").write_text('{"quotes": [1]}', encoding="utf-8")
    replay = ReplayFetcher(tmp_path)
    assert replay("https://x/v8/finance/chart/AAPL?interval=5m&range=5d") == (200, "{}")
    assert replay("https://x/v8/finance/chart/NQ%3DF?interval=5m")[0] == 404
    assert replay("https://x/v1/finance/search?q=NQ&quotesCount=8") == (200, '{"quotes": [1]}')
    assert replay("https://x/v1/finance/search?q=zz") == (200, '{"quotes": []}')
