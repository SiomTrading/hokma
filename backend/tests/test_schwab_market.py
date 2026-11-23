"""Schwab as a source of price, candles and the option chain (SPEC-015 5.4, 5.5, 5.6 and 16.15). No network: the fake Schwab of
schwab_support.py answers by path and the fake Alpaca of options_support.py is the next source of the options.

The numbers are made up and written by hand, so each expected value can be checked on paper."""
import asyncio
import json
import urllib.request
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from options_support import DEMO_KEY, OPEN_NOW, install
from schwab_support import FakeSchwab, clear_fake

from hokma.core.app import create_app
from hokma.market import api as market_api
from hokma.market import options
from hokma.market.adapters import ADAPTERS
from hokma.market.adapters import schwab as feed
from hokma.market.cache import RateLimiter
from hokma.market.errors import MarketError
from hokma.market.history import ConnectedSource
from hokma.market.messages import DOWN_CODES, connection_message
from hokma.market.models import Candle, Series
from hokma.market.options import OptionsError
from hokma.market.providers import get_catalog
from hokma.market.stream import SourceStatus, SymbolError, TerminalError, Tick, classify_error  # noqa: F401
from hokma.rpc.ws import SESSION_TOKEN
from hokma.schwab import endpoints, http

AUTH = {"X-Hokma-Token": SESSION_TOKEN}
QUOTES, CHAINS, EXPIRATIONS, HISTORY = ("/marketdata/v1/quotes", "/marketdata/v1/chains", "/marketdata/v1/expirationchain",
                                        "/marketdata/v1/pricehistory")
HOURS_OPTION = "/marketdata/v1/markets/option"
SCHWAB_KEYS = json.dumps({"client_id": "client-id-0001", "client_secret": "client-secret-0001"})


def ms(*moment) -> int:
    return int(datetime(*moment, tzinfo=timezone.utc).timestamp() * 1000)


QUOTE_MS = ms(2026, 11, 20, 20, 59, 58)             # more than 2**31: Schwab says int32 in the REF, the value does not fit
QUOTE_TEXT = "2026-11-20T20:59:58Z"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the tests must not use the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture(scope="module")
def _server():
    with FakeSchwab() as fake:
        yield fake


@pytest.fixture
def schwab(_server):
    clear_fake(_server)
    http.reset_limits()
    yield _server
    clear_fake(_server)
    endpoints.set_clock(None)


def paths(fake: FakeSchwab, path: str) -> list:
    return [c for c in fake.calls if c.path == path]


# ------------------------------------------------------------------------------------------ 5.4 the price by query

def quote_item(symbol: str, last: float = 100.0, volume: int = 1000, trade: int = QUOTE_MS, realtime: bool = True) -> dict:
    return {"assetMainType": "EQUITY", "symbol": symbol, "realtime": realtime, "quoteType": "NBBO",
            "quote": {"lastPrice": last, "bidPrice": round(last - 0.01, 2), "askPrice": round(last + 0.01, 2), "bidSize": 3,
                      "askSize": 4, "totalVolume": volume, "openPrice": round(last - 1, 2), "highPrice": round(last + 1, 2),
                      "lowPrice": round(last - 2, 2), "closePrice": round(last - 0.5, 2), "netChange": 0.5,
                      "netPercentChange": 0.5, "mark": last,
                      "quoteTime": trade + 5, "tradeTime": trade},
            "reference": {"description": symbol + " test", "exchangeName": "NASDAQ"}}


def test_the_adapter_is_registered_so_the_card_of_schwab_is_available():
    assert "schwab" in ADAPTERS
    source = ADAPTERS["schwab"](get_catalog().get("schwab"), None, None)
    assert isinstance(source, feed.SchwabSource) and source.name == "Schwab" and source.capabilities.max_symbols == 100
    assert "secret" not in repr(source).lower()


def test_the_card_of_schwab_with_a_session_is_not_marked_unavailable(monkeypatch):
    from hokma.market.hub import MarketHub
    from hokma.schwab import auth

    class Config:
        loaded = True

        def order(self, use):
            return list(get_catalog().default_priority[use])

        def is_enabled(self, provider_id):
            return True

        def has_key(self, provider_id):
            return provider_id == "schwab"

        def signed_in(self, provider_id):
            return True

    monkeypatch.setattr(auth, "session_card", lambda: {"state": "signed_in", "session_expires_at": None, "accounts": []})
    hub = MarketHub(get_catalog(), Config(), lambda: None)
    card = hub.card("schwab")
    assert card["available"] is True and card["auth"] == "oauth" and card["max_symbols"] == 100
    assert hub._eligible("schwab") is True


def test_a_provider_with_keys_but_no_session_is_not_eligible(monkeypatch):
    from hokma.market.hub import MarketHub

    class Config:
        loaded = True

        def order(self, use):
            return list(get_catalog().default_priority[use])

        def is_enabled(self, provider_id):
            return True

        def has_key(self, provider_id):
            return True

        def signed_in(self, provider_id):
            return False

    hub = MarketHub(get_catalog(), Config(), lambda: None)
    assert hub._eligible("schwab") is False and hub._eligible("alpaca") is True


