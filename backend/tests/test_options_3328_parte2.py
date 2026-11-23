"""Tarea 3328 part 2 (1.0.11): the option chain of Cristian's Schwab test on Sunday 22 Nov 2026 (QQQ, market closed: last 617.74, bid 617.50,
ask 617.80, regular close 608.45) against thinkorswim. Numbers are those of the evidence (Opciones_hokma.PNG, opciones_thinkorswing.PNG and
schwab-streaming-20261122T221129Z.json). No network."""
import json
from datetime import date, datetime, timezone

import pytest

from hokma.market import options
from hokma.market.option_subs import OptionSubs
from hokma.market.stream import OptionQuote
from test_options_live import DAY, hub_of, key, oq, open_options, source, structure  # noqa: F401  (source is a fixture)
from test_schwab_market import (CHAINS, EXPIRATIONS, HOURS_OPTION, _server, connect_schwab, no_network, paths, schwab)  # noqa: F401  (fixtures)
from test_hub_streaming import message, run

SUNDAY_EVENING = datetime(2026, 11, 22, 22, 20, tzinfo=timezone.utc).timestamp()          # 17:20 in New York
QQQ = {"symbol": "QQQ", "last": 617.74, "bid": 617.5, "ask": 617.8, "mark": 608.45, "close": 608.45}


def qqq_contract(kind: str, strike: float, **over) -> dict:
    """A contract as the REAL /chains answer sends it: bid, ask, last and mark (not the REF's bidPrice, askPrice, lastPrice and markPrice)."""
    letter = kind[0].upper()
    base = {"putCall": kind.upper(), "symbol": f"QQQ   261124{letter}{int(strike * 1000):08d}", "bid": 14.06, "ask": 14.53, "last": 14.52,
            "mark": 14.3, "totalVolume": 169, "quoteTimeInLong": 1795222799827, "volatility": 60.0, "delta": 0.8, "theta": -2.77,
            "openInterest": 0.0, "strikePrice": strike, "expirationDate": "2026-11-24T21:00:00.000+00:00", "daysToExpiration": 2,
            "multiplier": 100.0}
    return {**base, **over}


def qqq_chain(**over) -> dict:
    body = {"symbol": "QQQ", "status": "SUCCESS", "strategy": "SINGLE", "isDelayed": False, "underlyingPrice": 617.74, "underlying": QQQ,
            "callExpDateMap": {"2026-11-24:2": {"611.0": [qqq_contract("call", 611)], "618.0": [qqq_contract("call", 618, bid=6.95, ask=7.5, last=0.0)]}},
            "putExpDateMap": {"2026-11-24:2": {"611.0": [qqq_contract("put", 611, bid=0.1, ask=0.12, last=0.11)],
                                               "618.0": [qqq_contract("put", 618, bid=11.0, ask=11.2, last=11.1)]}}}
    return {**body, **over}


def sunday_hours(is_open: bool = False) -> dict:
    return {"option": {"EQO": {"date": "2026-11-22", "marketType": "OPTION", "product": "EQO", "productName": "equity option", "isOpen": is_open}}}


# ---------------------------------------------------------------------------------------------- 5: Last, Bid and Ask are not N/A

def test_the_chain_reads_bid_ask_and_last_with_the_names_the_real_answer_uses(monkeypatch, schwab):
    """Cause: the REST reader looked for bidPrice, askPrice and lastPrice (the names of the REF) and the real /chains answer sends bid, ask and last."""
    connect_schwab(monkeypatch, now=SUNDAY_EVENING)
    schwab.on("GET", HOURS_OPTION, sunday_hours())
    schwab.on("GET", CHAINS, qqq_chain())
    rows = options.chain("QQQ", "2026-11-24", "all")["expirations"][0]["rows"]
    call = rows[0]["call"]
    assert (call["bid"], call["ask"], call["last"]) == (14.06, 14.53, 14.52)          # what thinkorswim shows for the 611 call of 24 NOV
    assert rows[0]["put"]["bid"] == 0.1 and rows[1]["put"]["last"] == 11.1


