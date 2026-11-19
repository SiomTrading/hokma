"""Read-only company data tools (task 3289, piece B): recorded Finnhub and Alpaca answers, no network.

The fake `fetch` serves the files of `company_fixtures/` by URL path and keeps every request, so the tests can check
that the saved key goes in a header, never in a URL, and never comes back in an answer or an error."""
import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.i18n import t
from hokma.kit import company_tools, hokma_tools
from hokma.kit.hokma_tools import Caller
from hokma.market.errors import NETWORK, MarketError

FIXTURES = Path(__file__).parent / "company_fixtures"
NOW = datetime(2024, 11, 8, 12, 0, tzinfo=timezone.utc).timestamp()
FINNHUB_KEY = "c1nnhubSECRETkey0987654321zz"
ALPACA_KEYS = {"key_id": "AKALPACAIDXYZ12345", "secret": "alpacaSECRETvalue98765abc"}
TOOLS = ("company_profile", "company_news", "earnings_calendar", "earnings_surprises", "basic_financials")


def recorded(name: str):
    return (FIXTURES / name).read_text(encoding="utf-8")


class Connections:
    """The `ConnectionService` as the company tools see it."""

    loaded = True

    def __init__(self, keys: dict[str, str] | None = None, off: tuple[str, ...] = ()) -> None:
        self.keys, self.off = keys or {}, off

    def is_enabled(self, provider_id: str) -> bool:
        return provider_id not in self.off

    def has_key(self, provider_id: str) -> bool:
        return provider_id in self.keys

    def read_secret(self, provider_id: str) -> str | None:
        return self.keys.get(provider_id)


