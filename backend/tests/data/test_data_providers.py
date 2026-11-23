"""Provider contract, taxonomy, router, bar identity, retries (SPEC-017: AH-42, AH-44, OA-64, TA-52, TA-54, TA-55, TA-56, TA-59, TA-60)."""
from datetime import datetime, timezone

import pandas as pd
import pytest

from hokma.data import sentinels
from hokma.data.cache import ResultCache
from hokma.data.providers import (BarRef, BarService, CachedDataClient, DataClient, NoDataError, NotConfiguredError, ProviderConfig, RateLimitError,
                                  RawBars, check_status, is_empty, raise_for_empty, retry_with_backoff, route, vendor_reachable)

from .support import when

UTC = timezone.utc


class Client:
    name = "fake"

    def __init__(self, answers=None, error=None) -> None:
        self.answers, self.error, self.calls = answers if answers is not None else [1, 2], error, 0

    def fetch(self, category: str, **params):
        self.calls += 1
        if self.error:
            raise self.error
        return self.answers


def test_protocol_empty_is_absence_and_failure_raises_AH_42() -> None:
    assert isinstance(Client(), DataClient)
    assert is_empty([]) and is_empty(None) and is_empty(pd.DataFrame()) and not is_empty([0]) and not is_empty(pd.DataFrame({"a": [1]}))
    empty = route("t", {"p": lambda: []}, ["p"])
    assert empty.status == sentinels.NO_DATA                    # empty means it does not exist
    failing = route("t", {"p": lambda: (_ for _ in ()).throw(RateLimitError("429"))}, ["p"])
    assert failing.status == sentinels.DATA_UNAVAILABLE         # a failure is never an empty answer


def test_cached_client_only_caches_successes_AH_44() -> None:
    cache = ResultCache()
    failing = Client(error=RateLimitError("429"))
    with pytest.raises(RateLimitError):
        CachedDataClient(failing, cache).fetch("x", symbol="A")
    with pytest.raises(RateLimitError):
        CachedDataClient(failing, cache).fetch("x", symbol="A")
    assert failing.calls == 2                                   # the failure was not remembered
    good = Client([1, 2, 3])
    cached = CachedDataClient(good, cache)
    assert cached.fetch("x", symbol="A") == [1, 2, 3] and cached.fetch("x", symbol="A") == [1, 2, 3]
    assert good.calls == 1
    assert cached.fetch("x", symbol="B") == [1, 2, 3] and good.calls == 2     # another request is another key
    refreshed = CachedDataClient(good, cache, refresh=True)
    refreshed.fetch("x", symbol="A")
    assert good.calls == 3


def test_bar_id_carries_the_source_OA_64() -> None:
    a, b = BarRef.parse("yahoo|AAPL"), BarRef.parse("fmp|AAPL")
    assert (a.provider, a.symbol) == ("yahoo", "AAPL") and str(a) == "yahoo|AAPL" and a != b
    assert BarRef.parse("coinbase|BTC/USD").symbol == "BTC/USD"        # the symbol may hold a slash
    for bad in ("AAPL", "|AAPL", "yahoo|", ""):
        with pytest.raises(ValueError):
            BarRef.parse(bad)


def test_bar_service_uses_one_source_and_never_swaps_OA_64() -> None:
    frame = pd.DataFrame({"close": [1.0]})
    seen = []

    def good(symbol, interval):
        seen.append(("good", symbol, interval))
        return RawBars(frame, when("2024-01-01"))

    def broken(symbol, interval):
        seen.append(("broken", symbol, interval))
        raise RateLimitError("429")

    service = BarService({"good": good, "broken": broken})
    assert service.fetch("good|A", "1D").frame is frame
    with pytest.raises(RateLimitError):
        service.fetch("broken|A", "1D")
    assert seen == [("good", "A", "1D"), ("broken", "A", "1D")]        # the broken source was not replaced by the good one
    with pytest.raises(NotConfiguredError):
        service.fetch("unknown|A", "1D")


def test_router_tries_exactly_the_chain_TA_52() -> None:
    called = []

    def make(name, outcome):
        def run():
            called.append(name)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return run

    impls = {"a": make("a", NoDataError("S", None, "no rows")), "b": make("b", [1]), "c": make("c", [9])}
    result = route("t", impls, ["a", "b"])
    assert (result.status, result.source, result.value) == (sentinels.OK, "b", [1])
    assert called == ["a", "b"]                                         # c was not chosen: never tried
    only_a = route("t", impls, ["a"])
    assert only_a.status == sentinels.NO_DATA and only_a.reason == "no rows" and "c" not in called
    with pytest.raises(ValueError):
        route("t", impls, ["z"])


