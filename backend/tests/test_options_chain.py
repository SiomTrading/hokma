"""The option chain core (SPEC-014): contracts and snapshots of Alpaca joined, filtered and labeled. No network: the fake
Alpaca of options_support.py serves the files of options_fixtures/."""
import json
import math

import pytest
from options_support import DATA, DEMO_KEY, LIVE, PAPER, REAL_KEY, alpaca, install, load  # noqa: F401  (fixture)

from hokma.market import options
from hokma.market.options import OptionsError

ALL = {c["symbol"]: c for c in load("contracts_page1.json")["option_contracts"] + load("contracts_page2.json")["option_contracts"]}
SNAPS = load("snapshots_indicative_open.json")["snapshots"]


def chain(**kwargs):
    return options.options_chain("AAPL", **kwargs)


# ------------------------------------------------------------------------------------------ the join and the labels

def test_the_chain_joins_contracts_and_snapshots_with_every_field(alpaca):
    out = chain(type="call", expiration="2026-12-18", limit=200)
    assert (out["underlying"], out["underlying_price"], out["source"]) == ("AAPL", 228.4, "Alpaca")
    assert out["scanned"] == out["matched"] == 17 and len(out["contracts"]) == 17
    first = out["contracts"][0]
    assert list(first) == ["symbol", "type", "strike", "expiration", "days", "bid", "ask", "mid", "spread_pct", "last",
                           "volume", "open_interest", "open_interest_date", "delta", "gamma", "theta", "vega", "iv",
                           "quote_time"]
    snap, contract = SNAPS["AAPL261218C00190000"], ALL["AAPL261218C00190000"]
    assert first["symbol"] == "AAPL261218C00190000" and first["type"] == "call" and first["strike"] == 190.0
    assert first["expiration"] == "2026-12-18" and first["days"] == 28
    assert first["bid"] == snap["latestQuote"]["bp"] and first["ask"] == snap["latestQuote"]["ap"]
    assert first["last"] == snap["latestTrade"]["p"] and first["volume"] == snap["dailyBar"]["v"]
    assert first["open_interest"] == int(contract["open_interest"]) and first["open_interest_date"] == "2026-11-19"
    assert (first["delta"], first["gamma"], first["theta"], first["vega"], first["iv"]) == (
        snap["greeks"]["delta"], snap["greeks"]["gamma"], snap["greeks"]["theta"], snap["greeks"]["vega"],
        snap["impliedVolatility"])
    assert first["quote_time"] == snap["latestQuote"]["t"]
    keys = [(c["expiration"], c["strike"]) for c in out["contracts"]]
    assert keys == sorted(keys)


def test_the_free_plan_is_labeled_indicative_and_estimated(alpaca):
    out = chain(limit=1)
    assert (out["price_feed"], out["greeks"]) == ("indicative", "estimated")
    assert out["note"] == "Indicative prices and estimated greeks (free Alpaca plan)."
    assert out["market_open"] is True and out["notes"] == []


def test_the_spanish_note_follows_the_language(alpaca, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "es")
    assert chain(limit=1)["note"] == "Precios indicativos y griegas estimadas (plan gratis de Alpaca)."


def test_a_snapshot_without_contract_is_dropped_and_a_contract_without_snapshot_has_no_price(alpaca):
    ghost = dict(SNAPS["AAPL261218C00190000"])
    alpaca.indicative = {**alpaca.indicative, "AAPL261218C99999000": ghost}
    alpaca.by_symbol["AAPL261218C99999000"] = {**ALL["AAPL261218C00190000"], "symbol": "AAPL261218C99999000"}
    del alpaca.indicative["AAPL261218C00195000"]
    out = chain(type="call", expiration="2026-12-18", limit=200)
    symbols = [c["symbol"] for c in out["contracts"]]
    assert "AAPL261218C99999000" not in symbols and "AAPL261218C00195000" not in symbols and out["scanned"] == 16


# ------------------------------------------------------------------------------------------ the filters

PEAD = {"type": "call", "min_days": 60, "max_days": 90, "min_delta": 0.5, "max_delta": 0.7, "min_open_interest": 500,
        "max_spread_pct": 5}


def test_the_pead_case_returns_exactly_the_contracts_that_pass_the_five_filters(alpaca):
    out = chain(**PEAD)
    # Between 60 and 90 days only 2027-01-29 (70 days) exists in the fixture; its calls of 0.50 to 0.70 delta are the
    # 220 and 225 strikes (0.6527 and 0.5758), both with open interest above 500 and a spread under 5 percent.
    assert [c["symbol"] for c in out["contracts"]] == ["AAPL270129C00220000", "AAPL270129C00225000"]
    assert out["scanned"] == 17 and out["matched"] == 2
    assert all(c["days"] == 70 and 0.5 <= c["delta"] <= 0.7 and c["open_interest"] >= 500 and c["spread_pct"] <= 5
               for c in out["contracts"])