def test_map_quote_takes_the_fields_of_the_spec_and_marks_a_false_realtime_as_delayed():
    item = quote_item("AAPL", 228.4, 731132)
    mapped = feed.map_quote(item)
    assert mapped == {"symbol": "AAPL", "quote_type": "NBBO", "delayed": False, "last": 228.4, "bid": 228.39,
                      "ask": 228.41, "bid_size": 3.0, "ask_size": 4.0, "volume": 731132.0, "open": 227.4,
                      "high": 229.4, "low": 226.4, "close": 227.9, "change": 0.5, "change_pct": 0.5, "mark": 228.4,
                      "quote_time": QUOTE_MS + 5.0, "trade_time": float(QUOTE_MS)}
    assert feed.map_quote(quote_item("AAPL", realtime=False))["delayed"] is True
    assert feed.map_quote({"symbol": "X", "quote": {"lastPrice": "n/a", "bidPrice": float("nan")}})["last"] is None
    assert feed.map_quote({"symbol": "X"})["delayed"] is False and feed.map_quote({"symbol": "X"})["last"] is None


# ------------------------------------------------------------------------------------------ 5.5 the candles

NOW = int(datetime(2024, 11, 6, 15, 40, tzinfo=timezone.utc).timestamp())          # Wednesday, 10:40 in New York
OPEN_930 = int(datetime(2024, 11, 6, 14, 30, tzinfo=timezone.utc).timestamp())    # 09:30 in New York
SCHWAB_BARS = {"symbol": "AAPL", "empty": False, "candles": []}


class Config:
    loaded = True

    def __init__(self, keys=None, off=(), order=None, signed=True) -> None:
        self.keys, self.off, self._order, self.signed = keys if keys is not None else {"schwab": SCHWAB_KEYS}, off, order or {}, signed
        self.catalog = get_catalog()

    def is_enabled(self, provider_id):
        return provider_id not in self.off

    def has_key(self, provider_id):
        return provider_id in self.keys

    def signed_in(self, provider_id):
        return self.signed

    def order(self, use):
        return list(self._order.get(use, self.catalog.default_priority.get(use, ())))

    def read_secret(self, provider_id):
        return self.keys.get(provider_id)


class Yahoo:
    name = "Yahoo Finance"

    def __init__(self) -> None:
        self.calls = []

    def search(self, query):
        return []

    def history(self, symbol, timeframe):
        self.calls.append((symbol, timeframe))
        return Series(symbol, symbol, "equity", "NMS", "USD", "America/New_York", -18000, [Candle(OPEN_930, 1, 2, 0.5, 1.5, 10)],
                      None, OPEN_930, None, NOW, False, self.name)


def no_other_provider(url, headers, body=None):
    raise AssertionError("only Schwab was to be asked: " + url)


def history_chain(tmp_path, **config):
    yahoo = Yahoo()
    return ConnectedSource(yahoo, Config(**config), get_catalog(), tmp_path / "cache", fetch=no_other_provider,
                           clock=lambda: NOW), yahoo


def bar(start: int, o, h, l, c, v) -> dict:
    return {"datetime": start * 1000, "open": o, "high": h, "low": l, "close": c, "volume": v}


@pytest.mark.parametrize("base,query", [
    ("1m", {"frequencyType": "minute", "frequency": "1"}),
    ("5m", {"frequencyType": "minute", "frequency": "5"}),
    ("15m", {"frequencyType": "minute", "frequency": "15"}),
    ("1h", {"frequencyType": "minute", "frequency": "30"}),
    ("1D", {"periodType": "year", "frequencyType": "daily", "frequency": "1"}),
])
def test_the_candles_of_each_base_ask_the_combination_of_the_ref_with_the_span_in_milliseconds(tmp_path, schwab, base, query):
    from hokma.market.history import SPAN_DAYS

    schwab.on("GET", HISTORY, {**SCHWAB_BARS, "candles": [bar(OPEN_930, 10, 12, 9, 11, 100)]})
    source, yahoo = history_chain(tmp_path)
    series = source.history("AAPL", base)
    assert series.source == "Schwab" and yahoo.calls == []
    assert schwab.calls[0].method == "GET" and schwab.calls[0].path == HISTORY
    assert schwab.calls[0].query == {"symbol": "AAPL", **query, "startDate": str((NOW - SPAN_DAYS[base] * 86400) * 1000),
                                     "endDate": str(NOW * 1000), "needExtendedHoursData": "true"}


def test_five_minute_bars_are_read_as_they_come_in_seconds_and_in_order(tmp_path, schwab):
    rows = [bar(OPEN_930 + 300, 11, 13, 10, 12, 50), bar(OPEN_930, 10, 12, 9, 11, 100)]
    schwab.on("GET", HISTORY, {**SCHWAB_BARS, "candles": rows})
    source, _ = history_chain(tmp_path)
    assert source.history("AAPL", "5m").candles == [Candle(OPEN_930, 10, 12, 9, 11, 100), Candle(OPEN_930 + 300, 11, 13, 10, 12, 50)]


def test_the_hour_is_folded_from_30_minute_bars_on_the_clock_and_pre_market_is_kept(tmp_path, schwab):
    """SPEC-016 3.3 and 3.4: Schwab gives no 60 minute bar, so it is folded every hour on the hour. Nothing is dropped:
    pre-market (08:00 here) is a bar like any other. The 09:30 bar falls in the 09:00 hour, as the clock says."""
    rows = [bar(OPEN_930 - 5400, 1, 1, 1, 1, 1),                              # 08:00
            bar(OPEN_930, 10, 12, 9, 11, 100), bar(OPEN_930 + 1800, 11, 15, 10, 14, 200),
            bar(OPEN_930 + 3600, 14, 16, 13, 15, 300), bar(OPEN_930 + 5400, 15, 15.5, 12, 13, 400)]
    schwab.on("GET", HISTORY, {**SCHWAB_BARS, "candles": rows})
    source, _ = history_chain(tmp_path)
    series = source.history("AAPL", "1h")
    h08, h09, h10, h11 = OPEN_930 - 5400, OPEN_930 - 1800, OPEN_930 + 1800, OPEN_930 + 5400
    assert series.candles == [Candle(h08, 1, 1, 1, 1, 1), Candle(h09, 10, 12, 9, 11, 100), Candle(h10, 11, 16, 10, 15, 500),
                              Candle(h11, 15, 15.5, 12, 13, 400)]
    assert series.clock_aligned is True


