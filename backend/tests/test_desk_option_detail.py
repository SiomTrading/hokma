"""The Option detail panel data of a desk (SPEC-028, 3339-A): the endpoint, the table by date and price on the real chain, the events before expiration.

The chain, the events and the clock are replaced at their seams, so nothing here reaches a provider."""
import math
from datetime import date
from statistics import NormalDist

import numpy as np
import pytest
from desks_support import db, desks_db, env, make_desk  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.desks import option_detail
from hokma.desks.api import router as desks_router
from hokma.desks.errors import DeskError
from hokma.quant.options import payoff as pay
from hokma.rpc.ws import SESSION_TOKEN

N = NormalDist().cdf
TODAY, EXPIRATION = date(2026, 11, 25), date(2026, 12, 25)
SPOT, YEARS, IV, RATE = 100.0, 30 / 365, 0.30, 0.04


def bs(spot, strike, kind, t=YEARS, iv=IV):
    if t <= 0:
        return max(spot - strike, 0.0) if kind == "call" else max(strike - spot, 0.0)
    d1 = (math.log(spot / strike) + (RATE + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if kind == "call":
        return spot * N(d1) - strike * math.exp(-RATE * t) * N(d2)
    return strike * math.exp(-RATE * t) * N(-d2) - spot * N(-d1)


def rows(spot=SPOT):
    out = []
    for k in range(80, 121, 5):
        c, p = bs(spot, k, "call"), bs(spot, k, "put")
        out.append({"strike": float(k), "call": {"bid": round(c - 0.03, 2), "ask": round(c + 0.03, 2), "iv": IV, "delta": 0.5, "open_interest": 500},
                    "put": {"bid": round(max(p - 0.03, 0), 2), "ask": round(p + 0.03, 2), "iv": IV, "delta": -0.5, "open_interest": 500}})
    return out


def chain_of(spot=SPOT, **over):
    base = {"ok": True, "spot": spot, "rows": rows(spot), "multiplier": 100.0, "years": YEARS, "rate": RATE, "source": "Feed", "price_feed": "live", "greeks": "provider",
            "as_of": "2026-11-25T15:00:00Z", "market_open": True, "delayed": False, "notes": []}
    base.update(over)
    return base


def earnings(day):
    return {"ok": True, "source": "Calendar", "fetched_at": "t", "next": None if day is None else {"symbol": "AAPL", "date": day, "hour": "amc"}}


def dividend(day, last="2026-08-10"):
    return {"ok": True, "source": "Quotes", "fetched_at": "t", "next": day, "last": last, "amount": 0.25, "frequency": 4}


@pytest.fixture
def world(desks_db, monkeypatch):
    desk = make_desk(desks_db)
    state = {"chain": chain_of(), "earnings": earnings("2026-12-10"), "dividend": dividend(None), "chain_calls": []}

    def load_chain(desk_id, symbol, day):
        state["chain_calls"].append((symbol, day))
        return state["chain"]

    monkeypatch.setattr(option_detail, "_load_chain", load_chain)
    monkeypatch.setattr(option_detail, "_today", lambda: TODAY)
    monkeypatch.setattr(option_detail, "_earnings_block", lambda d, s, today: state["earnings"])
    monkeypatch.setattr(option_detail, "_ex_dividend_block", lambda d, s: state["dividend"])
    state["desk"] = desk
    return state


@pytest.fixture
def client(desks_db):
    app = FastAPI()
    app.include_router(desks_router)
    with TestClient(app) as c:
        c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
        yield c


BULL = [{"position": "long", "kind": "call", "strike": 100.0}, {"position": "short", "kind": "call", "strike": 110.0}]


def ask(world, **kw):
    args = dict(symbol="AAPL", expiration="2026-12-25", legs=BULL)
    args.update(kw)
    return option_detail.option_detail(world["desk"]["id"], args["symbol"], args["expiration"], args.get("legs"), args.get("structure"), args.get("strikes"),
                                       args.get("quantity"), args.get("range_pct"), args.get("iv_mult"), args.get("rows"), args.get("columns"))


def mid(strike, kind):
    cell = next(r for r in rows() if r["strike"] == strike)[kind]
    return (cell["bid"] + cell["ask"]) / 2


def test_d1_the_expiration_column_is_pnl_at_and_today_is_black_scholes_per_leg(world):
    out = ask(world)
    assert out["ok"] is True
    table = out["table"]
    prices = np.array([r["price"] for r in table["rows"]])
    legs = pay.parse_legs([{"position": "long", "kind": "call", "strike": 100.0, "premium": mid(100.0, "call"), "quantity": 1},
                           {"position": "short", "kind": "call", "strike": 110.0, "premium": mid(110.0, "call"), "quantity": 1}], 100.0)
    assert [row[-1] for row in table["pnl_usd"]] == pytest.approx(pay.pnl_at(legs, prices).tolist(), abs=1e-9)
    for i, price in enumerate(prices):
        want = ((bs(price, 100.0, "call") - mid(100.0, "call")) - (bs(price, 110.0, "call") - mid(110.0, "call"))) * 100
        assert table["pnl_usd"][i][0] == pytest.approx(want, abs=1e-8)
    assert table["columns"][0]["date"] == "2026-11-25" and table["columns"][-1]["date"] == "2026-12-25" and table["columns"][-1]["expiration"] is True
    assert out["legs"][0]["premium_source"] == "chain_mid" and out["legs"][0]["iv_source"] == "chain" and out["model"]["rate"] == RATE and out["model"]["dividend_yield"] == 0.0


def test_d2_volatility_multiple_changes_the_table_and_not_the_expiration_column(world):
    a, b = ask(world), ask(world, iv_mult=1.5)
    assert a["table"]["pnl_usd"] != b["table"]["pnl_usd"]
    assert [r[-1] for r in a["table"]["pnl_usd"]] == [r[-1] for r in b["table"]["pnl_usd"]]
    assert b["legs"][0]["iv_used"] == pytest.approx(IV * 1.5) and b["model"]["iv_mult"] == 1.5


def test_d3_range_of_three_percent_is_thirteen_rows_with_the_current_price_marked(world):
    out = ask(world, range_pct=3)
    rows_ = out["table"]["rows"]
    assert len(rows_) == 13 and rows_[0]["pct"] == pytest.approx(-3.0) and rows_[-1]["pct"] == pytest.approx(3.0)
    assert [r["current"] for r in rows_].count(True) == 1 and rows_[out["table"]["spot_row"]]["price"] == SPOT


def test_d4_when_the_underlying_moves_the_highlighted_row_and_the_figures_follow_and_the_rule_of_recalculation_travels_with_the_answer(world):
    a = ask(world)
    world["chain"] = chain_of(spot=101.0)
    b = ask(world)
    assert b["table"]["rows"][b["table"]["spot_row"]]["price"] == 101.0 and a["table"]["rows"][a["table"]["spot_row"]]["price"] == 100.0
    assert a["metrics"]["p_profit"] != b["metrics"]["p_profit"]
    for out in (a, b):
        assert out["recalc"] == {"price_move_pct": 0.1, "debounce_ms": 150, "by_event_only": True, "anchor_spot": out["spot"]}


def test_there_is_no_clock_in_the_module(world):
    import inspect

    text = inspect.getsource(option_detail)
    for word in ("sleep(", "Timer(", "schedule", "setInterval", "while True"):
        assert word not in text


def test_d5_an_iron_condor_has_two_breakevens_and_a_butterfly_is_built_from_the_catalog(world):
    out = ask(world, legs=None, structure="iron_condor", strikes=[85.0, 95.0, 105.0, 115.0], range_pct=20)
    assert out["ok"] and len(out["breakevens"]) == 2 and out["metrics"]["premium_type"] == "credit"
    credit = -out["metrics"]["net_premium_usd"] / 100
    assert out["breakevens"] == pytest.approx([95.0 - credit, 105.0 + credit])
    fly = ask(world, legs=None, structure="iron_butterfly", strikes=[90.0, 100.0, 110.0])
    assert fly["ok"] and len(fly["legs"]) == 4 and len(fly["breakevens"]) == 2


def test_the_protective_put_has_its_shares_and_its_floor(world):
    out = ask(world, legs=None, structure="protective_put", strikes=[95.0], range_pct=20)
    assert out["ok"] and out["legs"][0]["instrument"] == "stock" and out["legs"][0]["quantity"] == 100.0 and out["legs"][0]["premium"] == SPOT
    assert out["metrics"]["max_loss_usd"] == pytest.approx((SPOT - 95.0 + mid(95.0, "put")) * 100)
    assert min(r[-1] for r in out["table"]["pnl_usd"]) == pytest.approx(-out["metrics"]["max_loss_usd"])


def test_d7_a_report_before_expiration_is_marked_and_one_after_is_not(world):
    ev = ask(world)["events"]
    assert ev["earnings"]["before_expiration"] is True and ev["earnings"]["relation"] == "before_expiration" and ev["earnings"]["days_from_today"] == 15
    assert ev["earnings"]["date"] == "2026-12-10" and ev["earnings"]["hour"] == "amc" and ev["expiration"] == {"date": "2026-12-25", "days": 30}
    assert "2026-12-10" in ev["earnings"]["message"]
    world["earnings"] = earnings("2027-01-20")
    after = ask(world)["events"]["earnings"]
    assert after["before_expiration"] is False and after["relation"] == "after_expiration"
    world["earnings"] = earnings("2026-12-25")
    assert ask(world)["events"]["earnings"]["before_expiration"] is True            # the report of the day of expiration counts
    world["earnings"] = earnings(None)
    none = ask(world)["events"]["earnings"]
    assert none["ok"] and none["date"] is None and none["relation"] == "none"


def test_a_source_that_cannot_answer_the_calendar_is_no_data_with_its_source_never_a_date(world):
    world["earnings"] = {"ok": False, "source": "Calendar", "reason": "limit"}
    ev = ask(world)["events"]["earnings"]
    assert ev["ok"] is False and ev["date"] is None and ev["relation"] == "no_data" and ev["message"].startswith(("Sin dato de Calendar", "No data from Calendar"))


def test_the_ex_dividend_before_expiration_is_marked_with_the_note_that_the_model_ignores_it(world):
    world["dividend"] = dividend("2026-12-12")
    ev = ask(world)["events"]["ex_dividend"]
    assert ev["before_expiration"] is True and ev["date"] == "2026-12-12" and ev["days_from_today"] == 17 and ev["amount"] == 0.25 and ev["model_ignores_dividend"] is True
    world["dividend"] = dividend("2027-02-10")
    assert ask(world)["events"]["ex_dividend"]["relation"] == "after_expiration"
    world["dividend"] = dividend(None)
    none = ask(world)["events"]["ex_dividend"]
    assert none["ok"] and none["date"] is None and none["last_date"] == "2026-08-10" and none["relation"] == "none"
    world["dividend"] = dividend("2026-11-01")                                      # a date already past is not a coming one
    assert ask(world)["events"]["ex_dividend"]["relation"] == "none"


def test_without_a_source_of_ex_dividend_the_row_says_no_data(world):
    world["dividend"] = {"ok": False, "source": "la fecha ex-dividendo", "reason": "no session"}
    ev = ask(world)["events"]["ex_dividend"]
    assert ev["ok"] is False and ev["date"] is None and ev["relation"] == "no_data" and "no session" in ev["message"]
    assert ask(world)["ok"] is True                                                 # the events never take the table down


def test_a_leg_with_its_own_premium_uses_it_and_a_leg_without_quote_or_premium_is_no_data(world):
    given = ask(world, legs=[{"position": "long", "kind": "call", "strike": 100.0, "premium": 2.0}])
    assert given["legs"][0]["premium"] == 2.0 and given["legs"][0]["premium_source"] == "given" and given["metrics"]["net_premium_usd"] == 200.0
    far = ask(world, legs=[{"position": "long", "kind": "call", "strike": 150.0}])
    assert far["ok"] is False and far["code"] == "no_quote" and far["message"].startswith(("Sin dato de", "No data from"))
    far_with_premium = ask(world, legs=[{"position": "long", "kind": "call", "strike": 150.0, "premium": 0.1}])
    assert far_with_premium["ok"] is True and far_with_premium["legs"][0]["iv_source"] in ("premium", None)


def test_a_chain_that_cannot_answer_is_no_data_with_its_source(world):
    world["chain"] = {"ok": False, "source": "the option chain", "code": "no_connection", "reason": "connect a source"}
    out = ask(world)
    assert out["ok"] is False and out["code"] == "no_connection" and "connect a source" in out["message"] and "table" not in out


def test_an_expired_option_is_no_data_not_an_error(world):
    assert ask(world, expiration="2026-11-20")["code"] == "expired"
    world["chain"] = chain_of(years=0.0)
    assert ask(world)["code"] == "expired"


def test_the_leg_volatility_comes_from_the_mark_when_the_chain_has_none(world, monkeypatch):
    stripped = chain_of()
    for r in stripped["rows"]:
        r["call"]["iv"] = None
    world["chain"] = stripped
    monkeypatch.setattr(option_detail, "_iv_from_mark", lambda kind, spot, strike, mark, years, rate: 0.31)
    out = ask(world)
    assert out["legs"][0]["iv"] == 0.31 and out["legs"][0]["iv_source"] == "mark"
    monkeypatch.setattr(option_detail, "_iv_from_mark", lambda *a: None)
    none = ask(world)
    assert none["ok"] and all(row[0] is None for row in none["table"]["pnl_usd"]) and none["warnings"] and none["curves"]["mid_pnl_usd"] is None


@pytest.mark.parametrize("kw", [
    {"symbol": ""}, {"symbol": "AA PL"}, {"symbol": 5}, {"expiration": "tomorrow"}, {"expiration": "2026-13-45"}, {"expiration": None},
    {"legs": None}, {"legs": [], "structure": None}, {"legs": BULL, "structure": "iron_condor", "strikes": [1.0, 2.0, 3.0, 4.0]},
    {"legs": [{"position": "long", "kind": "call"}]}, {"legs": [{"position": "up", "kind": "call", "strike": 100.0}]},
    {"legs": [{"position": "long", "kind": "swap", "strike": 100.0}]}, {"legs": [{"position": "long", "instrument": "stock"}]},
    {"legs": BULL * 5}, {"legs": [{"position": "long", "kind": "call", "strike": -1.0}]}, {"legs": [{"position": "long", "kind": "call", "strike": 100.0, "premium": -1.0}]},
    {"legs": None, "structure": "nope", "strikes": [100.0]}, {"legs": None, "structure": "long_call", "strikes": [100.0, 110.0]},
    {"legs": None, "structure": "debit_call_spread", "strikes": [110.0, 100.0]}, {"range_pct": 2.9}, {"range_pct": 20.1}, {"range_pct": "ten"}, {"iv_mult": 0.4},
    {"iv_mult": 2.5}, {"rows": 12}, {"rows": 63}, {"rows": 1}, {"columns": 1}, {"columns": 13}, {"quantity": 0}, {"quantity": 5000}, {"quantity": True},
    {"legs": [{"instrument": "stock", "position": "long", "premium": 100.0, "quantity": 1e9}, {"position": "long", "kind": "call", "strike": 100.0}]}])
def test_a_request_that_is_not_valid_is_bad_option_detail_args(world, kw):
    with pytest.raises(DeskError) as err:
        ask(world, **kw)
    assert err.value.code == "bad_option_detail_args" and err.value.status == 422
    assert world["chain_calls"] == []                                             # nothing is read for a request that is refused


def test_the_endpoint_answers_and_refuses_the_same_way(world, client):
    url = f"/api/desks/{world['desk']['id']}/option-detail"
    r = client.post(url, json={"symbol": "aapl", "expiration": "2026-12-25", "legs": BULL, "range_pct": 5})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["symbol"] == "AAPL" and len(r.json()["table"]["rows"]) == 13
    for body in ({}, {"symbol": "AAPL"}):
        assert client.post(url, json=body).status_code == 422                       # a missing field is refused by the shape of the body
    for body in ({"symbol": "AAPL", "expiration": "2026-12-25"}, {"symbol": "AAPL", "expiration": "2026-12-25", "legs": BULL, "range_pct": 99}):
        bad = client.post(url, json=body)
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_option_detail_args"
    assert client.post("/api/desks/99999/option-detail", json={"symbol": "AAPL", "expiration": "2026-12-25", "legs": BULL}).status_code == 404
    assert client.post(url, json={"symbol": "AAPL", "expiration": "2026-12-25", "legs": BULL}, headers={"X-Hokma-Token": "wrong"}).status_code in (401, 403)


def test_the_size_of_the_grid_is_bounded_in_the_answer(world):
    out = ask(world, rows=61, columns=12, range_pct=20)
    assert len(out["table"]["rows"]) == 61 and len(out["table"]["columns"]) == 12 and len(out["curves"]["spots"]) <= 201
    assert out["limits"]["rows"]["max"] == 61 and out["limits"]["legs"]["max"] == 8 and out["limits"]["range_pct"] == {"min": 3.0, "max": 20.0, "default": 10.0}


def test_the_code_names_no_provider_the_name_arrives_as_data():
    import hokma.quant.options.detail as quant_detail

    for module in (option_detail, quant_detail):
        text = open(module.__file__, encoding="utf-8").read().lower()
        for name in ("schwab", "alpaca", "thinkorswim", "finnhub", "anthropic", "openai"):
            assert name not in text, (module.__name__, name)
        assert "—" not in text


# ---- the seams against the real modules, with their sources replaced ----------------------------------------------------------------------

def test_the_chain_loader_reads_the_expiration_asked_and_the_rate_of_the_chain_panel(monkeypatch):
    from hokma.market import options

    seen = {}

    def fake_chain(symbol, dates, strikes=None, desk_id=None):
        seen.update(symbol=symbol, dates=list(dates), strikes=strikes)
        return {"underlying_price": 100.0, "source": "Feed", "price_feed": "live", "greeks": "provider", "as_of": "t", "market_open": True, "delayed": False, "notes": [],
                "expirations": [{"date": dates[0], "days": 30, "multiplier": 100, "rows": rows()}]}

    monkeypatch.setattr(options, "chain", fake_chain)
    got = option_detail._load_chain(1, "AAPL", date(2026, 12, 18))
    assert got["ok"] and seen["dates"] == ["2026-12-18"] and got["rate"] == options.PROB_ITM_RATE and got["multiplier"] == 100.0 and got["spot"] == 100.0


def test_the_chain_loader_turns_a_source_failure_into_no_data(monkeypatch):
    from hokma.market import options

    def broken(symbol, dates, strikes=None, desk_id=None):
        raise options.OptionsError("no_connection", 409, "connect a source")

    monkeypatch.setattr(options, "chain", broken)
    got = option_detail._load_chain(1, "AAPL", date(2026, 12, 18))
    assert got["ok"] is False and got["code"] == "no_connection" and got["reason"] == "connect a source"
    monkeypatch.setattr(options, "chain", lambda *a, **k: {"underlying_price": None, "expirations": [{"rows": rows()}]})
    assert option_detail._load_chain(1, "AAPL", date(2026, 12, 18))["code"] == "empty"


# ---- the ex-dividend date of the market module -----------------------------------------------------------------------------------------

@pytest.fixture
def quotes(monkeypatch):
    import json
    from pathlib import Path

    from hokma.market import options
    from hokma.schwab import models

    options.reset()
    data = json.loads((Path(__file__).parent / "schwab_fixtures" / "quotes.json").read_text(encoding="utf-8"))
    calls = []

    def fake_quotes(symbols, fields=None, indicative=None):
        calls.append((list(symbols), fields))
        return models.QuoteResponse.read({k: v for k, v in data.items() if k in symbols})

    monkeypatch.setattr(options.schwab_endpoints, "quotes", fake_quotes)
    monkeypatch.setattr(options, "_providers", lambda desk_id: ["schwab"])
    yield calls
    options.reset()


def test_the_ex_dividend_comes_from_the_fundamental_block_and_is_cached(quotes):
    from hokma.market import options

    got = options.ex_dividend("AAPL", 1)
    assert got["next_ex_date"] == date(2026, 11, 20) and got["last_ex_date"] == date(2026, 11, 20) and got["amount"] == 11.75 and got["frequency"] == 4
    assert got["source"] == "Schwab" and quotes == [(["AAPL"], "fundamental")]
    options.ex_dividend("AAPL", 1)
    assert len(quotes) == 1                                                       # the panel asks on every slider: one read per 15 minutes


def test_the_ex_dividend_block_of_the_desk_turns_it_into_the_event_data_and_a_refusal_into_no_data(quotes, monkeypatch):
    from hokma.market import options

    block = option_detail._ex_dividend_block(1, "AAPL")
    assert block["ok"] and block["next"] == "2026-11-20" and block["last"] == "2026-11-20" and block["amount"] == 11.75
    monkeypatch.setattr(options, "_providers", lambda desk_id: ["alpaca"])
    options.reset()
    refused = option_detail._ex_dividend_block(1, "AAPL")
    assert refused["ok"] is False and refused["source"] in ("la fecha ex-dividendo", "the ex-dividend date") and refused["reason"]


def test_a_symbol_the_fundamental_block_does_not_cover_is_no_ex_dividend(quotes):
    from hokma.market import options

    with pytest.raises(options.OptionsError) as err:
        options.ex_dividend("MSFT", 1)
    assert err.value.code == "no_ex_dividend" and err.value.status == 404


def test_any_failure_of_the_calendar_or_dividend_source_is_no_data_never_a_500(world, monkeypatch):
    from hokma.kit import company_tools
    from hokma.market import options
    world["earnings"] = None
    monkeypatch.undo()

    def boom(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(company_tools, "earnings_calendar", boom)
    monkeypatch.setattr(options, "ex_dividend", boom)
    block = option_detail._earnings_block(1, "AAPL", TODAY)
    assert block["ok"] is False and block["reason"]
    block = option_detail._ex_dividend_block(1, "AAPL")
    assert block["ok"] is False and block["reason"] and "exploded" not in block["reason"]


def test_the_endpoint_carries_the_three_marks_of_the_breakevens_at_half_of_the_term(world, client):
    """3339-A, Vera: the panel reads these three keys to say a breakeven of the curve at half of the term is approximate and only inside the range."""
    r = client.post(f"/api/desks/{world['desk']['id']}/option-detail", json={"symbol": "AAPL", "expiration": "2026-12-25", "legs": BULL, "range_pct": 10})
    assert r.status_code == 200
    curves = r.json()["curves"]
    assert curves["mid_breakevens_approximate"] is True and curves["mid_breakevens_in_range_only"] is True
    assert isinstance(curves["mid_breakevens"], list) and len(curves["mid_breakevens"]) == 1
    low, high = curves["mid_breakevens_range"]
    assert low < curves["mid_breakevens"][0] < high and low == pytest.approx(curves["spots"][0]) and high == pytest.approx(curves["spots"][-1])
    assert r.json()["breakevens"] == pytest.approx(r.json()["metrics"]["breakevens"])               # the ones at expiration are exact: they do not carry the marks


def test_the_endpoint_says_sin_dato_de_the_source_when_the_calendar_or_the_dividend_source_fails(world, client):
    """3339-A, Vera: a source that fails is "Sin dato de {source}" in its event, in the answer of the route, and the table is still there."""
    world["earnings"] = {"ok": False, "source": "Calendar", "reason": "the source did not answer"}
    world["dividend"] = {"ok": False, "source": "Quotes", "reason": "the source did not answer"}
    r = client.post(f"/api/desks/{world['desk']['id']}/option-detail", json={"symbol": "AAPL", "expiration": "2026-12-25", "legs": BULL})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["table"]["rows"]
    for name, source in (("earnings", "Calendar"), ("ex_dividend", "Quotes")):
        event = body["events"][name]
        assert event["ok"] is False and event["relation"] == "no_data" and event["date"] is None
        assert source in event["message"] and "the source did not answer" in event["message"]