@pytest.mark.parametrize("change", [{"min_open_interest": 5700}, {"max_spread_pct": 3.0}, {"min_delta": 0.6},
                                    {"max_delta": 0.6}, {"min_days": 71}, {"max_days": 69}])
def test_each_filter_cuts_the_pead_list(alpaca, change):
    base = [c["symbol"] for c in chain(**PEAD)["contracts"]]
    cut = [c["symbol"] for c in chain(**{**PEAD, **change})["contracts"]]
    assert len(cut) < len(base) and set(cut) <= set(base)


def test_delta_is_judged_in_absolute_value_so_puts_work_the_same(alpaca):
    out = chain(type="put", expiration="2026-12-18", min_delta=0.2, max_delta=0.3, limit=200)
    assert out["contracts"] and all(c["type"] == "put" and c["delta"] < 0 and 0.2 <= abs(c["delta"]) <= 0.3
                                    for c in out["contracts"])
    wanted = [s for s, snap in SNAPS.items() if ALL[s]["type"] == "put" and ALL[s]["expiration_date"] == "2026-12-18"
              and 0.2 <= abs(snap["greeks"]["delta"]) <= 0.3]
    assert sorted(c["symbol"] for c in out["contracts"]) == sorted(wanted)


def test_spread_is_a_percent_of_the_mid_and_null_when_a_side_is_zero(alpaca):
    out = chain(expiration="2026-11-20", limit=200)
    rows = {c["symbol"]: c for c in out["contracts"]}
    zero = [c for c in rows.values() if c["bid"] == 0]
    assert zero and all(c["spread_pct"] is None and c["mid"] is None for c in zero)
    ok = rows["AAPL261120C00230000"]
    assert ok["mid"] == round((ok["bid"] + ok["ask"]) / 2, 4)
    assert ok["spread_pct"] == round((ok["ask"] - ok["bid"]) / ((ok["bid"] + ok["ask"]) / 2) * 100, 2)
    assert chain(expiration="2026-11-20", max_spread_pct=100, limit=200)["matched"] < out["matched"]      # null never passes


def test_limit_cuts_the_list_but_matched_keeps_the_count(alpaca):
    out = chain(expiration="2026-12-18", limit=3)
    assert len(out["contracts"]) == 3 and out["matched"] == 34


def test_an_expiration_ignores_the_days(alpaca):
    out = chain(expiration="2027-03-19", min_days=0, max_days=10, limit=200)
    assert {c["expiration"] for c in out["contracts"]} == {"2027-03-19"} and out["scanned"] == 34
    assert alpaca.queries(PAPER)[0]["expiration_date"] == "2027-03-19"
    assert "expiration_date_gte" not in alpaca.queries(PAPER)[0]


def test_the_default_window_is_zero_to_sixty_days_and_the_dates_go_in_every_request(alpaca):
    out = chain(limit=200)
    assert {c["expiration"] for c in out["contracts"]} == {"2026-11-20", "2026-12-18", "2027-01-15"}      # 56 days, not 70
    for host in (PAPER, DATA):
        for query in alpaca.queries(host):
            assert query["expiration_date_gte"] == "2026-11-20" and query["expiration_date_lte"] == "2027-01-19"


# ------------------------------------------------------------------------------------------ pages and feeds

def test_pagination_follows_the_token_and_stops_at_five_pages(alpaca):
    alpaca.page_size = 10
    out = chain(limit=200, max_days=1100)
    assert len(alpaca.paths(PAPER)) == 5 and [q["feed"] for q in alpaca.queries(DATA)] == ["opra"] + ["indicative"] * 5
    assert out["scanned"] == 50 and out["notes"] and "narrow" in out["notes"][0]
    tokens = [q.get("page_token") for q in alpaca.queries(PAPER)]
    assert tokens == [None, "10", "20", "30", "40"]


def test_two_pages_are_read_and_joined(alpaca):
    alpaca.page_size = 100
    assert chain(limit=200, max_days=1100)["scanned"] == 204
    assert len(alpaca.paths(PAPER)) == 3                       # 100, 100 and 4


