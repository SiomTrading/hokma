"""T-3: the assumptions of the interval table (SPEC chart addendum, 3.3) checked against answers that Yahoo really gave,
recorded once on 2026-11-05 in `market_fixtures/yahoo_caps_t3.json` (a summary of each answer: status, bars, first and last
time, and the error text when it refused). The test reads the file; nothing touches the network.

What the recording says and this test keeps true:
- the range that `yahoo.py` asks for each base is accepted for both AAPL and NQ=F, and the next longer one is refused (1m,
  5m, 15m, 1h), so the table asks the longest range each base allows;
- 1h bars of a stock start at the regular open (09:30 New York, the half hour) and the 1h bars of futures start on the hour,
  which is what the session anchor of `sessions.py` expects;
- `range=730d` is NOT 730 days for 1h (it returned 1063 days for AAPL and 874 for NQ=F): the table uses `2y`.
"""
import json
from datetime import datetime

import pytest

from hokma.market.tz import NEW_YORK
from hokma.market.yahoo import UPSTREAM_PARAMS
from market_support import FIXTURES

CASES = json.loads((FIXTURES / "yahoo_caps_t3.json").read_text(encoding="utf-8"))["cases"]
SYMBOLS = ("AAPL", "NQ=F")
# Yahoo's own interval name for each base of the table, and the first range it refused for that base.
REFUSED = {"1m": ("1m", "1mo"), "5m": ("5m", "3mo"), "15m": ("15m", "3mo"), "1h": ("1h", "3y")}


@pytest.mark.parametrize("symbol", SYMBOLS)
@pytest.mark.parametrize("base", list(UPSTREAM_PARAMS))
def test_the_range_asked_for_each_base_is_accepted_by_yahoo(symbol, base):
    interval, span = UPSTREAM_PARAMS[base]
    answer = CASES[f"{symbol}|{interval}|{span}"]
    assert answer["status"] == 200 and answer["bars"] > 100, answer


@pytest.mark.parametrize("symbol", SYMBOLS)
@pytest.mark.parametrize("base", list(REFUSED))
def test_the_next_longer_range_is_refused_so_the_table_asks_the_longest_one(symbol, base):
    interval, too_long = REFUSED[base]
    answer = CASES[f"{symbol}|{interval}|{too_long}"]
    assert answer["status"] == 422 and "not available" in answer["error"]


def test_one_minute_bars_are_limited_per_request_to_about_8_days():
    for symbol in SYMBOLS:
        assert CASES[f"{symbol}|1m|7d"]["span_days"] <= 9
        assert "Only 8 days" in CASES[f"{symbol}|1m|1mo"]["error"]


def test_five_and_fifteen_minute_bars_start_within_60_days():
    for symbol in SYMBOLS:
        for interval in ("5m", "15m"):
            assert "within the last 60 days" in CASES[f"{symbol}|{interval}|3mo"]["error"]


def test_730d_is_not_730_days_for_hourly_bars_so_the_table_uses_2y():
    assert UPSTREAM_PARAMS["1h"] == ("1h", "2y")
    for symbol in SYMBOLS:
        assert CASES[f"{symbol}|1h|730d"]["span_days"] > 800
        assert CASES[f"{symbol}|1h|2y"]["span_days"] <= 731


def test_hourly_bars_of_a_stock_start_at_half_past_and_those_of_futures_on_the_hour():
    stock = CASES["AAPL|1h|2y"]["last_40"][:-1]                         # the last one is the closing print, off the grid
    future = CASES["NQ=F|1h|2y"]["last_40"][:-1]                        # and the one in progress
    stock_minutes = {datetime.fromtimestamp(t, NEW_YORK).minute for t in stock}
    future_minutes = {datetime.fromtimestamp(t, NEW_YORK).minute for t in future}
    assert stock_minutes == {30} and future_minutes == {0}
    first_of_day = [t for before, t in zip(stock, stock[1:]) if t - before > 3600]              # the bar after a gap
    assert {datetime.fromtimestamp(t, NEW_YORK).strftime("%H:%M") for t in first_of_day} == {"09:30"}