def test_the_names_of_the_ref_are_still_read_and_a_last_of_zero_is_no_value(monkeypatch, schwab):
    connect_schwab(monkeypatch, now=SUNDAY_EVENING)
    schwab.on("GET", HOURS_OPTION, sunday_hours())
    old = qqq_contract("call", 611)
    for name in ("bid", "ask", "last"):
        old[name + "Price"] = old.pop(name)
    schwab.on("GET", CHAINS, qqq_chain(callExpDateMap={"2026-11-24:2": {"611.0": [old], "618.0": [qqq_contract("call", 618, last=0.0)]}}))
    rows = options.chain("QQQ", "2026-11-24", "all")["expirations"][0]["rows"]
    assert (rows[0]["call"]["bid"], rows[0]["call"]["last"]) == (14.06, 14.52)
    assert rows[1]["call"]["last"] is None and rows[1]["call"]["bid"] == 14.06               # Schwab's last 0 is "no trade yet", the bid stays


def test_a_message_of_the_stream_without_a_field_never_erases_what_the_chain_gave():
    """The stream sends None for what it has not said; the cell used to take the None over the value of the REST chain."""
    cell = {"symbol": "X", "bid": 14.06, "ask": 14.53, "last": 14.52, "iv": 0.6, "itm": True}
    out = OptionSubs._live_cell(cell, {"bid": None, "ask": 14.6, "last": None, "iv": None, "volume": 5}, 611.0, "call", 617.5, 0.005)
    assert "bid" not in out and "last" not in out and out["ask"] == 14.6 and out["volume"] == 5 and out["iv"] == 0.6


def test_the_live_cell_takes_the_reference_price_and_keeps_the_last_values_out_of_hours(source, monkeypatch):
    real = structure

    def closed(*args, **kwargs):
        return {**real(*args, **kwargs), "market_open": False}

    monkeypatch.setattr(options, "structure", closed)

    async def go():
        hub = hub_of()
        client, snap = await open_options(hub, 4)
        row = next(r for r in snap["expirations"][0]["rows"] if r["strike"] == 500.0)
        assert (row["call"]["bid"], row["call"]["ask"], row["call"]["last"]) == (0.9, 1.1, 1.0)      # the first photo keeps the chain's values
        assert row["call"]["intrinsic"] == 0.39                                                      # market closed: the bid 500.39, not the last 500.4
        source["source"].emit(oq(500.0, "C", bid=None, ask=1.3, last=None, volume=7))
        update = await message(client, lambda m: m["type"] == "options")
        (change,) = update["changes"]
        assert "bid" not in change and "last" not in change and change["ask"] == 1.3 and change["intrinsic"] == 0.39
        await hub.close()
    run(go())


# ---------------------------------------------------------------------------------------------- 6: every expiration Schwab gives

EXPIRATION_LIST = ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-27", "2026-11-30", "2026-12-01", "2026-12-18", "2027-01-15"]


def test_every_expiration_of_schwab_is_listed_and_the_first_is_tomorrow_monday(monkeypatch, schwab):
    """The list of the evidence starts with 2026-11-23 (daysToExpiration 1): on Sunday evening in New York it is tomorrow and it must be there."""
    connect_schwab(monkeypatch, now=SUNDAY_EVENING)
    schwab.on("GET", EXPIRATIONS, {"status": "SUCCESS", "expirationList": [
        {"daysToExpiration": (date.fromisoformat(d) - date(2026, 11, 22)).days, "expirationDate": d, "expirationType": "W", "standard": True,
         "settlementType": "P", "optionRoots": "QQQ"} for d in EXPIRATION_LIST]})
    out = options.expirations("QQQ")
    assert [e["date"] for e in out["expirations"]] == EXPIRATION_LIST
    assert out["expirations"][0] == {"date": "2026-11-23", "days": 1, "multiplier": 100}


def test_the_expiration_of_today_and_of_the_next_session_are_both_kept_the_day_of_the_close(monkeypatch, schwab):
    connect_schwab(monkeypatch, now=SUNDAY_EVENING)
    schwab.on("GET", EXPIRATIONS, {"status": "SUCCESS", "expirationList": [
        {"daysToExpiration": 0, "expirationDate": "2026-11-22"}, {"daysToExpiration": 1, "expirationDate": "2026-11-23"}]})
    assert [e["days"] for e in options.expirations("QQQ")["expirations"]] == [0, 1]