def test_the_daily_candle_of_schwab_is_returned_as_it_comes(tmp_path, schwab):
    """SPEC-016 3.4 point 2: date, open, high, low, close and volume of `pricehistory` daily are not touched."""
    row = bar(OPEN_930 - 14 * 3600 + 5 * 3600, 10.01, 12.02, 9.03, 11.04, 123456)       # a time of day nobody normalizes
    schwab.on("GET", HISTORY, {**SCHWAB_BARS, "candles": [row]})
    source, _ = history_chain(tmp_path)
    assert source.history("AAPL", "1D").candles == [Candle(row["datetime"] // 1000, 10.01, 12.02, 9.03, 11.04, 123456)]


def test_empty_true_is_no_data_and_not_an_error_so_the_next_source_answers(tmp_path, schwab):
    schwab.on("GET", HISTORY, {"symbol": "AAPL", "empty": True, "candles": []})
    source, yahoo = history_chain(tmp_path)
    with pytest.raises(MarketError):                                  # the broker is the only source: no Yahoo (SPEC-016 3.7)
        source.history("AAPL", "5m")
    assert yahoo.calls == [] and len(schwab.calls) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500, 404])
def test_a_failure_of_schwab_is_skipped_without_an_error_and_remembered(tmp_path, schwab, status):
    schwab.on("GET", HISTORY, (status, {"errors": [{"status": str(status)}]}))
    source, yahoo = history_chain(tmp_path)
    for _ in range(3):
        with pytest.raises(MarketError):                               # a connected broker that fails is not replaced by another source
            source.history("AAPL", "5m")
    assert yahoo.calls == []
    assert len([c for c in schwab.calls if c.path == HISTORY]) <= (2 if status == 401 else 1)     # a 401 is asked twice: renewed once


def test_the_bars_follow_the_use_bars_and_not_the_order_of_the_live_price(tmp_path, schwab):
    schwab.on("GET", HISTORY, {**SCHWAB_BARS, "candles": [bar(OPEN_930, 10, 12, 9, 11, 100)]})

    def alpaca(url, headers, body=None):
        stamp = datetime.fromtimestamp(OPEN_930, timezone.utc).isoformat().replace("+00:00", "Z")
        return 200, json.dumps({"bars": [{"t": stamp, "o": 1, "h": 2, "l": 1, "c": 2, "v": 3}]})

    keys = {"schwab": SCHWAB_KEYS, "alpaca": json.dumps({"key_id": "AKTESTKEYID12345", "secret": "alpaca-secret-67890"})}

    def build(order, name):
        return ConnectedSource(Yahoo(), Config(keys=keys, order=order), get_catalog(), tmp_path / name, fetch=alpaca,
                               clock=lambda: NOW)

    # SPEC-016 3.6: with a broker connected it is the only source of the candles, whatever the old order of use says
    assert build({"equity": ["alpaca", "schwab"], "bars": ["schwab", "alpaca"]}, "a").history("AAPL", "5m").source == "Schwab"
    assert build({"equity": ["schwab", "alpaca"], "bars": ["alpaca", "schwab"]}, "b").history("AAPL", "5m").source == "Schwab"
    assert build({"bars": ["alpaca", "schwab"]}, "c").providers_for("AAPL") == ["schwab"]


def test_schwab_is_not_asked_without_a_session_or_when_it_is_switched_off(tmp_path, schwab):
    source, yahoo = history_chain(tmp_path, signed=False)
    assert source.providers_for("AAPL") == [] and source.history("AAPL", "5m").source == "Yahoo Finance"
    source, _ = history_chain(tmp_path / "off", off=("schwab",))
    assert source.providers_for("AAPL") == []
    source, _ = history_chain(tmp_path / "nokey", keys={})
    assert source.providers_for("AAPL") == [] and schwab.calls == []


def test_schwab_serves_stocks_etfs_and_futures_but_not_crypto(tmp_path, schwab):
    from hokma.market.history import EXCLUSIVE_CLASSES, HISTORY_CLASSES

    assert HISTORY_CLASSES["schwab"] == ("equity", "future") and EXCLUSIVE_CLASSES == ("equity",)
    source, yahoo = history_chain(tmp_path)
    assert "schwab" not in source.providers_for("BTC-USD") and source.providers_for("NQ=F") == ["schwab"]


def test_futures_candles_ask_schwab_for_the_root_and_say_schwab(tmp_path, schwab):
    schwab.on("GET", HISTORY, {**SCHWAB_BARS, "symbol": "/NQ", "candles": [bar(OPEN_930, 10, 12, 9, 11, 100)]})
    source, yahoo = history_chain(tmp_path)
    series = source.history("NQ=F", "5m")
    assert series.source == "Schwab" and series.kind == "future" and yahoo.calls == []
    assert schwab.calls[0].query["symbol"] == "/NQ"


