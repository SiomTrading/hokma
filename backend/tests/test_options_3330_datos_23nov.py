"""Tarea 3330, 23-nov-2026 (QQQ): what Schwab sends against what thinkorswim shows. The values are those of Cristian's files: the diagnostic
schwab-streaming-20261123T160706Z.json (chain and stream at 11:05 in New York) and the screenshots Ociones_QQQ_2311_Think.PNG and Opciones_QQQ_2311.PNG
(pre-market, 9:25). Only a few contracts are copied here. No network."""
import math
from datetime import date

import pytest

from hokma.market import options
from hokma.market.adapters.schwab import option_quote_of
from hokma.market.option_subs import OptionSubs

# /chains contracts of the diagnostic, as Schwab sent them (REST rows of the file; `intrinsic` is its intrinsicValue)
CHAIN = {
    ("call", 648): {"bid": 5.79, "ask": 5.85, "mark": 5.82, "last": 6.03, "volatility": 62.611, "theta": -2.675, "delta": 0.613, "intrinsicValue": 3.34},
    ("put", 648): {"bid": 2.5, "ask": 2.54, "mark": 2.52, "last": 2.51, "volatility": 62.528, "theta": -2.7, "delta": -0.387, "intrinsicValue": -3.34},
    ("call", 661): {"bid": 0.56, "ask": 0.57, "mark": 0.56, "last": 0.57, "volatility": 54.533, "theta": -0.505, "delta": 0.12, "intrinsicValue": -9.66},
    ("put", 661): {"bid": 10.17, "ask": 10.34, "mark": 10.26, "last": 10.66, "volatility": 54.533, "theta": -0.536, "delta": -0.88, "intrinsicValue": 9.66},
}


def test_the_chain_shows_the_last_theta_and_prices_exactly_as_schwab_sends_them():
    for (kind, strike), item in CHAIN.items():
        quote = options._schwab_quote(item)
        assert quote["last"] == item["last"] and quote["theta"] == item["theta"] and quote["bid"] == item["bid"] and quote["ask"] == item["ask"]
        assert quote["delta"] == item["delta"] and quote["iv"] == pytest.approx(item["volatility"] / 100.0)


def test_the_stream_shows_the_last_and_theta_exactly_as_schwab_sends_them():
    """Two messages of the file for the 23 NOV 648 call: field 4 is the last trade (with 18, its size, and 39, its time) and field 30 the theta."""
    traded = option_quote_of("QQQ   261123C00648000", {"2": 6.36, "3": 6.41, "4": 6.41, "8": 13523, "18": 5, "37": 6.385, "38": 1795449958630, "39": 1795449958367}, False, 0)
    greeks = option_quote_of("QQQ   261123C00648000", {"2": 6.34, "3": 6.39, "10": 62.50613862, "28": 0.66361443, "30": -2.24415321, "34": 6.30415321,
                                                       "37": 6.365, "38": 1795449957558}, False, 0)
    assert traded.last == 6.41 and traded.volume == 13523 and greeks.theta == -2.24415321 and greeks.delta == 0.66361443
    assert options._iv(greeks.iv) == pytest.approx(0.6250613862)
    cell = OptionSubs._live_cell({"symbol": "X", "iv": 0.62, "itm": True, "intrinsic": 3.34, "last": 6.03, "theta": -2.675}, {"last": 6.41, "theta": -2.24415321},
                                 648.0, "call", 651.34, 0.0002, True)
    assert cell["last"] == 6.41 and cell["theta"] == -2.24415321           # the live value is the one on screen, untouched


def test_an_out_of_the_money_contract_shows_zero_intrinsic_not_schwabs_signed_distance():
    """The file: 648 put -3.34 and 661 call -9.66 at 651.34. thinkorswim shows 0 there; the in the money side stays as Schwab sends it (648 call 3.34, 661 put 9.66)."""
    got = {key: options._schwab_quote(item)["intrinsic"] for key, item in CHAIN.items()}
    assert got == {("call", 648): 3.34, ("put", 648): 0.0, ("call", 661): 0.0, ("put", 661): 9.66}
    assert options._schwab_quote({"bid": 1.0, "ask": 1.1})["intrinsic"] is None and options._schwab_quote({"intrinsicValue": -999.0})["intrinsic"] is None


