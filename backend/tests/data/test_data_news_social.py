"""News, prediction markets, StockTwits and Reddit (SPEC-017: TA-77, TA-71, TA-72, TA-73)."""
import json

import pytest

from hokma.data import news as news_module, sentinels, social
from hokma.data.providers import NoDataError, RateLimitError
from hokma.data.social import parse_atom, stocktwits_symbol, strip_html

from .support import RecordedTransport, constant, fixture_bytes, fixture_json, no_network, when  # noqa: F401

NOW = when("2024-04-10T12:00:00")
clock = lambda: NOW


# ------------------------------------------------------------------------------------------------ TA-77
def test_article_shapes_are_normalised_and_titleless_ones_dropped_TA_77() -> None:
    nested = news_module.extract_article(fixture_json("yf_news.json")[0])
    assert nested == {"title": "ACME beats estimates", "summary": "Results above consensus.", "publisher": "Wire",
                      "link": "https://example.test/a", "published_at": "2024-03-27T14:00:00+00:00"}
    flat = news_module.extract_article(fixture_json("yf_news.json")[1])
    assert flat["publisher"] == "Desk" and flat["published_at"] == "2024-03-26T12:00:00+00:00"
    assert news_module.extract_article(fixture_json("yf_news.json")[4]) is None
    assert news_module.extract_article({"title": "Odd", "providerPublishTime": "garbage"})["published_at"] is None


def test_news_is_cut_to_the_window_and_as_of_TA_77() -> None:
    feed = lambda symbol: fixture_json("yf_news.json")
    result = news_module.news("ACME", as_of=when("2024-04-01"), start=when("2024-03-20"), fetch=feed, clock=clock)
    assert result.ok and result.source == "yahoo_news" and result.received_at == NOW and result.as_of == when("2024-04-01")
    same_day = news_module.news("ACME", as_of=when("2024-03-27T13:00:00"), start=when("2024-03-20"), fetch=feed, clock=clock)
    assert [a["title"] for a in same_day.value] == ["ACME announces buyback"]                       # 14:00 is after the 13:00 cut, though on the same day
    assert [a["title"] for a in result.value] == ["ACME beats estimates", "ACME announces buyback"]      # newest first; after-the-cut and undated items are out
    assert all(a["published_at"] <= "2024-04-01T23:59:59+00:00" for a in result.value)


def test_news_window_the_feed_never_observed_is_unavailable_not_an_absence_TA_77() -> None:
    feed = lambda symbol: fixture_json("yf_news.json")
    old = news_module.news("ACME", as_of=when("2023-01-10"), start=when("2023-01-01"), fetch=feed, clock=clock)
    assert (old.status, old.reason, old.source) == (sentinels.DATA_UNAVAILABLE, sentinels.REASON_NOT_OBSERVED, "yahoo_news")
    assert "not an absence" in old.warnings[0] and old.value is None
    older = {"title": "ACME older", "providerPublishTime": 1706788800}                              # 2024-02-01: the feed reaches back past the window
    quiet = news_module.news("ACME", as_of=when("2024-03-01"), start=when("2024-02-25"), fetch=lambda s: [older, fixture_json("yf_news.json")[1]], clock=clock)
    assert quiet.status == sentinels.NO_DATA and quiet.reason == sentinels.REASON_EMPTY              # coverage reaches the window: a real absence


def test_news_failures_carry_provider_and_reason_TA_77() -> None:
    throttled = news_module.news("ACME", as_of=when("2024-04-01"), fetch=lambda s: (_ for _ in ()).throw(RateLimitError("429")), clock=clock)
    assert (throttled.status, throttled.reason, throttled.source) == (sentinels.DATA_UNAVAILABLE, "rate_limited", "yahoo_news")
    absent = news_module.news("ZZZZ", as_of=when("2024-04-01"), fetch=lambda s: (_ for _ in ()).throw(NoDataError("ZZZZ", None, "unknown symbol")), clock=clock)
    assert (absent.status, absent.reason) == (sentinels.NO_DATA, "unknown symbol")


def test_global_news_is_deduplicated_and_cut_TA_77() -> None:
    seen = []

    def feed(query):
        seen.append(query)
        return fixture_json("yf_news.json")

    result = news_module.global_news(as_of=when("2024-04-01"), look_back_days=14, fetch=feed, clock=clock, limit=10)
    assert result.ok and len(seen) == len(news_module.DEFAULT_GLOBAL_QUERIES) and len(result.value) == 2    # five searches, same two headlines once
    assert news_module.global_news(as_of=when("2024-04-01"), fetch=lambda q: (_ for _ in ()).throw(RateLimitError("429")), clock=clock).status == sentinels.DATA_UNAVAILABLE


# ------------------------------------------------------------------------------------------------ TA-71
def test_prediction_markets_are_open_forward_looking_and_ranked_TA_71() -> None:
    transport = RecordedTransport(constant(200, fixture_bytes("polymarket_search.json")))
    result = social.prediction_markets("fed cut", as_of=NOW, transport=transport, clock=clock)
    assert result.ok and result.source == "polymarket" and len(result.value) == 1                   # the closed and the ended markets are dropped
    assert result.value[0]["probability"] == 0.62 and result.value[0]["volume"] == 250000 and "odds_are_live_not_a_forecast" in result.warnings
    assert transport.calls[0][1]["q"] == "fed cut"