def test_opra_that_answers_403_falls_back_to_indicative_and_remembers_it_for_an_hour(alpaca):
    out = chain(limit=1)
    feeds = [q["feed"] for q in alpaca.queries(DATA)]
    assert feeds == ["opra", "indicative"] and out["price_feed"] == "indicative"
    options.forget_cache()
    chain(limit=1, max_days=61)
    assert [q["feed"] for q in alpaca.queries(DATA)][2:] == ["indicative"]       # not asked again
    alpaca.now += 3601
    options.forget_cache()
    chain(limit=1, max_days=62)
    assert [q["feed"] for q in alpaca.queries(DATA)][3:] == ["opra", "indicative"]


def test_a_plan_with_opra_says_opra_and_calculated(monkeypatch):
    fake = install(monkeypatch, opra=True)
    out = chain(limit=1)
    assert [q["feed"] for q in fake.queries(DATA)] == ["opra"]
    assert (out["price_feed"], out["greeks"], out["note"]) == ("opra", "calculated", "OPRA prices; greeks calculated by Alpaca.")


# ------------------------------------------------------------------------------------------ the key decides the host

@pytest.mark.parametrize("key,host,other", [(DEMO_KEY, PAPER, LIVE), (REAL_KEY, LIVE, PAPER)])
def test_a_demo_key_reads_contracts_from_paper_and_a_real_key_from_the_real_host_both_with_open_interest(
        monkeypatch, key, host, other):
    fake = install(monkeypatch, key)
    out = chain(**PEAD)
    assert out["contracts"] and all(isinstance(c["open_interest"], int) for c in out["contracts"])
    assert set(fake.hosts()) == {host, DATA} and other not in fake.hosts()
    for query_host in (host,):
        assert set(fake.paths(query_host)) == {"/v2/options/contracts"}
    call = fake.calls[0]
    assert call[1] == {"APCA-API-KEY-ID": key["key_id"], "APCA-API-SECRET-KEY": key["secret"]}


def test_a_key_with_another_prefix_tries_paper_then_the_real_host_and_remembers_which_answered(monkeypatch):
    fake = install(monkeypatch, {"key_id": "XXOTHERKEYID12345", "secret": "otherSECRETvalue123456"})
    fake.status[PAPER] = 401
    out = chain(limit=1)
    assert out["contracts"] and [h for h in fake.hosts() if h != DATA] == [PAPER, LIVE]
    fake.calls.clear()
    options.forget_cache()
    chain(limit=1, max_days=61)
    assert [h for h in fake.hosts() if h != DATA][0] == LIVE and PAPER not in fake.hosts()


def test_a_key_with_another_prefix_that_no_host_accepts_is_refused(monkeypatch):
    fake = install(monkeypatch, {"key_id": "XXOTHERKEYID12345", "secret": "otherSECRETvalue123456"})
    fake.status[PAPER] = fake.status[LIVE] = 401
    with pytest.raises(OptionsError) as error:
        chain()
    assert error.value.code == "auth_failed" and str(error.value) == "Alpaca did not accept the saved key."


# ------------------------------------------------------------------------------------------ arguments

@pytest.mark.parametrize("kwargs,text", [
    ({"min_days": -1}, "min_days must be a whole number from 0 to 1100."),
    ({"max_days": 1101}, "max_days must be a whole number from 0 to 1100."),
    ({"max_days": "30"}, "max_days must be a whole number from 0 to 1100."),
    ({"max_days": True}, "max_days must be a whole number from 0 to 1100."),
    ({"min_days": 50, "max_days": 10}, "min_days must be not greater than max_days."),
    ({"min_delta": 1.5}, "min_delta must be a number from 0 to 1."),
    ({"max_delta": -0.1}, "max_delta must be a number from 0 to 1."),
    ({"min_delta": 0.8, "max_delta": 0.2}, "min_delta must be not greater than max_delta."),
    ({"min_open_interest": -5}, "min_open_interest must be a whole number of 0 or more."),
    ({"min_open_interest": 1.5}, "min_open_interest must be a whole number of 0 or more."),
    ({"max_spread_pct": 0}, "max_spread_pct must be a number greater than 0."),
    ({"max_spread_pct": "5"}, "max_spread_pct must be a number greater than 0."),
    ({"limit": 0}, "limit must be a whole number from 1 to 200."),
    ({"limit": 201}, "limit must be a whole number from 1 to 200."),
    ({"type": "straddle"}, "type must be call or put."),
    ({"expiration": "18-12-2026"}, "expiration must be a date as YYYY-MM-DD."),
    ({"expiration": "2026-13-45"}, "expiration must be a date as YYYY-MM-DD."),
])
def test_every_argument_is_validated_with_its_own_text_before_any_request(alpaca, kwargs, text):
    with pytest.raises(ValueError) as error:
        chain(**kwargs)
    assert str(error.value) == text and alpaca.calls == []