def test_the_intrinsic_of_the_chain_is_the_distance_to_the_strike_of_the_underlying_last_when_open():
    """At 11:05 the underlying was 651.34: Schwab's 3.34 for the 648 call is 651.34 - 648, the same figure thinkorswim calculates."""
    assert options.intrinsic_value("call", 651.34, 648) == CHAIN[("call", 648)]["intrinsicValue"]
    assert options.intrinsic_value("put", 651.34, 661) == CHAIN[("put", 661)]["intrinsicValue"]


# ---------------------------------------------------------------------------------------------- expected move

SUNDAY_F, MONDAY_F = 0.9587, 0.285           # days from the screenshot to 16:00 of the nearest expiration (calendar days, as the T of Prob. ITM)


@pytest.mark.parametrize("days, iv, tos", [
    (MONDAY_F, 1.0505, 14.222), (1 + MONDAY_F, 0.5620, 16.186), (2 + MONDAY_F, 0.5040, 19.364),
    (4 + MONDAY_F, 0.3890, 20.471), (7 + MONDAY_F, 0.3459, 23.740), (8 + MONDAY_F, 0.3562, 26.076),
])
def test_the_expected_move_of_monday_23_nov_is_thinkorswims_within_one_percent(days, iv, tos):
    """Pre-market: bid 654.68, regular last 608.45. The header IV is thinkorswim's own, read from its screen."""
    spot = options.move_price(654.68, 608.45, False)
    assert spot == 608.45
    assert abs(options.expected_move(spot, iv, days / 365.0) - tos) / tos < 0.01


@pytest.mark.parametrize("days, iv, tos", [(SUNDAY_F, 0.832, 20.701), (9 + SUNDAY_F - 1, 0.5262, 40.109), (53 + SUNDAY_F, 0.4214, 79.349)])
def test_the_expected_move_of_sunday_22_nov_is_thinkorswims_within_one_percent(days, iv, tos):
    spot = options.move_price(617.5, 608.45, False)
    assert abs(options.expected_move(spot, iv, days / 365.0) - tos) / tos < 0.01


def test_the_expected_move_was_measured_against_the_bid_before_and_is_now_against_the_regular_last():
    old = options.expected_move(654.42, 0.5878, (1 + MONDAY_F) / 365.0)           # 1.0.12: 18.1, thinkorswim 16.186 at its own IV of 56.20 %
    assert abs(old - 18.125) < 0.1
    assert options.move_price(654.42, 651.3, True) == 654.42 and options.move_price(654.42, None, False) == 654.42      # open, or no regular last: the reference


def test_the_group_expected_move_uses_the_move_price_not_the_reference():
    rows = [{"strike": 654.0, "call": {"iv": 0.5620}, "put": {"iv": 0.5620}}]
    years = (1 + MONDAY_F) / 365.0
    day = date(2026, 11, 24)
    measured = options._group(day, date(2026, 11, 23), 100, rows, 654.68, years, None, False, 608.45)
    assert abs(measured["expected_move"] - 16.186) / 16.186 < 0.01 and measured["iv"] == 0.562
    assert options._group(day, date(2026, 11, 23), 100, rows, 654.68, years, None, False)["expected_move"] > 17.0       # no move price: the reference, as before


# ---------------------------------------------------------------------------------------------- Prob. ITM

@pytest.mark.parametrize("strike, iv, tos", [(649, 0.551, 43.34), (650, 0.528, 44.37), (652, 0.530, 47.10), (654, 0.526, 49.78)])
def test_prob_itm_of_the_25_nov_puts_is_thinkorswims_within_one_point(strike, iv, tos):
    """S = 654.68 (thinkorswim's bid), T = calendar days over 365 and the IV Schwab gave Hokma for the contract: the same Black-Scholes d2."""
    got = options.prob_itm("put", 654.68, strike, iv, (2 + 395 / 1440) / 365.0) * 100.0
    assert abs(got - tos) < 1.0


def test_prob_itm_of_call_and_put_with_one_iv_add_up_to_one():
    """Schwab sends one IV for the call and the put of a strike; thinkorswim shows its own per contract IV, which is why its two columns may not add to 100."""
    years = (1 + 0.2743) / 365.0
    assert math.isclose(options.prob_itm("call", 654.68, 654, 0.55, years) + options.prob_itm("put", 654.68, 654, 0.55, years), 1.0, abs_tol=2e-4)
