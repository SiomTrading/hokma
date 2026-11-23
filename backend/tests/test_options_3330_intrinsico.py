"""Tarea 3330 (decision 1269): the intrinsic value of a Schwab contract is the `intrinsicValue` of /chains as Schwab sends it, not a number Hokma
calculates. Hokma computes it only when Schwab sends none. No network."""
from hokma.market import options
from hokma.market.option_subs import OptionSubs
from test_schwab_market import _server  # noqa: F401  (fixture)
from test_options_3328_parte2 import (CHAINS, HOURS_OPTION, SUNDAY_EVENING, connect_schwab, qqq_chain, qqq_contract, schwab, sunday_hours,  # noqa: F401
                                      no_network, paths)  # noqa: F401  (fixtures)


def test_the_intrinsic_value_is_the_one_schwab_sends_in_the_chain(monkeypatch, schwab):
    connect_schwab(monkeypatch, now=SUNDAY_EVENING)
    schwab.on("GET", HOURS_OPTION, sunday_hours(False))
    schwab.on("GET", CHAINS, qqq_chain(callExpDateMap={"2026-11-24:2": {"611.0": [qqq_contract("call", 611, intrinsicValue=6.31)],
                                                                        "618.0": [qqq_contract("call", 618, intrinsicValue=0.0)]}},
                                       putExpDateMap={"2026-11-24:2": {"611.0": [qqq_contract("put", 611, intrinsicValue=0.0)],
                                                                       "618.0": [qqq_contract("put", 618, intrinsicValue=0.77)]}}))
    rows = options.chain("QQQ", "2026-11-24", "all")["expirations"][0]["rows"]
    assert rows[0]["call"]["intrinsic"] == 6.31 and rows[1]["put"]["intrinsic"] == 0.77        # computed against the bid they would be 6.50 and 0.50
    assert rows[1]["call"]["intrinsic"] == 0.0 and rows[0]["put"]["intrinsic"] == 0.0           # a real zero is a value, not a missing one


def test_without_intrinsic_value_or_with_schwabs_no_data_the_intrinsic_is_computed(monkeypatch, schwab):
    connect_schwab(monkeypatch, now=SUNDAY_EVENING)
    schwab.on("GET", HOURS_OPTION, sunday_hours(False))
    schwab.on("GET", CHAINS, qqq_chain(callExpDateMap={"2026-11-24:2": {"611.0": [qqq_contract("call", 611, intrinsicValue=-999.0)],
                                                                        "618.0": [qqq_contract("call", 618)]}}))
    rows = options.chain("QQQ", "2026-11-24", "all")["expirations"][0]["rows"]
    assert rows[0]["call"]["intrinsic"] == 6.5 and rows[1]["call"]["intrinsic"] == 0.0         # 617.50 - 611 (reference price of 3328); 618 is out of the money


def test_the_live_cell_keeps_schwabs_intrinsic_until_the_underlying_moves():
    cell = {"symbol": "X", "iv": 0.6, "itm": True, "intrinsic": 6.31}
    kept = OptionSubs._live_cell(cell, {"ask": 14.6}, 611.0, "call", 617.5, 0.005, True)
    moved = OptionSubs._live_cell(cell, {"ask": 14.6}, 611.0, "call", 618.5, 0.005, False)
    assert kept["intrinsic"] == 6.31 and moved["intrinsic"] == 7.5
    assert OptionSubs._live_cell({**cell, "intrinsic": None}, {}, 611.0, "call", 617.5, 0.005, True)["intrinsic"] == 6.5      # nothing from Schwab: computed