def test_the_symbol_must_be_a_us_stock_or_etf(alpaca):
    for symbol in ("NQ=F", "BTC-USD", "EURUSD=X", "^SPX"):
        with pytest.raises(OptionsError) as error:
            options.options_chain(symbol)
        assert error.value.code == "not_equity" and error.value.status == 422
        assert str(error.value) == f"Options are available only for US stocks and ETFs, not for {symbol}."
    for symbol in ("bad symbol!!", "", None, 5):
        with pytest.raises(ValueError, match="symbol must be a US stock or ETF symbol"):
            options.options_chain(symbol)
    assert alpaca.calls == []
    assert options.options_chain("aapl", limit=1)["underlying"] == "AAPL"


# ------------------------------------------------------------------------------------------ connection and failures

def test_without_a_saved_key_the_error_says_what_is_missing_and_where_to_connect(monkeypatch):
    fake = install(monkeypatch, None)
    with pytest.raises(OptionsError) as error:
        chain()
    assert error.value.code == "no_connection" and error.value.status == 409
    assert str(error.value) == "The option chain needs Schwab or Alpaca IEX: connect one of them in Connections."
    assert fake.calls == []


def test_with_alpaca_switched_off_nothing_is_asked(monkeypatch):
    fake = install(monkeypatch, off=("alpaca",))
    with pytest.raises(OptionsError, match="needs Schwab or Alpaca IEX"):
        chain()
    assert fake.calls == []


def test_a_desk_that_does_not_use_alpaca_gets_no_data(monkeypatch):
    fake = install(monkeypatch, allowed={"finnhub"})
    with pytest.raises(OptionsError) as error:
        chain(desk_id=7)
    assert str(error.value) == "This desk does not use Alpaca IEX. Turn it on in the desk's connections."
    assert fake.calls == []
    fake = install(monkeypatch, allowed={"alpaca", "finnhub"})
    assert chain(desk_id=7, limit=1)["contracts"]


@pytest.mark.parametrize("status,code,http", [(401, "auth_failed", 401), (403, "auth_failed", 401),
                                              (429, "rate_limited", 429), (500, "upstream", 502)])
def test_what_alpaca_answers_becomes_a_code_a_status_and_a_text(alpaca, status, code, http):
    alpaca.status[PAPER] = status
    with pytest.raises(OptionsError) as error:
        chain()
    assert (error.value.code, error.value.status) == (code, http)


def test_a_network_failure_is_an_upstream_error(alpaca):
    alpaca.fail = True
    with pytest.raises(OptionsError) as error:
        chain()
    assert error.value.code == "upstream" and str(error.value) == "Alpaca did not answer the option chain."


def test_a_damaged_saved_key_is_an_auth_error_without_asking_alpaca(monkeypatch):
    fake = install(monkeypatch, None)
    fake.connections.keys["alpaca"] = "not json"
    with pytest.raises(OptionsError) as error:
        chain()
    assert error.value.code == "auth_failed" and fake.calls == []


def test_the_limiter_stops_at_sixty_calls_a_minute_and_frees_after_it(alpaca):
    for days in range(1, 30):                                     # two calls each, all different so the cache does not help
        chain(limit=1, max_days=days)
    assert len(alpaca.calls) == 59                                 # the first answer of OPRA (403) counts as a call
    with pytest.raises(OptionsError) as error:                      # the 60th call goes, the 61st is refused
        chain(limit=1, max_days=30)
    assert error.value.code == "rate_limited" and error.value.status == 429
    alpaca.now += 61
    assert chain(limit=1, max_days=41)["contracts"]


def test_the_cache_answers_a_repeat_for_thirty_seconds(alpaca):
    chain(limit=1)
    count = len(alpaca.calls)
    chain(limit=5)
    assert len(alpaca.calls) == count
    alpaca.now += 31
    chain(limit=1)
    assert len(alpaca.calls) > count


def test_the_underlying_price_is_null_when_the_chart_has_none_and_the_chain_still_comes(monkeypatch):
    install(monkeypatch, quote=None)
    out = chain(limit=1)
    assert out["underlying_price"] is None and out["contracts"]


# ------------------------------------------------------------------------------------------ Schwab as a source (SPEC-015 5.6)
# The chain of Schwab itself is read in test_schwab_market.py; here, what the order of the use "options" changes.

def test_the_default_order_puts_schwab_first_but_without_a_session_alpaca_answers(alpaca):
    assert alpaca.connections.order("options") == ["schwab", "alpaca"]
    alpaca.connections.keys["schwab"] = "{}"
    alpaca.connections.signed = False                                  # Client ID and Secret saved, no sign-in
    out = chain(limit=1)
    assert out["source"] == "Alpaca" and out["greeks"] == "estimated" and out["price_feed"] == "indicative"