@pytest.mark.parametrize("answer", [(400, {"errors": [{"status": "400"}]}), (404, {"errors": [{"status": "404"}]}), (500, {}),
                                    (403, {"errors": [{"status": "403"}]}), {"symbol": "/NQ", "empty": True, "candles": []}])
def test_futures_without_bars_at_schwab_fall_to_yahoo_which_says_its_name(tmp_path, schwab, answer):
    schwab.on("GET", HISTORY, answer)
    source, yahoo = history_chain(tmp_path)
    assert source.history("NQ=F", "5m").source == "Yahoo Finance" and ("NQ=F", "5m") in yahoo.calls


def test_a_futures_refusal_does_not_close_schwab_for_stocks(tmp_path, schwab):
    schwab.on("GET", HISTORY, (403, {"errors": [{"status": "403"}]}))
    source, yahoo = history_chain(tmp_path)
    assert source.history("NQ=F", "5m").source == "Yahoo Finance"
    assert source.providers_for("AAPL") == ["schwab"] and "schwab" not in source._denied
    with pytest.raises(MarketError):                                  # stocks keep their rule: the broker is the only source
        source.history("AAPL", "5m")


# ------------------------------------------------------------------------------------------ 5.6 the option chain

def contract(kind: str, strike: float, **over) -> dict:
    letter = kind[0].upper()
    base = {"putCall": kind.upper(), "symbol": f"AAPL  261218{letter}{int(strike * 1000):08d}", "description": "test",
            "bidPrice": 5.0, "askPrice": 5.2, "lastPrice": 5.1, "markPrice": 5.1, "bidSize": 10, "askSize": 12,
            "totalVolume": 150, "quoteTimeInLong": QUOTE_MS, "tradeTimeInLong": QUOTE_MS - 1000, "volatility": 25.5,
            "delta": 0.5, "gamma": 0.01, "theta": -0.05, "vega": 0.2, "rho": 0.01, "openInterest": 1234.0,
            "strikePrice": strike, "expirationDate": "2026-12-18T21:00:00.000+00:00", "daysToExpiration": 28,
            "multiplier": 100.0, "isInTheMoney": False}
    return {**base, **over}


def chain_body(**over) -> dict:
    body = {
        "symbol": "AAPL", "status": "SUCCESS", "strategy": "SINGLE", "isDelayed": False, "underlyingPrice": 228.5,
        "underlying": {"symbol": "AAPL", "last": 228.5, "bid": 228.48, "ask": 228.52, "bidSize": 3, "askSize": 4,
                       "change": -0.65, "openPrice": 229.0, "highPrice": 229.9, "lowPrice": 227.8, "totalVolume": 731132,
                       "quoteTime": QUOTE_MS, "delayed": False},
        "callExpDateMap": {"2026-12-18:28": {
            "220.0": [contract("call", 220, bidPrice=11.0, askPrice=11.2, lastPrice=11.1, delta=0.8, gamma=0.012,
                               volatility=25.5, isInTheMoney=True)],
            "230.0": [contract("call", 230, bidPrice=-999.0, askPrice=0.5, lastPrice=0.45, delta=-999.0, gamma=float("nan"),
                               theta=-999.0, vega=-999.0, volatility=-999.0, totalVolume=0, openInterest=0.0)]}},
        "putExpDateMap": {"2026-12-18:28": {
            "220.0": [contract("put", 220, delta=-0.2, volatility=27.0)],
            "230.0": [contract("put", 230, bidPrice=3.0, askPrice=3.2, lastPrice=3.1, delta=-0.6, volatility=30.0,
                               isInTheMoney=True)]}},
    }
    return {**body, **over}


def connect_schwab(monkeypatch, *, alpaca: bool = False, now: float = OPEN_NOW, **kwargs):
    """The options module with Schwab connected (and Alpaca too when asked): returns the fake Alpaca of options_support."""
    fake = install(monkeypatch, DEMO_KEY if alpaca else None, now=now, **kwargs)
    fake.connections.keys["schwab"] = SCHWAB_KEYS
    endpoints.set_clock(lambda: datetime.fromtimestamp(fake.now, timezone.utc))
    return fake


def test_the_tool_reads_a_chain_of_schwab_with_the_names_of_ref_a6(monkeypatch, schwab):
    connect_schwab(monkeypatch)
    schwab.on("GET", CHAINS, chain_body())
    out = options.options_chain("AAPL", expiration="2026-12-18", limit=200)
    call = paths(schwab, CHAINS)[0]
    assert call.query == {"symbol": "AAPL", "contractType": "ALL", "strategy": "SINGLE", "includeUnderlyingQuote": "true",
                          "fromDate": "2026-12-18", "toDate": "2026-12-18"}
    assert (out["source"], out["price_feed"], out["greeks"], out["note"]) == ("Schwab", "schwab", "provider", None)
    assert out["underlying"] == "AAPL" and out["underlying_price"] == 228.5 and out["delayed"] is False
    assert out["scanned"] == out["matched"] == 4 and out["as_of"] == QUOTE_TEXT and out["market_open"] is None
    rows = {(c["type"], c["strike"]): c for c in out["contracts"]}
    first = rows[("call", 220.0)]
    assert list(first) == ["symbol", "type", "strike", "expiration", "days", "bid", "ask", "mid", "spread_pct", "last",
                           "volume", "open_interest", "open_interest_date", "delta", "gamma", "theta", "vega", "iv",
                           "quote_time"]
    assert first["symbol"] == "AAPL  261218C00220000" and first["expiration"] == "2026-12-18" and first["days"] == 28
    assert (first["bid"], first["ask"], first["mid"], first["spread_pct"], first["last"]) == (11.0, 11.2, 11.1, 1.8, 11.1)
    assert first["volume"] == 150 and first["open_interest"] == 1234 and isinstance(first["open_interest"], int)
    assert first["open_interest_date"] is None
    assert (first["delta"], first["gamma"], first["theta"], first["vega"]) == (0.8, 0.012, -0.05, 0.2)
    assert first["iv"] == 0.255                                            # volatility 25.5 in percent, as a fraction
    assert first["quote_time"] == QUOTE_TEXT                               # quoteTimeInLong, 64 bits
    assert [(c["type"], c["strike"]) for c in out["contracts"]] == [("call", 220.0), ("put", 220.0), ("call", 230.0),
                                                                     ("put", 230.0)]


