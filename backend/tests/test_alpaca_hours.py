"""SPEC-016 A3: the calendar and the clock of Alpaca (v3), read in ONE module. Verified by Iris against the official documentation (names of the fields and
paths); the envelope of the answer and the exact form of the times are assumed, so a missing field is "unknown", never a guess."""
from datetime import date, datetime, timezone

import pytest

from hokma.market import alpaca_hours
from conftest import REAL_ALPACA_FETCH_DAY, REAL_ALPACA_PHASE

DAY = date(2026, 11, 20)


def utc(h, m):
    return int(datetime(2026, 11, 20, h, m, tzinfo=timezone.utc).timestamp())


ENTRY = {"date": "2026-11-20", "pre_start": "2026-11-20T09:00:00Z", "pre_end": "2026-11-20T14:30:00Z", "core_start": "2026-11-20T14:30:00Z",
         "core_end": "2026-11-20T21:00:00Z", "post_start": "2026-11-20T21:00:00Z", "post_end": "2026-11-21T01:00:00Z"}


@pytest.mark.parametrize("payload", [[ENTRY], {"calendar": [ENTRY]}])
def test_a_listed_day_gives_pre_core_and_post_with_the_names_of_the_other_provider_in_either_envelope(payload):
    hours = alpaca_hours.parse_calendar(payload, DAY)
    assert [(s.name, s.start, s.end) for s in hours.segments] == [("preMarket", utc(9, 0), utc(14, 30)), ("regularMarket", utc(14, 30), utc(21, 0)),
                                                              ("postMarket", utc(21, 0), utc(21, 0) + 4 * 3600)]
    assert hours.open_at(utc(15, 0)) is True and hours.open_at(utc(21, 0) + 5 * 3600) is False


def test_a_bare_time_is_read_as_utc_on_that_date_and_a_day_not_listed_is_closed():
    entry = {"date": "2026-11-20", "core_start": "14:30", "core_end": "21:00"}
    assert [(s.name, s.start, s.end) for s in alpaca_hours.parse_calendar([entry], DAY).segments] == [("regularMarket", utc(14, 30), utc(21, 0))]
    closed = alpaca_hours.parse_calendar([], DAY)
    assert closed.segments == () and closed.open_at(utc(15, 0)) is False


@pytest.mark.parametrize("payload", [{"message": "nope"}, "text", [{"date": "2026-11-20", "pre_start": "x"}], [{"date": "2026-11-20", "core_start": "14:30"}]])
def test_a_listed_day_without_its_core_times_is_unknown(payload):
    assert alpaca_hours.parse_calendar(payload, DAY) is None


def test_it_asks_the_v3_calendar_of_one_day_in_utc_on_the_trading_host_of_the_key_and_a_failure_is_no_hours():
    seen = []

    def fetch(url, headers):
        seen.append(url)
        return 200, '[{"date": "2026-11-20", "core_start": "2026-11-20T14:30:00Z", "core_end": "2026-11-20T21:00:00Z"}]'

    hours = REAL_ALPACA_FETCH_DAY(fetch, {"APCA-API-KEY-ID": "k"}, DAY, alpaca_hours.trading_hosts("PKDEMO"))
    assert seen == ["https://paper-api.alpaca.markets/v3/calendar/NYSE?start=2026-11-20&end=2026-11-20&timezone=UTC"]
    assert [s.name for s in hours.segments] == ["regularMarket"]
    assert REAL_ALPACA_FETCH_DAY(lambda url, headers: (403, ""), {}, DAY) is None
    assert REAL_ALPACA_FETCH_DAY(lambda url, headers: (200, "not json"), {}, DAY) is None


def test_the_hosts_follow_the_prefix_of_the_key_and_the_real_one_needs_the_lock():
    assert alpaca_hours.trading_hosts("PKDEMO") == ["https://paper-api.alpaca.markets"]
    assert alpaca_hours.trading_hosts("AKREAL") == ["https://api.alpaca.markets"]
    assert len(alpaca_hours.trading_hosts("XXOTHER")) == 2
    urls = []
    assert REAL_ALPACA_FETCH_DAY(lambda url, headers: urls.append(url) or (404, ""), {}, DAY, ["https://api.alpaca.markets"]) is None
    assert urls and urls[0].startswith("https://api.alpaca.markets/v3/calendar/NYSE")        # the lock allows this exact read


def test_the_phase_of_the_clock_is_read_from_the_v3_clock():
    seen = []

    def fetch(url, headers):
        seen.append(url)
        return 200, '{"clocks": [{"market": "NYSE", "phase": "pre", "phase_until": "2026-11-20T14:30:00Z"}]}'

    assert REAL_ALPACA_PHASE(fetch, {}, ["https://paper-api.alpaca.markets"]) == "pre"
    assert seen == ["https://paper-api.alpaca.markets/v3/clock?markets=NYSE"]
    assert REAL_ALPACA_PHASE(lambda url, headers: (200, '{"clocks": []}'), {}, ["https://paper-api.alpaca.markets"]) is None


def test_the_v3_reads_are_the_only_calendar_reads_the_backend_may_make_to_the_trading_host_besides_the_contracts():
    from hokma.kit import hardblock

    assert hardblock.read_exception_allows("api.alpaca.markets", "GET", "/v3/calendar/NYSE", "hokma_backend")
    assert hardblock.read_exception_allows("api.alpaca.markets", "GET", "/v3/clock", "hokma_backend")
    assert not hardblock.read_exception_allows("api.alpaca.markets", "GET", "/v2/clock", "hokma_backend")
    assert not hardblock.read_exception_allows("api.alpaca.markets", "GET", "/v3/calendar/../orders", "hokma_backend")
    assert not hardblock.read_exception_allows("api.alpaca.markets", "POST", "/v3/clock", "hokma_backend")
    assert not hardblock.read_exception_allows("api.alpaca.markets", "GET", "/v3/clock", "an_agent")