def test_a_desk_that_does_not_use_schwab_is_served_by_alpaca(monkeypatch):
    fake = install(monkeypatch, allowed={"alpaca"})
    fake.connections.keys["schwab"] = "{}"
    out = chain(limit=1, desk_id=7)
    assert out["source"] == "Alpaca" and fake.calls


def test_alpaca_still_says_estimated_greeks_and_has_no_delayed_flag(alpaca):
    out = chain(limit=1)
    assert out["greeks"] == "estimated" and out["note"] and "delayed" not in out


def test_the_order_of_the_use_options_can_put_alpaca_first_and_schwab_is_then_the_second_source(monkeypatch):
    fake = install(monkeypatch)
    fake.connections.keys["schwab"] = "{}"
    fake.connections.options = ("alpaca", "schwab")
    assert chain(limit=1)["source"] == "Alpaca"


# ------------------------------------------------------------------------------------------ Hokma's own numbers

def test_probability_in_the_money_uses_d2_with_the_contract_iv_and_no_rates():
    assert options.prob_itm("call", 100, 100, 0.2, 1.0) == pytest.approx(0.4602, abs=1e-4)
    assert options.prob_itm("put", 100, 100, 0.2, 1.0) == pytest.approx(0.5398, abs=1e-4)
    assert options.prob_itm("call", 110, 100, 0.3, 0.25) > options.prob_itm("call", 110, 105, 0.3, 0.25) > 0.5
    assert options.prob_itm("call", 100, 100, None, 1.0) is None and options.prob_itm("call", None, 100, 0.2, 1.0) is None


def test_at_or_after_the_close_of_the_expiration_the_probability_is_one_or_zero():
    assert options.prob_itm("call", 110, 100, 0.2, 0) == 1.0 and options.prob_itm("call", 90, 100, 0.2, -0.001) == 0.0
    assert options.prob_itm("put", 90, 100, None, 0) == 1.0 and options.prob_itm("put", 110, 100, None, 0) == 0.0


def test_intrinsic_value_and_the_shaded_side():
    assert options.intrinsic_value("call", 228.4, 220) == 8.4 and options.intrinsic_value("call", 228.4, 230) == 0.0
    assert options.intrinsic_value("put", 228.4, 230) == 1.6 and options.intrinsic_value("put", 228.4, 220) == 0.0
    assert options.intrinsic_value("call", None, 220) is None
    assert options.in_the_money("call", 228.4, 220) and not options.in_the_money("call", 228.4, 230)
    assert options.in_the_money("put", 228.4, 230) and not options.in_the_money("put", 228.4, 220)
    assert not options.in_the_money("call", None, 220) and not options.in_the_money("put", None, 220)


def test_the_expected_move_matches_the_screen_of_the_platform_within_three_percent(monkeypatch):
    # The capture: price 32.52, IV 59.69 %, 29 days, shown as +-4.378.
    years = 29 * 1440 / 525600
    assert options.expected_move(32.52, 0.5969, years) == pytest.approx(4.378, rel=0.03)
    assert options.expected_move(None, 0.5969, years) is None and options.expected_move(32.52, None, years) is None
    assert options.expected_move(32.52, 0.5969, 0) is None
    assert 29 * 1440 / 525600 == pytest.approx(29 / 365)
    assert 4.378 == pytest.approx(32.52 * 0.5969 * math.sqrt(29 / 365) * math.sqrt(2 / math.pi), rel=0.03)


def test_years_to_expiry_counts_minutes_to_the_close_of_new_york(monkeypatch):
    install(monkeypatch)
    from datetime import date
    # 2026-11-20 16:00:05 UTC is 11:00 in New York; the close of that day is 16:00 New York = 21:00 UTC.
    assert options.years_to_expiry(date(2026, 11, 20)) == pytest.approx((5 * 3600 - 5) / 60 / 525600)
    assert options.years_to_expiry(date(2026, 11, 19)) < 0


def test_market_open_follows_the_regular_session_of_new_york():
    from datetime import datetime, timezone
    at = lambda *a: datetime(*a, tzinfo=timezone.utc).timestamp()
    assert options.market_open(at(2026, 11, 20, 14, 30)) and options.market_open(at(2026, 11, 20, 20, 59))
    assert not options.market_open(at(2026, 11, 20, 14, 29)) and not options.market_open(at(2026, 11, 20, 21, 0))
    assert not options.market_open(at(2026, 11, 21, 16, 0)) and not options.market_open(at(2026, 11, 22, 16, 0))