def test_the_sentinel_and_the_values_that_are_not_finite_become_null(monkeypatch, schwab):
    connect_schwab(monkeypatch)
    schwab.on("GET", CHAINS, chain_body())
    row = next(c for c in options.options_chain("AAPL", expiration="2026-12-18")["contracts"]
               if (c["type"], c["strike"]) == ("call", 230.0))
    assert (row["bid"], row["delta"], row["gamma"], row["theta"], row["vega"], row["iv"]) == (None,) * 6
    assert row["mid"] is None and row["spread_pct"] is None                   # a side without price has no mid
    assert (row["ask"], row["last"], row["volume"], row["open_interest"]) == (0.5, 0.45, 0, 0)


def test_the_volatility_unit_is_the_one_fixed_by_analytics(monkeypatch, schwab):
    from hokma.schwab import analytics

    connect_schwab(monkeypatch)
    schwab.on("GET", CHAINS, chain_body())
    monkeypatch.setattr(analytics, "VOLATILITY_UNIT", "fraction")
    row = next(c for c in options.options_chain("AAPL", expiration="2026-12-18")["contracts"] if c["strike"] == 220.0)
    assert row["iv"] == 25.5
    monkeypatch.setattr(analytics, "VOLATILITY_UNIT", "percent")
    options.forget_cache()
    assert next(c for c in options.options_chain("AAPL", expiration="2026-12-18")["contracts"])["iv"] == 0.255


def test_the_window_of_days_and_the_type_go_to_schwab_as_dates_and_contract_type(monkeypatch, schwab):
    connect_schwab(monkeypatch)
    schwab.on("GET", CHAINS, chain_body())
    options.options_chain("AAPL", type="put", min_days=7, max_days=45)
    query = paths(schwab, CHAINS)[0].query
    assert query["contractType"] == "PUT" and query["fromDate"] == "2026-11-27" and query["toDate"] == "2027-01-04"


def test_the_filters_of_1_0_8_work_on_the_chain_of_schwab(monkeypatch, schwab):
    connect_schwab(monkeypatch)
    schwab.on("GET", CHAINS, chain_body())
    out = options.options_chain("AAPL", expiration="2026-12-18", min_delta=0.5, max_delta=0.7, min_open_interest=1000)
    assert [(c["type"], c["strike"]) for c in out["contracts"]] == [("put", 230.0)]
    assert out["scanned"] == 4 and out["matched"] == 1 and out["contracts"][0]["delta"] == -0.6


def test_a_chain_that_is_delayed_says_so(monkeypatch, schwab):
    connect_schwab(monkeypatch)
    schwab.on("GET", CHAINS, chain_body(isDelayed=True))
    assert options.options_chain("AAPL", expiration="2026-12-18")["delayed"] is True
    options.forget_cache()
    body = chain_body()
    body["underlying"]["delayed"] = True
    schwab.on("GET", CHAINS, body)
    assert options.options_chain("AAPL", expiration="2026-12-18")["delayed"] is True


def test_a_symbol_without_chain_is_an_empty_answer_and_not_an_error(monkeypatch, schwab):
    connect_schwab(monkeypatch)
    schwab.on("GET", CHAINS, (404, {"errors": [{"status": "404"}]}))
    out = options.options_chain("AAPL")
    assert out["contracts"] == [] and out["scanned"] == 0 and out["source"] == "Schwab"


def test_the_expirations_accept_expirationDate_and_expiration(monkeypatch, schwab):
    connect_schwab(monkeypatch)
    schwab.on("GET", EXPIRATIONS, {"status": "SUCCESS", "expirationList": [
        {"expirationDate": "2026-11-27", "daysToExpiration": 7, "expirationType": "W", "standard": True},
        {"expiration": "2026-12-18", "daysToExpiration": 28, "expirationType": "S", "standard": True},
        {"expiration": "2026-12-18", "expirationDate": "2026-12-18", "daysToExpiration": 28},       # both names, one date
        {"expirationDate": "2026-11-19", "daysToExpiration": -1},                  # already expired
        {"expirationDate": "2028-12-15", "daysToExpiration": 756}]})               # more than 400 days away
    out = options.expirations("AAPL")
    assert paths(schwab, EXPIRATIONS)[0].query == {"symbol": "AAPL"}
    assert out == {"symbol": "AAPL", "source": "Schwab", "expirations": [
        {"date": "2026-11-27", "days": 7, "multiplier": 100}, {"date": "2026-12-18", "days": 28, "multiplier": 100}]}