class Net:
    """Serves the recorded answers; `status` forces a status for a provider host."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.status: dict[str, int] = {}
        self.bodies: dict[str, str] = {}
        self.fail = False

    def __call__(self, url: str, headers: dict) -> tuple[int, str]:
        self.calls.append((url, dict(headers)))
        if self.fail:
            raise MarketError(NETWORK, 502)
        parts = urllib.parse.urlsplit(url)
        host, path = parts.netloc, parts.path
        if path == "/ff_calendar_thisweek.json" and path not in self.bodies:
            return 503, ""                  # Forex Factory is off unless a test serves it (the Finnhub tests stay as they were)
        forced = self.status.get(host)
        if forced:
            return forced, recorded("finnhub_error_403.json")
        if path in self.bodies:
            return 200, self.bodies[path]
        files = {"/api/v1/stock/profile2": "finnhub_profile2_AAPL.json",
                 "/api/v1/company-news": "finnhub_company_news_AAPL.json",
                 "/api/v1/calendar/earnings": "finnhub_calendar_earnings_AAPL.json",
                 "/api/v1/stock/earnings": "finnhub_stock_earnings_AAPL.json",
                 "/api/v1/stock/metric": "finnhub_stock_metric_AAPL.json",
                 "/v1beta1/news": "alpaca_news_AAPL.json"}
        return 200, recorded(files[path])


@pytest.fixture
def net(monkeypatch):
    fake = Net()
    clock = {"now": NOW}
    monkeypatch.setattr(company_tools, "_fetch", fake)
    monkeypatch.setattr(company_tools, "_clock", lambda: clock["now"])
    monkeypatch.setattr(company_tools, "_connections", lambda: fake.connections)
    monkeypatch.setattr(company_tools, "_allowed", lambda desk_id: fake.allowed)
    fake.connections = Connections({"finnhub": FINNHUB_KEY})
    fake.allowed = None
    fake.clock = clock
    from datetime import timezone
    from hokma.kit import today_data
    monkeypatch.setattr(today_data, "_owner_zone", lambda: timezone.utc)      # never the zone of the real profile on this machine
    company_tools.reset()
    yield fake
    company_tools.reset()


def calls(**kwargs):
    return {
        "company_profile": lambda: company_tools.company_profile("aapl", **kwargs),
        "company_news": lambda: company_tools.company_news("AAPL", **kwargs),
        "earnings_calendar": lambda: company_tools.earnings_calendar(symbol="AAPL", **kwargs),
        "earnings_surprises": lambda: company_tools.earnings_surprises("AAPL", **kwargs),
        "basic_financials": lambda: company_tools.basic_financials("AAPL", **kwargs),
    }


# ------------------------------------------------------------------------------------------ what each tool returns

def test_company_profile(net):
    out = company_tools.company_profile("aapl")
    assert out["source"] == "Finnhub" and out["fetched_at"] == "2024-11-08T12:00:00Z"
    assert out["symbol"] == "AAPL" and out["name"] == "Apple Inc" and out["industry"] == "Technology"
    assert out["market_cap_millions"] == 3520000.5 and out["country"] == "US" and "logo" not in out


def test_company_news_newest_first_with_limit_and_range(net):
    out = company_tools.company_news("AAPL", "2024-11-01", "2024-11-08", 2)
    assert out["source"] == "Finnhub" and out["count"] == 2
    assert [n["headline"] for n in out["news"]] == ["Analysts raise Apple targets after earnings",
                                                     "Apple reports fiscal fourth quarter results"]
    assert out["news"][0]["time"] == "2024-11-06T13:33:20Z" and out["news"][0]["url"].startswith("https://")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(net.calls[-1][0]).query)
    assert query["from"] == ["2024-11-01"] and query["to"] == ["2024-11-08"] and query["symbol"] == ["AAPL"]


def test_company_news_defaults_to_the_last_seven_days(net):
    company_tools.company_news("AAPL")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(net.calls[-1][0]).query)
    assert query["from"] == ["2024-11-01"] and query["to"] == ["2024-11-08"]


def test_earnings_calendar_sorted_with_estimates_and_actuals(net):
    out = company_tools.earnings_calendar("2024-10-31", "2025-01-30", "AAPL")
    assert out["source"] == "Finnhub" and out["count"] == 2 and out["truncated"] is False
    first, second = out["earnings"]
    assert first["date"] == "2024-10-31" and first["eps_actual"] == 1.64 and first["eps_estimate"] == 1.6
    assert second["date"] == "2025-01-30" and second["eps_actual"] is None and second["hour"] == "amc"
    assert second["revenue_estimate"] == 124100000000


def test_earnings_calendar_without_symbol_defaults_to_the_next_week(net):
    company_tools.earnings_calendar()
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(net.calls[-1][0]).query)
    assert query["from"] == ["2024-11-08"] and query["to"] == ["2024-11-15"] and "symbol" not in query


def test_earnings_calendar_is_capped(net):
    rows = [{"date": "2024-11-12", "symbol": f"S{i}", "epsEstimate": 1.0} for i in range(250)]
    net.bodies["/api/v1/calendar/earnings"] = json.dumps({"earningsCalendar": rows})
    out = company_tools.earnings_calendar()
    assert out["count"] == 100 and len(out["earnings"]) == 100 and out["truncated"] is True


def test_earnings_surprises_estimate_actual_and_surprise(net):
    out = company_tools.earnings_surprises("AAPL", 2)
    assert out["source"] == "Finnhub" and out["count"] == 2
    latest = out["surprises"][0]
    assert latest == {"period": "2024-09-30", "year": 2024, "quarter": 4, "eps_estimate": 1.6, "eps_actual": 1.64,
                      "surprise": 0.04, "surprise_pct": 2.5}
    assert out["surprises"][1]["period"] == "2024-06-30"
    assert "limit=2" in net.calls[-1][0]


def test_basic_financials_keeps_the_key_metrics_only(net):
    out = company_tools.basic_financials("AAPL")
    assert out["source"] == "Finnhub"
    assert out["metrics"]["peTTM"] == 36.9 and out["metrics"]["52WeekHigh"] == 237.49 and out["metrics"]["beta"] == 1.24
    assert "someOtherMetric" not in out["metrics"] and "series" not in out


def test_long_texts_are_cut(net):
    rows = [{"datetime": 1730800000, "headline": "H" * 5000, "summary": "S" * 5000, "source": "x", "url": "u"}]
    net.bodies["/api/v1/company-news"] = json.dumps(rows)
    out = company_tools.company_news("AAPL")
    assert len(out["news"][0]["headline"]) <= company_tools.MAX_TEXT and len(out["news"][0]["summary"]) <= company_tools.MAX_TEXT


# ------------------------------------------------------------------------------------------ the key never shows

def test_the_key_goes_in_a_header_and_never_in_an_url_or_an_answer(net):
    for name, call in calls().items():
        text = json.dumps(call())
        assert FINNHUB_KEY not in text, name
    assert net.calls and all(FINNHUB_KEY not in url for url, _ in net.calls)
    assert all(headers == {"X-Finnhub-Token": FINNHUB_KEY} for _, headers in net.calls)


def test_a_key_that_a_provider_echoes_back_is_scrubbed(net):
    net.bodies["/api/v1/stock/profile2"] = json.dumps({"name": f"Apple {FINNHUB_KEY}", "ticker": "AAPL"})
    text = json.dumps(company_tools.company_profile("AAPL"))
    assert FINNHUB_KEY not in text and "[key]" in text


def test_the_key_is_not_in_any_error(net):
    net.status["finnhub.io"] = 403
    messages = []
    for name, call in calls().items():
        with pytest.raises(LookupError) as raised:
            call()
        messages.append(str(raised.value))
    net.status["finnhub.io"] = 401
    company_tools.reset()
    with pytest.raises(LookupError) as raised:
        company_tools.company_profile("AAPL")
    messages.append(str(raised.value))
    net.status.clear()
    net.fail = True
    company_tools.reset()
    with pytest.raises(LookupError) as raised:
        company_tools.company_profile("AAPL")
    messages.append(str(raised.value))
    assert messages and all(FINNHUB_KEY not in m and "token" not in m.lower() for m in messages)


# ------------------------------------------------------------------------------------------ plan, connection, limits

def test_a_403_of_the_plan_says_so_and_is_not_asked_again(net):
    net.status["finnhub.io"] = 403
    with pytest.raises(LookupError) as raised:
        company_tools.earnings_surprises("AAPL")
    message = str(raised.value)
    assert message == t("company.err.denied", provider="Finnhub", what=t("company.what.surprises"))
    assert "403" in message and "Finnhub" in message and "plan" in message
    asked = len(net.calls)
    with pytest.raises(LookupError):
        company_tools.earnings_surprises("MSFT")
    assert len(net.calls) == asked                                   # the refusal is remembered, no new request
    net.status.clear()
    assert company_tools.company_profile("AAPL")["source"] == "Finnhub"      # the refusal is only for that data


def test_a_rejected_key_asks_to_save_it_again(net):
    net.status["finnhub.io"] = 401
    with pytest.raises(LookupError) as raised:
        company_tools.company_profile("AAPL")
    assert str(raised.value) == t("company.err.bad_key", provider="Finnhub")


@pytest.mark.parametrize("name", TOOLS)
def test_without_finnhub_the_error_says_which_connection_is_missing(net, name):
    net.connections = Connections()
    with pytest.raises(LookupError) as raised:
        calls()[name]()
    expected = t("company.err.missing_news") if name == "company_news" else t("company.err.missing")
    assert str(raised.value) == expected
    assert "Finnhub" in expected and ("Conexiones" in expected or "Connections" in expected)
    assert not net.calls


def test_a_switched_off_finnhub_counts_as_missing(net):
    net.connections = Connections({"finnhub": FINNHUB_KEY}, off=("finnhub",))
    with pytest.raises(LookupError) as raised:
        company_tools.company_profile("AAPL")
    assert str(raised.value) == t("company.err.missing")


def test_a_desk_that_does_not_use_finnhub_is_told_to_turn_it_on(net):
    net.allowed = {"coinbase"}
    with pytest.raises(LookupError) as raised:
        company_tools.company_profile("AAPL")
    assert str(raised.value) == t("company.err.not_on_desk", provider="Finnhub")
    assert not net.calls


def test_calls_per_minute_are_limited_and_come_back_after_the_minute(net):
    limit = company_tools.CALLS_PER_MINUTE["finnhub"]
    assert limit < 60                                               # under the free plan of Finnhub
    for i in range(limit):
        company_tools.company_profile(f"S{i}")
    assert len(net.calls) == limit
    with pytest.raises(LookupError) as raised:
        company_tools.company_profile("ZZZ")
    assert str(raised.value) == t("company.err.rate", provider="Finnhub", limit=limit)
    assert len(net.calls) == limit                                  # the call was not made
    net.clock["now"] = NOW + 61
    assert company_tools.company_profile("ZZZ")["source"] == "Finnhub"


def test_a_429_from_the_provider_is_the_same_clear_message(net):
    net.status["finnhub.io"] = 429
    with pytest.raises(LookupError) as raised:
        company_tools.company_profile("AAPL")
    assert str(raised.value) == t("company.err.rate", provider="Finnhub", limit=company_tools.CALLS_PER_MINUTE["finnhub"])


def test_no_network_and_empty_answers_have_their_own_message(net):
    net.fail = True
    with pytest.raises(LookupError) as raised:
        company_tools.company_profile("AAPL")
    assert str(raised.value) == t("company.err.network", provider="Finnhub")
    net.fail = False
    net.bodies["/api/v1/stock/profile2"] = "{}"
    with pytest.raises(LookupError) as raised:
        company_tools.company_profile("ZZZZ")
    assert str(raised.value) == t("company.err.empty", provider="Finnhub", what=t("company.what.profile"), symbol="ZZZZ")
    net.bodies["/api/v1/stock/profile2"] = "not json"
    with pytest.raises(LookupError):
        company_tools.company_profile("QQQQ")


# ------------------------------------------------------------------------------------------ cache and fallback

def test_a_second_call_comes_from_the_short_cache_with_the_first_date(net):
    first = company_tools.earnings_surprises("AAPL")
    net.clock["now"] = NOW + 60
    again = company_tools.earnings_surprises("AAPL")
    assert again == first and again["fetched_at"] == "2024-11-08T12:00:00Z" and len(net.calls) == 1
    net.clock["now"] = NOW + company_tools.CACHE_DEFAULT + 1
    company_tools.earnings_surprises("AAPL")
    assert len(net.calls) == 2


def test_news_falls_back_to_alpaca_when_there_is_no_finnhub(net):
    net.connections = Connections({"alpaca": json.dumps(ALPACA_KEYS)})
    out = company_tools.company_news("AAPL", limite=5)
    assert out["source"] == "Alpaca" and out["news"][0]["headline"] == "Apple shares move after earnings"
    url, headers = net.calls[-1]
    assert headers == {"APCA-API-KEY-ID": ALPACA_KEYS["key_id"], "APCA-API-SECRET-KEY": ALPACA_KEYS["secret"]}
    assert ALPACA_KEYS["secret"] not in url and ALPACA_KEYS["secret"] not in json.dumps(out)


def test_news_falls_back_to_alpaca_when_the_finnhub_plan_has_no_news(net):
    net.connections = Connections({"finnhub": FINNHUB_KEY, "alpaca": json.dumps(ALPACA_KEYS)})
    net.status["finnhub.io"] = 403
    out = company_tools.company_news("AAPL")
    assert out["source"] == "Alpaca"


def test_only_news_has_a_second_provider(net):
    net.connections = Connections({"alpaca": json.dumps(ALPACA_KEYS)})
    with pytest.raises(LookupError) as raised:
        company_tools.earnings_surprises("AAPL")
    assert str(raised.value) == t("company.err.missing")
    net.status["data.alpaca.markets"] = 403
    net.connections = Connections({"finnhub": FINNHUB_KEY, "alpaca": json.dumps(ALPACA_KEYS)})
    net.status["finnhub.io"] = 403
    with pytest.raises(LookupError) as raised:
        company_tools.company_news("AAPL")
    assert "Finnhub" in str(raised.value) and "Alpaca" in str(raised.value)
    assert ALPACA_KEYS["secret"] not in str(raised.value)


# ------------------------------------------------------------------------------------------ arguments

@pytest.mark.parametrize("symbol", ["", "NQ=F", "BTC-USD", "bad symbol", "^GSPC", None, 5])
def test_only_stock_symbols_are_accepted(net, symbol):
    with pytest.raises(ValueError):
        company_tools.company_profile(symbol)
    assert not net.calls


def test_class_shares_are_accepted(net):
    assert company_tools.company_profile("brk.b")["source"] == "Finnhub"


@pytest.mark.parametrize("desde,hasta", [("2024-13-01", None), ("yesterday", None), ("2024-11-09", "2024-11-01"),
                                         ("2024-01-01", "2024-11-01")])
def test_bad_dates_and_too_long_ranges_are_refused(net, desde, hasta):
    with pytest.raises(ValueError):
        company_tools.company_news("AAPL", desde, hasta)
    with pytest.raises(ValueError):
        company_tools.earnings_calendar(desde, hasta)
    assert not net.calls


@pytest.mark.parametrize("limite", [0, -1, 21, True, "5", 2.5])
def test_the_limit_is_a_whole_number_in_range(net, limite):
    with pytest.raises(ValueError):
        company_tools.company_news("AAPL", limite=limite)
    assert not net.calls


# ------------------------------------------------------------------------------------------ the MCP server

def test_the_tools_are_listed_read_only_and_strict():
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    for name in TOOLS:
        assert hokma_tools.TOOLS[name][4] is False and name in hokma_tools.NO_UNIT
        assert listed[name]["inputSchema"]["additionalProperties"] is False
    assert listed["company_news"]["inputSchema"]["required"] == ["symbol"]
    assert set(listed["company_news"]["inputSchema"]["properties"]) == {"symbol", "desde", "hasta", "limite"}
    assert listed["earnings_calendar"]["inputSchema"]["required"] == []
    assert set(listed["earnings_surprises"]["inputSchema"]["properties"]) == {"symbol", "limite"}


def test_the_default_fetch_goes_over_the_system_certificate_store():
    from hokma.market import history

    assert company_tools._fetch is history.http_get and company_tools.http_get is history.http_get
    assert "outbound_context" in Path(history.__file__).read_text(encoding="utf-8")


def run(name, args):
    text, is_error = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": "iris"}))
    return text, is_error


def test_through_the_server_an_answer_is_json_and_an_error_is_text(db, net):
    text, err = run("earnings_surprises", {"symbol": "AAPL", "limite": 2})
    assert not err and json.loads(text)["surprises"][0]["eps_actual"] == 1.64 and FINNHUB_KEY not in text
    net.connections = Connections()
    text, err = run("earnings_surprises", {"symbol": "AAPL"})
    assert err and t("company.err.missing") in text
    text, err = run("company_news", {"symbol": "AAPL", "limite": 99})
    assert err and "limite" in text
    assert run("company_profile", {"symbol": "AAPL", "extra": 1})[1]