def test_router_priority_no_data_then_unavailable_then_key_TA_52() -> None:
    impls = {"limited": lambda: (_ for _ in ()).throw(RateLimitError("429")), "nokey": lambda: (_ for _ in ()).throw(NotConfiguredError("nokey")),
             "empty": lambda: []}
    assert route("t", impls, ["limited", "empty"]).status == sentinels.NO_DATA
    limited = route("t", impls, ["nokey", "limited"])
    assert (limited.status, limited.reason, limited.source) == (sentinels.DATA_UNAVAILABLE, "rate_limited", "limited")
    nokey = route("t", impls, ["nokey"])
    assert (nokey.status, nokey.reason, nokey.source) == (sentinels.DATA_UNAVAILABLE, sentinels.REASON_KEY_MISSING, "nokey")


def test_taxonomy_has_three_reactions_TA_54() -> None:
    no_data = NoDataError("AAPL", "AAPL", "stale")
    assert "AAPL" in str(no_data) and no_data.detail == "stale"
    assert str(NoDataError("XAUUSD", "GC=F", "no rows")).count("GC=F") == 1
    assert RateLimitError("x").reason == "rate_limited" and RateLimitError("x", reason="provider_unreachable").reason == "provider_unreachable"
    assert isinstance(NotConfiguredError("p"), ValueError)
    for status, expected in ((401, NotConfiguredError), (403, NotConfiguredError), (429, RateLimitError), (503, RateLimitError), (418, NoDataError)):
        with pytest.raises(expected):
            check_status("p", status, b"body")
    check_status("p", 200, b"")


def test_optional_category_degrades_and_core_raises_TA_55() -> None:
    def boom():
        raise RuntimeError("unexpected: 'leaked-key' in the message")

    macro = route("m", {"fred": boom}, ["fred"], category="macro")
    assert macro.status == sentinels.DATA_UNAVAILABLE and macro.reason == "optional_category_failed"     # flavour data never aborts a run
    with pytest.raises(RuntimeError):
        route("m", {"fred": boom}, ["fred"], category="bars")                                            # a broken core category is loud


def test_provider_config_per_category_and_tool_TA_56() -> None:
    config = ProviderConfig(categories={"news": "a,b"}, tools={"get_x": ["only_this"]})
    assert config.chain_for("news") == ("a", "b")
    assert config.chain_for("news", "get_y") == ("a", "b")
    assert config.chain_for("news", "get_x") == ("only_this",)                    # the tool wins over its category
    assert config.chain_for("unknown_category", available=["p", "q"]) == ("p", "q")   # default: every provider that serves it
    assert ProviderConfig().chain_for("bars") == ("hokma_market",)
    assert ProviderConfig(categories={"fundamentals": "default"}).chain_for("fundamentals", available=["z"]) == ("z",)


def test_vendor_reachable_tells_outage_from_absence_TA_59() -> None:
    assert vendor_reachable("https://x.test", probe=lambda url, timeout: True) is True
    with pytest.raises(RateLimitError) as outage:
        raise_for_empty("AAPL", "AAPL", "price rows", host="https://x.test", reachable=lambda host: False)
    assert outage.value.reason == "provider_unreachable"
    with pytest.raises(NoDataError) as absence:
        raise_for_empty("ZZZZ", "ZZZZ", "price rows", host="https://x.test", reachable=lambda host: True)
    assert "price rows" in absence.value.detail


def test_retry_with_backoff_doubles_and_gives_up_TA_60() -> None:
    sleeps, attempts = [], []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RateLimitError("429")
        return "ok"

    assert retry_with_backoff(flaky, base_delay=2.0, sleep=sleeps.append) == "ok"
    assert sleeps == [2.0, 4.0]
    sleeps.clear()
    with pytest.raises(RateLimitError):
        retry_with_backoff(lambda: (_ for _ in ()).throw(RateLimitError("429")), max_retries=3, base_delay=1.0, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0, 4.0]
    with pytest.raises(KeyError):                                     # other errors are not retried
        retry_with_backoff(lambda: (_ for _ in ()).throw(KeyError("x")), sleep=sleeps.append)