def test_the_dashboard_chain_trims_the_strikes_around_the_price_and_labels_schwab(monkeypatch, schwab):
    connect_schwab(monkeypatch)
    schwab.on("GET", CHAINS, chain_body())
    out = options.chain("AAPL", "2026-12-18", "14")
    query = paths(schwab, CHAINS)[0].query
    assert "strikeCount" not in query and query["fromDate"] == query["toDate"] == "2026-12-18"        # every strike of Schwab, cut here (SPEC-016 4.3)
    assert (out["source"], out["price_feed"], out["greeks"], out["delayed"]) == ("Schwab", "schwab", "provider", False)
    assert out["underlying_price"] == 228.5 and out["as_of"] == QUOTE_TEXT and out["notes"] == []
    group = out["expirations"][0]
    assert group["date"] == "2026-12-18" and group["days"] == 28 and group["multiplier"] == 100
    assert [r["strike"] for r in group["rows"]] == [220.0, 230.0]
    years = options.years_to_expiry(date(2026, 12, 18))
    call = group["rows"][0]["call"]
    assert call["itm"] is True and call["intrinsic"] == 8.5 and call["iv"] == 0.255 and call["delta"] == 0.8
    assert call["prob_itm"] == options.contract_prob_itm("call", 228.5, 220.0, call["bid"], call["ask"], 0.255, years) and call["open_interest"] == 1234
    assert call["quote_time"] == QUOTE_TEXT and call["bid"] == 11.0 and call["theta"] == -0.05
    far_call = group["rows"][1]["call"]
    assert far_call["itm"] is False and far_call["intrinsic"] == 0.0 and far_call["iv"] is None and far_call["prob_itm"] == options.contract_prob_itm("call", 228.5, 230.0, far_call["bid"], far_call["ask"], None, years)       # no IV from Schwab, the mark gives its own
    put = group["rows"][1]["put"]
    assert put["itm"] is True and put["intrinsic"] == 1.5 and put["prob_itm"] == options.contract_prob_itm("put", 228.5, 230.0, put["bid"], put["ask"], 0.3, years)
    assert group["iv"] == 0.3                                                   # the strike nearest the price has the put's IV
    assert group["expected_move"] == options.expected_move(228.5, 0.3, years)


def test_the_dashboard_chain_cuts_to_the_number_of_strikes_asked(monkeypatch, schwab):
    connect_schwab(monkeypatch)
    calls = {f"{s}.0": [contract("call", s)] for s in range(200, 260, 5)}
    puts = {f"{s}.0": [contract("put", s)] for s in range(200, 260, 5)}
    schwab.on("GET", CHAINS, chain_body(callExpDateMap={"2026-12-18:28": calls}, putExpDateMap={"2026-12-18:28": puts}))
    rows = options.chain("AAPL", "2026-12-18", "6")["expirations"][0]["rows"]
    assert [r["strike"] for r in rows] == [215.0, 220.0, 225.0, 230.0, 235.0, 240.0]            # three below, three from the price up
    assert "strikeCount" not in paths(schwab, CHAINS)[0].query                                 # the list is whole; the cut is made here
    options.forget_cache()
    every = options.chain("AAPL", "2026-12-18", "all")["expirations"][0]["rows"]
    assert len(every) == 12 and "strikeCount" not in paths(schwab, CHAINS)[1].query


def test_the_dashboard_chain_asks_once_per_expiration_and_keeps_no_30_second_memory(monkeypatch, schwab):
    """SPEC-016 4.3: the structure is asked when the chain opens, when an expiration changes and on the refresh button; the prices
    do not come from a memory of 30 seconds."""
    connect_schwab(monkeypatch)
    schwab.on("GET", CHAINS, chain_body())
    options.chain("AAPL", "2026-12-18,2027-01-15", "14")
    assert [c.query["fromDate"] for c in paths(schwab, CHAINS)] == ["2026-12-18", "2027-01-15"]
    options.chain("AAPL", "2026-12-18,2027-01-15", "14")
    assert len(paths(schwab, CHAINS)) == 4


def test_the_underlying_bar_comes_from_the_quote_of_schwab(monkeypatch, schwab):
    connect_schwab(monkeypatch)
    schwab.on("GET", QUOTES, {"AAPL": quote_item("AAPL", 228.4, 731132)})
    bar_ = options.underlying("AAPL")
    assert paths(schwab, QUOTES)[0].query == {"symbols": "AAPL", "fields": "quote"}
    assert bar_ == {"symbol": "AAPL", "last": 228.4, "last_exchange": None, "net_change": 0.5, "bid": 228.39,
                    "bid_exchange": None, "ask": 228.41, "ask_exchange": None, "bid_size": 3, "ask_size": 4,
                    "volume": 731132, "open": 227.4, "high": 229.4, "low": 226.4, "as_of": "2026-11-20T20:59:58Z",
                    "delayed": False}
    schwab.on("GET", QUOTES, {"AAPL": quote_item("AAPL", realtime=False)})
    assert options.underlying("AAPL")["delayed"] is True


# ------------------------------------------------------------------------------------------ the order of the use options

def test_with_schwab_first_in_the_order_it_is_the_source_and_alpaca_is_not_asked(monkeypatch, schwab):
    alpaca = connect_schwab(monkeypatch, alpaca=True)
    schwab.on("GET", CHAINS, chain_body())
    out = options.options_chain("AAPL", expiration="2026-12-18")
    assert out["source"] == "Schwab" and out["price_feed"] == "schwab" and out["greeks"] == "provider"
    assert alpaca.calls == [] and out["note"] is None and "estimated" not in json.dumps(out)