# ---------------------------------------------------------------------------------------------- 7: intrinsic against the same price as thinkorswim

def test_the_reference_price_is_the_bid_with_the_market_closed_and_the_last_otherwise():
    assert options.reference_price(617.74, 617.5, False) == 617.5
    assert options.reference_price(617.74, 617.5, True) == 617.74 and options.reference_price(617.74, 617.5, None) == 617.74
    assert options.reference_price(617.74, None, False) == 617.74 and options.reference_price(617.74, 0.0, False) == 617.74


def test_intrinsic_value_with_the_market_closed_is_thinkorswims(monkeypatch, schwab):
    connect_schwab(monkeypatch, now=SUNDAY_EVENING)
    schwab.on("GET", HOURS_OPTION, sunday_hours(False))
    schwab.on("GET", CHAINS, qqq_chain())
    rows = options.chain("QQQ", "2026-11-24", "all")["expirations"][0]["rows"]
    assert rows[0]["call"]["intrinsic"] == 6.5 and rows[1]["put"]["intrinsic"] == 0.5          # 617.50 - 611 and 618 - 617.50, thinkorswim's 6.50 and 0.50
    assert rows[0]["call"]["itm"] is True and rows[1]["call"]["itm"] is False


def test_intrinsic_value_with_the_market_open_is_against_the_last(monkeypatch, schwab):
    connect_schwab(monkeypatch, now=SUNDAY_EVENING)
    schwab.on("GET", HOURS_OPTION, sunday_hours(True))
    schwab.on("GET", CHAINS, qqq_chain())
    rows = options.chain("QQQ", "2026-11-24", "all")["expirations"][0]["rows"]
    assert round(rows[0]["call"]["intrinsic"], 2) == 6.74


# ---------------------------------------------------------------------------------------------- 8: Prob. ITM and expected move

def test_prob_itm_is_black_scholes_with_the_iv_of_the_contract_and_matches_thinkorswim_on_the_money():
    """23 NOV, S = 617.50, Schwab's IV of the stream (field 10, in percent): 618 call 64.26 % and 617 call 63.23 %; thinkorswim: 48.47 % and 50.66 %."""
    years = 1 / 365
    assert abs(options.prob_itm("call", 617.5, 618.0, 0.6426, years) - 0.4847) < 0.002
    assert abs(options.prob_itm("call", 617.5, 617.0, 0.6323, years) - 0.5066) < 0.004
    d2 = (options.math.log(617.5 / 618.0) - 0.6426 ** 2 * years / 2) / (0.6426 * options.math.sqrt(years))
    assert options.prob_itm("call", 617.5, 618.0, 0.6426, years) == round(options._cdf(d2), 4)
    assert options.prob_itm("put", 617.5, 618.0, 0.6426, years) == round(1 - options._cdf(d2), 4)


def test_the_probability_of_the_chain_is_measured_against_the_reference_price(monkeypatch, schwab):
    connect_schwab(monkeypatch, now=SUNDAY_EVENING)
    schwab.on("GET", HOURS_OPTION, sunday_hours(False))
    schwab.on("GET", CHAINS, qqq_chain())
    group = options.chain("QQQ", "2026-11-24", "all")["expirations"][0]
    years = options.years_to_expiry(date(2026, 11, 24))
    call = group["rows"][0]["call"]
    assert call["prob_itm"] == options.contract_prob_itm("call", 617.5, 611.0, call["bid"], call["ask"], 0.6, years)
    assert group["expected_move"] == options.expected_move(617.5, group["iv"], years)


def test_the_expected_move_uses_the_factor_thinkorswim_uses():
    """thinkorswim 23 NOV: 83.20 % and +/-20.701; 24 NOV: 77.89 % and +/-27.722. S x IV x sqrt(T) x sqrt(2/pi) gives T = 0.93 and 1.91 days, one day apart."""
    for iv, move, days in ((0.832, 20.701, 0.931), (0.7789, 27.722, 1.905)):
        assert abs(options.expected_move(617.5, iv, days / 365) - move) < 0.05