def test_prediction_markets_are_denied_for_a_past_as_of_TA_71() -> None:
    transport = RecordedTransport(constant(200, fixture_bytes("polymarket_search.json")))
    past = social.prediction_markets("fed cut", as_of=when("2024-03-01"), transport=transport, clock=clock)
    assert (past.status, past.reason, past.value) == (sentinels.NO_DATA, sentinels.REASON_UNSUPPORTED, None) and "withheld" in past.warnings[0]
    assert transport.calls == []                                                                   # not even asked


def test_prediction_markets_failures_TA_71() -> None:
    assert social.prediction_markets("x", as_of=NOW, transport=RecordedTransport(constant(503)), clock=clock).reason == "provider_unreachable"
    assert social.prediction_markets("x", as_of=NOW, transport=RecordedTransport(constant(200, b"not json")), clock=clock).reason == "bad_response"
    assert social.prediction_markets("x", as_of=NOW, transport=RecordedTransport(constant(200, b'{"events": []}')), clock=clock).status == sentinels.NO_DATA


# ------------------------------------------------------------------------------------------------ TA-72
def test_stocktwits_counts_bullish_and_bearish_in_the_window_TA_72() -> None:
    transport = RecordedTransport(constant(200, fixture_bytes("stocktwits_stream.json")))
    result = social.stocktwits("ACME", as_of=when("2024-03-29"), start=when("2024-03-28"), transport=transport, clock=lambda: when("2024-03-29T18:00:00"))
    assert result.ok and result.source == "stocktwits"
    assert (result.value["bullish"], result.value["bearish"], result.value["unlabeled"]) == (1, 1, 1)   # the message after the cut is not counted
    assert result.value["bullish_pct"] == 33 and "after the cut" not in json.dumps(result.value)
    assert transport.calls[0][0].endswith("/ACME.json")


def test_stocktwits_symbol_for_crypto_and_failures_TA_72() -> None:
    assert stocktwits_symbol("BTC-USD") == "BTC.X" and stocktwits_symbol("btcusdt") == "BTC.X" and stocktwits_symbol("aapl") == "AAPL"
    down = social.stocktwits("ACME", as_of=NOW, transport=RecordedTransport(constant(503)), clock=clock)
    assert (down.status, down.reason, down.source) == (sentinels.DATA_UNAVAILABLE, "provider_unreachable", "stocktwits")
    assert "stocktwits unavailable" in down.warnings[0]                                              # a failure is not "no messages"
    old = social.stocktwits("ACME", as_of=when("2023-01-02"), start=when("2023-01-01"), transport=RecordedTransport(constant(200, fixture_bytes("stocktwits_stream.json"))), clock=clock)
    assert old.status == sentinels.DATA_UNAVAILABLE and old.reason == sentinels.REASON_NOT_OBSERVED
    empty = social.stocktwits("ACME", as_of=NOW, start=when("2024-04-09"), transport=RecordedTransport(constant(200, b'{"messages": []}')), clock=clock)
    assert empty.status == sentinels.DATA_UNAVAILABLE                                                # a feed with no coverage of the window cannot claim silence


# ------------------------------------------------------------------------------------------------ TA-73
def test_reddit_posts_parse_and_cut_TA_73() -> None:
    transport = RecordedTransport(constant(200, fixture_bytes("reddit_search.atom.xml")))
    result = social.reddit_posts("ACME", as_of=when("2024-03-30"), start=when("2024-03-25"), transport=transport, clock=lambda: when("2024-04-01T00:00:00"))
    assert result.ok and result.source == "reddit" and [p["title"] for p in result.value] == ["ACME thesis"]
    assert result.value[0]["text"] == "Long & short case" and result.value[0]["link"] == "https://example.test/r1"
    assert transport.calls[0][0].endswith("/r/wallstreetbets+stocks+investing/search.rss") and transport.calls[0][1]["q"] == "ACME"


def test_reddit_unavailable_is_not_the_same_as_no_posts_TA_73() -> None:
    down = social.reddit_posts("ACME", as_of=NOW, transport=RecordedTransport(constant(429)), clock=clock)
    assert (down.status, down.reason, down.source) == (sentinels.DATA_UNAVAILABLE, "rate_limited", "reddit") and "reddit unavailable" in down.warnings[0]
    week_old = social.reddit_posts("ACME", as_of=when("2024-03-25"), start=when("2024-03-20"), clock=lambda: when("2024-04-03T12:00:00"),
                                   transport=RecordedTransport(constant(200, fixture_bytes("reddit_search.atom.xml"))))
    assert week_old.status == sentinels.DATA_UNAVAILABLE and week_old.reason == sentinels.REASON_NOT_OBSERVED   # the feed covers one week: this window is past its reach
    silent = social.reddit_posts("ACME", as_of=when("2024-04-10"), start=when("2024-04-08"), clock=clock, transport=RecordedTransport(constant(200, b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>')))
    assert silent.status == sentinels.NO_DATA                                                          # the window was observed and is empty: a real absence


def test_feeds_that_declare_entities_are_refused_and_html_is_stripped_TA_73() -> None:
    refused = social.reddit_posts("ACME", as_of=NOW, transport=RecordedTransport(constant(200, fixture_bytes("reddit_entity.atom.xml"))), clock=clock)
    assert refused.status == sentinels.DATA_UNAVAILABLE and refused.reason == "bad_response"
    with pytest.raises(Exception):
        parse_atom(fixture_bytes("reddit_entity.atom.xml"))
    assert strip_html("<!-- SC_OFF --><div>a &amp; <b>b</b></div><!-- SC_ON --><p>footer</p>") == "a & b"
    assert strip_html("") == "" and len(parse_atom(fixture_bytes("reddit_search.atom.xml"))) == 2