def test_when_alpaca_is_first_and_fails_schwab_is_the_next_source(monkeypatch, schwab):
    alpaca = connect_schwab(monkeypatch, alpaca=True)
    alpaca.connections.options = ("alpaca", "schwab")
    alpaca.status["paper-api.alpaca.markets"] = 500
    schwab.on("GET", CHAINS, chain_body())
    out = options.options_chain("AAPL", expiration="2026-12-18")
    assert out["source"] == "Schwab" and paths(schwab, CHAINS)


def test_with_alpaca_first_in_the_order_schwab_is_not_asked(monkeypatch, schwab):
    alpaca = connect_schwab(monkeypatch, alpaca=True)
    alpaca.connections.options = ("alpaca", "schwab")
    out = options.options_chain("AAPL", limit=1)
    assert out["source"] == "Alpaca" and out["price_feed"] == "indicative" and out["greeks"] == "estimated"
    assert paths(schwab, CHAINS) == []


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_a_session_that_ended_a_limit_or_a_server_error_passes_to_alpaca_in_the_same_call(monkeypatch, schwab, status):
    alpaca = connect_schwab(monkeypatch, alpaca=True)
    schwab.on("GET", CHAINS, (status, {"errors": [{"status": str(status)}]}))
    out = options.options_chain("AAPL", expiration="2026-12-18", limit=1)
    assert out["source"] == "Alpaca" and out["contracts"] and alpaca.calls
    assert len(paths(schwab, CHAINS)) >= 1


def test_the_expirations_and_the_chain_of_the_dashboard_also_pass_to_alpaca(monkeypatch, schwab):
    connect_schwab(monkeypatch, alpaca=True)
    schwab.on("GET", EXPIRATIONS, (401, {}))
    schwab.on("GET", CHAINS, (401, {}))
    assert options.expirations("AAPL")["source"] == "Alpaca"
    assert options.chain("AAPL", "2026-12-18", "6")["source"] == "Alpaca"


def test_the_bar_passes_to_alpaca_too(monkeypatch, schwab):
    alpaca = connect_schwab(monkeypatch, alpaca=True)
    schwab.on("GET", QUOTES, (500, {}))
    out = options.underlying("AAPL")
    assert out["bid"] == 228.33 and any("/snapshot" in url for url, _ in alpaca.calls)


def test_a_desk_that_does_not_use_schwab_goes_to_alpaca(monkeypatch, schwab):
    alpaca = connect_schwab(monkeypatch, alpaca=True, allowed={"alpaca"})
    schwab.on("GET", CHAINS, chain_body())
    out = options.options_chain("AAPL", limit=1, desk_id=7)
    assert out["source"] == "Alpaca" and paths(schwab, CHAINS) == [] and alpaca.calls


def test_a_desk_that_uses_schwab_gets_schwab(monkeypatch, schwab):
    connect_schwab(monkeypatch, alpaca=True, allowed={"alpaca", "schwab"})
    schwab.on("GET", CHAINS, chain_body())
    assert options.options_chain("AAPL", expiration="2026-12-18", desk_id=7)["source"] == "Schwab"


def test_a_desk_without_either_source_is_told_so(monkeypatch, schwab):
    connect_schwab(monkeypatch, allowed={"finnhub"})
    with pytest.raises(OptionsError) as error:
        options.options_chain("AAPL", desk_id=7)
    assert error.value.code == "desk" and error.value.status == 409 and schwab.calls == []


def test_schwab_without_a_session_or_switched_off_is_not_asked(monkeypatch, schwab):
    fake = connect_schwab(monkeypatch, alpaca=True)
    fake.connections.signed = False
    assert options.options_chain("AAPL", limit=1)["source"] == "Alpaca"
    fake.connections.signed, fake.connections.off = True, ("schwab",)
    options.forget_cache()
    assert options.options_chain("AAPL", limit=1, max_days=61)["source"] == "Alpaca" and schwab.calls == []


def test_without_any_source_the_code_is_no_connection_with_the_new_text(monkeypatch, schwab):
    install(monkeypatch, None)
    with pytest.raises(OptionsError) as error:
        options.options_chain("AAPL")
    assert error.value.code == "no_connection" and error.value.status == 409
    assert str(error.value) == "The option chain needs Schwab or Alpaca Trading: connect one of them in Connections."
    assert schwab.calls == []


@pytest.mark.parametrize("status,code,http_status,text", [
    (401, "schwab.auth", 401, "Your Schwab session ended or is not valid. Sign in again in Connections."),
    (403, "schwab.auth", 401, "Your Schwab session ended or is not valid. Sign in again in Connections."),
    (429, "schwab.rate_limited", 429, "Schwab is limiting requests. Wait a minute and try again."),
    (500, "schwab.upstream", 502, "The Schwab option chain could not be loaded. Try again later."),
    (400, "schwab.upstream", 502, "The Schwab option chain could not be loaded. Try again later.")])
def test_when_schwab_is_the_only_source_its_failure_has_a_short_code_a_status_and_a_text(monkeypatch, schwab, status, code,
                                                                                         http_status, text):
    connect_schwab(monkeypatch)
    schwab.on("GET", CHAINS, (status, {"errors": [{"status": str(status)}]}))
    with pytest.raises(OptionsError) as error:
        options.options_chain("AAPL")
    assert (error.value.code, error.value.status, str(error.value)) == (code, http_status, text)


def test_the_routes_send_the_schwab_codes_with_the_options_prefix_so_the_client_keeps_its_session(monkeypatch, schwab):
    connect_schwab(monkeypatch)
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=1000, window=60))
    client = TestClient(create_app())
    schwab.on("GET", CHAINS, (401, {}))
    schwab.on("GET", QUOTES, (401, {}))
    response = client.get("/api/market/options/chain?symbol=AAPL&expirations=2026-12-18", headers=AUTH)
    assert response.status_code == 401 and response.json()["detail"]["code"] == "options.schwab.auth"
    assert response.json()["detail"]["message"].startswith("Your Schwab session ended")
    schwab.on("GET", EXPIRATIONS, (429, {}))
    http.reset_limits()
    response = client.get("/api/market/options/expirations?symbol=AAPL", headers=AUTH)
    assert response.status_code == 429 and response.json()["detail"]["code"] == "options.schwab.rate_limited"
    schwab.on("GET", CHAINS, chain_body())
    http.reset_limits()
    options.forget_cache()
    body = client.get("/api/market/options/chain?symbol=AAPL&expirations=2026-12-18&strikes=6", headers=AUTH)
    assert body.status_code == 200 and body.json()["price_feed"] == "schwab" and body.json()["greeks"] == "provider"
    assert body.json()["source"] == "Schwab"


# ------------------------------------------------------------------------------------------ 16.15 market closed

THANKSGIVING = ms(2026, 11, 26, 15, 0, 0) / 1000                 # Thursday 10:00 in New York, a holiday: the NYSE is closed


def hours(day: str, *, is_open: bool, offset: str = "-05:00") -> dict:
    entry = {"date": day, "marketType": "OPTION", "product": "EQO", "productName": "equity option", "isOpen": is_open}
    if is_open:
        entry["sessionHours"] = {"regularMarket": [{"start": f"{day}T09:30:00{offset}", "end": f"{day}T16:00:00{offset}"}]}
    return {"option": {"EQO": entry}}


def test_a_holiday_on_a_weekday_is_market_closed_by_the_hours_of_schwab(monkeypatch, schwab):
    connect_schwab(monkeypatch, now=THANKSGIVING)
    schwab.on("GET", HOURS_OPTION, hours("2026-11-26", is_open=False))
    schwab.on("GET", CHAINS, chain_body())
    out = options.options_chain("AAPL", expiration="2026-12-18")
    assert out["market_open"] is False
    assert paths(schwab, HOURS_OPTION)[0].query == {"date": "2026-11-26"}
    options.forget_cache()
    assert options.chain("AAPL", "2026-12-18", "6")["market_open"] is False


def test_a_regular_day_is_open_inside_the_session_and_closed_outside_it(monkeypatch, schwab):
    fake = connect_schwab(monkeypatch)
    schwab.on("GET", HOURS_OPTION, hours("2026-11-20", is_open=True))
    schwab.on("GET", CHAINS, chain_body())
    assert options.options_chain("AAPL", expiration="2026-12-18")["market_open"] is True        # 11:00 in New York
    fake.now = ms(2026, 11, 20, 22, 0, 0) / 1000                                                 # 17:00: after the close
    options.forget_cache()
    assert options.options_chain("AAPL", expiration="2026-12-18")["market_open"] is False


def test_the_hours_are_asked_once_per_day_and_a_failure_is_unknown_not_a_guess(monkeypatch, schwab):
    fake = connect_schwab(monkeypatch)
    schwab.on("GET", CHAINS, chain_body())
    schwab.on("GET", HOURS_OPTION, (500, {}))
    assert options.options_chain("AAPL", expiration="2026-12-18")["market_open"] is None        # Schwab failed: unknown (no fixed 09:30 to 16:00)
    options.forget_cache()
    options.options_chain("AAPL", expiration="2026-12-18")
    assert len(paths(schwab, HOURS_OPTION)) == 1                                                 # the failure is remembered a minute
    schwab.on("GET", HOURS_OPTION, hours("2026-11-20", is_open=True))
    fake.now += 61
    assert options.options_chain("AAPL", expiration="2026-12-18")["market_open"] is True
    fake.now += 31                                                                  # the chain is asked again, the hours are not
    options.options_chain("AAPL", expiration="2026-12-18")
    assert len(paths(schwab, HOURS_OPTION)) == 2 and len(paths(schwab, CHAINS)) == 4


def test_without_schwab_the_market_state_is_what_alpaca_says_and_unknown_without_it(monkeypatch, schwab):
    from hokma.market import alpaca_hours

    install(monkeypatch)
    assert options.options_chain("AAPL", limit=1)["market_open"] is None and paths(schwab, HOURS_OPTION) == []
    options.forget_cache()
    monkeypatch.setattr(alpaca_hours, "phase", lambda *a, **k: "core")
    assert options.options_chain("AAPL", limit=1)["market_open"] is True


def test_the_tool_never_leaks_a_token_a_key_or_an_account_number(monkeypatch, schwab):
    connect_schwab(monkeypatch)
    schwab.on("GET", CHAINS, chain_body())
    text = json.dumps([options.options_chain("AAPL", expiration="2026-12-18"), options.chain("AAPL", "2026-12-18", "6")])
    for secret in (schwab.token, "client-id-0001", "client-secret-0001"):
        assert secret not in text
