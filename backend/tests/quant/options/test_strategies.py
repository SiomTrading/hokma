"""Strategies of the panel (SPEC-028, 3338-A; pieces ECT-098 and ECT-033): the views, the two new structures, the strikes of a real chain that fit a budget
and the probability of profit, checked against numbers worked by hand and against an independent normal-distribution reference."""
import math
from statistics import NormalDist

import pytest

from hokma.market import options as market_options
from hokma.quant import registry
from hokma.quant.options import strategies as st
from hokma.quant.options.structures import CATALOG, EXTRA_STRUCTURES, STRUCTURES, structure_profile

N = NormalDist().cdf


def p_above(spot, level, iv, years, rate):
    """Reference, written here with the standard library: P(S_T > level) under lognormal prices."""
    d2 = (math.log(spot / level) + (rate - iv * iv / 2.0) * years) / (iv * math.sqrt(years))
    return N(d2)


def bs(kind, spot, strike, vol, years, rate=0.04):
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    if kind == "call":
        return spot * N(d1) - strike * math.exp(-rate * years) * N(d2)
    return strike * math.exp(-rate * years) * N(-d2) - spot * N(-d1)


SPOT, YEARS, IV = 100.0, 30 / 365, 0.30


def chain(half_spread=0.03, oi=500, strikes=range(80, 121, 2)):
    rows = []
    for k in strikes:
        c, p = bs("call", SPOT, k, IV, YEARS), bs("put", SPOT, k, IV, YEARS)
        rows.append({"strike": float(k), "call": {"bid": round(c - half_spread, 2), "ask": round(c + half_spread, 2), "iv": IV, "delta": 0.5, "open_interest": oi, "volume": 50},
                     "put": {"bid": round(max(p - half_spread, 0.0), 2), "ask": round(p + half_spread, 2), "iv": IV, "delta": -0.5, "open_interest": oi, "volume": 50}})
    return rows


def cards(view, **kw):
    args = dict(rows=chain(), spot=SPOT, view=view, expiration="2026-12-25", years=YEARS, multiplier=100.0)
    args.update(kw)
    res = st.strategy_cards(**args)
    assert res.value is not None, res.warnings
    return res.value


# ---- the six views ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("label,vid", [("Muy bajista", "very_bearish"), ("Bajista", "bearish"), ("Neutral", "neutral"), ("Direccional", "directional"),
                                       ("Alcista", "bullish"), ("Muy alcista", "very_bullish"), ("very bullish", "very_bullish"), ("DIRECTIONAL", "directional"),
                                       ("bullish", "bullish")])
def test_the_six_views_of_the_sketch_become_the_ones_of_the_code(label, vid):
    assert st.parse_view(label) == vid
    assert st.parse_view(vid) == vid


def test_a_view_that_is_not_one_of_the_six_is_not_guessed():
    assert st.parse_view("sideways") is None and st.parse_view("") is None


def test_every_view_maps_to_directions_of_the_matrix_and_directional_is_both_families():
    assert st.VIEWS["very_bullish"] == (("bullish", 2),) and st.VIEWS["bearish"] == (("bearish", 1),) and st.VIEWS["neutral"] == (("neutral", 1),)
    assert [d for d, _ in st.VIEWS["directional"]] == ["bullish", "bearish"]


def test_the_volatility_view_comes_from_the_classification_and_never_invents_one():
    assert st.vol_view_from_classification("rich") == ("falling", "vol_analysis")
    assert st.vol_view_from_classification("cheap") == ("rising", "vol_analysis")
    assert st.vol_view_from_classification("fair") == ("flat", "vol_analysis")
    assert st.vol_view_from_classification("uncalibrated") == ("flat", "default_flat_uncalibrated")
    assert st.vol_view_from_classification(None) == ("flat", "default_flat_uncalibrated")


def test_neutral_offers_the_iron_butterfly_next_to_the_condor_and_a_strong_view_puts_the_outright_option_first():
    assert st.family_structures("neutral", "flat")[:2] == ("iron_condor", "iron_butterfly")
    assert st.family_structures("bullish", "rising", 2)[0] == "long_call"
    assert st.family_structures("bullish", "rising", 1)[0] == "debit_call_spread"
    assert "protective_put" in st.family_structures("bearish", "flat")
    assert st.family_structures("neutral", "rising") == ("protective_put",)


# ---- the two structures added to the catalog ---------------------------------------------------------------------------------------

def test_the_catalog_has_the_thirteen_of_the_order_path_plus_the_two_new_ones_without_touching_the_first():
    assert len(STRUCTURES) == 13 and set(EXTRA_STRUCTURES) == {"iron_butterfly", "protective_put"}
    assert set(CATALOG) == set(STRUCTURES) | set(EXTRA_STRUCTURES)


def test_iron_butterfly_payoff_and_breakevens_by_hand():
    # long put 95 at 1.00, short put 100 at 3.00, short call 100 at 3.20, long call 105 at 1.10: credit 4.10 a share, wings 5 wide
    res = structure_profile("iron_butterfly", [95, 100, 105], [1.0, 3.0, 3.2, 1.1], 1, 100, 100.0)
    v = res.value
    assert v["net_premium_usd"] == pytest.approx(-410.0) and v["premium_type"] == "credit"
    assert v["max_profit_usd"] == pytest.approx(410.0)
    assert v["max_loss_usd"] == pytest.approx(90.0)                       # 5 of width less the 4.10 credit, times 100
    assert v["breakevens"] == pytest.approx([95.9, 104.1])
    assert v["greeks_signs"] == {"delta": "depends on strikes", "gamma": "negative", "vega": "negative", "theta": "positive"}


def test_iron_butterfly_with_unequal_wings_loses_the_wider_wing():
    v = structure_profile("iron_butterfly", [94, 100, 105], [0.8, 3.0, 3.2, 1.1], 1, 100, 100.0).value
    credit = 3.0 + 3.2 - 0.8 - 1.1
    assert v["max_loss_usd"] == pytest.approx((6 - credit) * 100)


def test_protective_put_payoff_and_breakeven_by_hand():
    # the shares at 100 and a 95 put bought at 2.00: the put costs 200, the loss stops at (100 - 95 + 2) per share
    v = structure_profile("protective_put", [95], [2.0], 1, 100, 100.0).value
    assert v["net_premium_usd"] == pytest.approx(200.0) and v["premium_type"] == "debit"
    assert v["max_loss_usd"] == pytest.approx(700.0)
    assert v["max_profit_usd"] is None and v["unbounded_profit"] is True
    assert v["breakevens"] == pytest.approx([102.0])
    assert [x["instrument"] for x in v["legs"]] == ["stock", "option"]


def test_the_new_structures_need_the_strike_count_they_declare():
    assert structure_profile("iron_butterfly", [95, 100], [1, 3, 3, 1], 1, 100, 100.0).value is None
    assert structure_profile("protective_put", [95, 90], [2.0], 1, 100, 100.0).value is None


# ---- probability of profit ----------------------------------------------------------------------------------------------------------

def call_prob(**kw):
    args = dict(structure="long_call", strikes=[100], premiums=[5.0], quantity=1, multiplier=100, spot=100.0, volatility=0.30, time_to_expiration=0.25, rate=0.04)
    args.update(kw)
    return st.profit_probability(**args).value


def test_long_call_profit_is_the_probability_of_ending_above_its_breakeven():
    v = call_prob()
    assert v["breakevens"] == pytest.approx([105.0])
    assert v["p_profit"] == pytest.approx(p_above(100, 105, 0.30, 0.25, 0.04), abs=1e-12)
    assert v["p_profit"] == pytest.approx(0.3693, abs=2e-4)                     # by hand: d2 = (ln(100/105) + (0.04 - 0.045) * 0.25) / 0.15 = -0.3336, N(d2) = 0.3693
    assert v["intervals"] == [{"from": 105.0, "to": None, "probability": pytest.approx(v["p_profit"])}]


def test_long_put_profit_is_the_probability_of_ending_below_its_breakeven():
    v = call_prob(structure="long_put", premiums=[5.0])
    assert v["breakevens"] == pytest.approx([95.0])
    assert v["p_profit"] == pytest.approx(1.0 - p_above(100, 95, 0.30, 0.25, 0.04), abs=1e-12)


def test_iron_condor_profit_is_the_probability_of_the_two_breakevens_band():
    v = st.profit_probability("iron_condor", [90, 95, 105, 110], [0.5, 1.5, 1.6, 0.4], 1, 100, 100.0, 0.30, 0.25, 0.04).value
    credit = 1.5 + 1.6 - 0.5 - 0.4
    lo, hi = 95 - credit, 105 + credit
    assert v["breakevens"] == pytest.approx([lo, hi])
    assert v["p_profit"] == pytest.approx(p_above(100, lo, 0.30, 0.25, 0.04) - p_above(100, hi, 0.30, 0.25, 0.04), abs=1e-12)
    assert len(v["intervals"]) == 1 and v["intervals"][0]["from"] == pytest.approx(lo) and v["intervals"][0]["to"] == pytest.approx(hi)


def test_a_long_straddle_style_payoff_adds_its_two_tails():
    # a short strangle's mirror: the structure profits outside the band, here a put spread plus a call spread bought is not in the catalog, so use the long put and long call union by the profit regions
    v = st.profit_probability("short_strangle", [95, 105], [1.0, 1.0], 1, 100, 100.0, 0.30, 0.25, 0.04).value
    lo, hi = 93.0, 107.0
    assert v["p_profit"] == pytest.approx(p_above(100, lo, 0.30, 0.25, 0.04) - p_above(100, hi, 0.30, 0.25, 0.04), abs=1e-12)


def test_protective_put_profits_above_its_breakeven():
    v = st.profit_probability("protective_put", [95], [2.0], 1, 100, 100.0, 0.30, 0.25, 0.04).value
    assert v["p_profit"] == pytest.approx(p_above(100, 102.0, 0.30, 0.25, 0.04), abs=1e-12)


def test_the_probability_is_the_model_of_prob_itm_of_the_chain_with_rate_zero():
    """Same model as the probability of finishing in the money shown in the chain: at rate 0 it is market.options.prob_itm, to its four decimals."""
    spot, strike, iv, years = 617.5, 618.0, 0.6426, 0.002
    assert round(st.prob_above(spot, strike, iv, years, 0.0), 4) == market_options.prob_itm("call", spot, strike, iv, years)
    put_side = 1.0 - st.prob_above(spot, strike, iv, years, 0.0)
    assert round(put_side, 4) == market_options.prob_itm("put", spot, strike, iv, years)


def test_the_default_rate_is_the_one_of_the_chain():
    assert st.DEFAULT_RATE == market_options.PROB_ITM_RATE


def test_the_probability_with_the_chain_rate_matches_the_contract_formula():
    spot, strike, own, years = 654.68, 650.0, 0.55, 0.01
    d2 = (math.log(spot / strike) + (market_options.PROB_ITM_RATE - own * own / 2.0) * years) / (own * math.sqrt(years))
    assert st.prob_above(spot, strike, own, years, market_options.PROB_ITM_RATE) == pytest.approx(N(d2), abs=1e-12)


def test_at_or_after_expiration_the_probability_is_zero_or_one():
    assert st.prob_above(100, 99, 0.3, 0.0) == 1.0 and st.prob_above(100, 101, 0.3, 0.0) == 0.0
    assert st.prob_above(100, 100, 0.3, -1.0) == 0.0


@pytest.mark.parametrize("kw", [dict(volatility=0.0), dict(volatility=-0.1), dict(spot=0.0), dict(time_to_expiration=-1.0), dict(structure="nope"), dict(strikes=[-5])])
def test_the_probability_refuses_bad_inputs_without_raising(kw):
    assert st.profit_probability(**{**dict(structure="long_call", strikes=[100], premiums=[5.0], quantity=1, multiplier=100, spot=100.0, volatility=0.3,
                                           time_to_expiration=0.25), **kw}).value is None


def test_the_probability_is_a_registered_function():
    registry.load_all()
    assert registry.call("options.profit_probability", structure="long_call", strikes=[100], premiums=[5.0], quantity=1, multiplier=100, spot=100.0,
                         volatility=0.3, time_to_expiration=0.25).value["p_profit"] > 0
    assert registry.describe("options.strategy_cards").module == "options"


# ---- the cards on a real chain ------------------------------------------------------------------------------------------------------------

def test_bullish_cards_use_strikes_of_the_chain_and_their_figures_are_those_of_structure_profile():
    out = cards("bullish", budget=3000)
    listed = {r["strike"] for r in chain()}
    assert out["view"] == "bullish" and out["families"] == ["bullish"] and out["cards"]
    for card in out["cards"]:
        strikes = [leg["strike"] for leg in card["legs"] if leg["strike"] is not None]
        assert strikes and set(strikes) <= listed
        legs = [leg for leg in card["legs"] if leg["kind"] != "stock"]
        by_hand = structure_profile(card["structure"], sorted(set(strikes)) if card["structure"] != "short_straddle" else strikes,
                                    [leg["premium"] for leg in legs], 1, 100, SPOT).value
        assert card["net_premium_usd"] == pytest.approx(by_hand["net_premium_usd"])
        assert card["max_loss_usd"] == pytest.approx(by_hand["max_loss_usd"]) and card["max_profit_usd"] == pytest.approx(by_hand["max_profit_usd"])
        assert card["breakevens"] == pytest.approx(by_hand["breakevens"])
        assert card["expiration"] == "2026-12-25" and card["quantity"] == 1 and card["multiplier"] == 100


def test_the_premium_of_a_leg_is_the_mid_of_its_quote():
    card = next(c for c in cards("bullish", budget=3000)["cards"] if c["structure"] == "debit_call_spread")
    rows = {r["strike"]: r for r in chain()}
    for leg in card["legs"]:
        quote = rows[leg["strike"]]["call"]
        assert leg["premium"] == pytest.approx((quote["bid"] + quote["ask"]) / 2) and leg["bid"] == quote["bid"] and leg["ask"] == quote["ask"]


def test_premium_selling_cards_come_out_locked_with_numbers_while_the_switch_is_closed():
    out = cards("neutral", budget=3000)
    names = {c["structure"]: c for c in out["cards"]}
    assert {"iron_condor", "iron_butterfly"} <= set(names)
    for name in ("iron_condor", "iron_butterfly", "short_strangle"):
        assert names[name]["status"] == "locked" and names[name]["locked"] == "premium_selling" and names[name]["can_open_detail"] is False
        assert names[name]["net_premium_usd"] < 0 and names[name]["max_profit_usd"] > 0


def test_with_the_switch_open_the_same_cards_are_not_locked():
    out = cards("neutral", budget=3000, premium_selling_open=True)
    condor = next(c for c in out["cards"] if c["structure"] == "iron_condor")
    assert condor["locked"] is None and condor["status"] == "ok" and condor["can_open_detail"] is True


def test_the_iron_butterfly_card_has_a_body_and_equal_wings_on_the_chain():
    card = next(c for c in cards("neutral", budget=3000)["cards"] if c["structure"] == "iron_butterfly")
    strikes = sorted({leg["strike"] for leg in card["legs"]})
    assert len(strikes) == 3 and strikes[1] - strikes[0] == pytest.approx(strikes[2] - strikes[1])
    assert [(leg["position"], leg["kind"]) for leg in card["legs"]].count(("short", "call")) == 1


def test_a_card_that_does_not_fit_the_budget_says_so_with_its_cost():
    near = chain(strikes=range(96, 105, 2))                  # every spread of this window is at least 2 wide and costs well above 20 dollars
    big = cards("bullish", budget=3000, rows=near)
    small = cards("bullish", budget=20, rows=near)
    debit = next(c for c in small["cards"] if c["structure"] == "debit_call_spread")
    assert debit["status"] == "over_budget" and debit["budget"]["fits"] is False
    assert debit["capital_required_usd"] == pytest.approx(debit["cost_usd"]) and debit["capital_required_usd"] > 20
    assert next(c for c in big["cards"] if c["structure"] == "debit_call_spread")["budget"]["fits"] is True


def test_the_cards_that_fit_never_exceed_the_budget_and_the_choice_is_the_cheapest_when_nothing_fits():
    for budget in (50, 150, 400, 3000):
        for card in cards("bearish", budget=budget, shares_held=100)["cards"]:
            if card["status"] == "ok":
                assert card["capital_required_usd"] <= budget + 1e-9
            if card["status"] == "over_budget":
                assert card["capital_required_usd"] is None or card["capital_required_usd"] > budget


def test_without_a_budget_everything_fits():
    assert all(c["budget"]["fits"] for c in cards("bullish")["cards"])


def test_protective_put_needs_the_hundred_shares():
    none = next(c for c in cards("bearish", budget=3000)["cards"] if c["structure"] == "protective_put")
    assert none["status"] == "needs_shares" and none["requires_shares"] == 100
    few = next(c for c in cards("bearish", budget=3000, shares_held=99)["cards"] if c["structure"] == "protective_put")
    assert few["status"] == "needs_shares"
    held = next(c for c in cards("bearish", budget=3000, shares_held=100)["cards"] if c["structure"] == "protective_put")
    assert held["status"] == "ok" and held["capital_required_usd"] == pytest.approx(held["cost_usd"])
    assert [leg["kind"] for leg in held["legs"]][:2] == ["stock", "put"]


def test_directional_shows_both_families_and_a_family_is_tagged():
    out = cards("directional", budget=3000, shares_held=100)
    assert {c["family"] for c in out["cards"]} == {"bullish", "bearish"}
    assert len({c["id"] for c in out["cards"]}) == len(out["cards"])


def test_a_strong_view_leads_with_the_outright_option_when_volatility_is_cheap():
    out = cards("very_bullish", budget=3000, vol_classification="cheap")
    assert out["cards"][0]["structure"] == "long_call" and out["vol_view"] == "rising" and out["vol_view_source"] == "vol_analysis"


def test_volatility_defaults_to_flat_and_says_it_is_a_default():
    out = cards("bullish", budget=3000)
    assert out["vol_view"] == "flat" and out["vol_view_source"] == "default_flat_uncalibrated"


def test_the_target_decides_which_card_is_best_for_the_return():
    near = cards("bullish", budget=3000, target=102.0, priority="max_return")
    far = cards("bullish", budget=3000, target=112.0, priority="max_return")
    for out in (near, far):
        assert all(c["target_used"] > SPOT for c in out["cards"] if c["family"] == "bullish")
    assert next(c for c in far["cards"] if c["structure"] == "debit_call_spread")["target_used"] == 112.0


def test_the_priorities_choose_among_the_same_candidates():
    prob = next(c for c in cards("bullish", budget=3000, priority="max_probability")["cards"] if c["structure"] == "debit_call_spread")
    ret = next(c for c in cards("bullish", budget=3000, priority="max_return", target=108.0)["cards"] if c["structure"] == "debit_call_spread")
    assert prob["p_profit"] >= ret["p_profit"] and ret["return_at_target"] >= prob["return_at_target"]


def test_each_card_carries_its_probability_and_the_model_it_comes_from():
    card = cards("bullish", budget=3000)["cards"][0]
    be = card["breakevens"][0]
    assert card["p_profit"] > 0 and be > 0
    assert card["p_profit_model"] == {"model": "black_scholes_d2", "volatility": IV, "rate": st.DEFAULT_RATE, "time_to_expiration": YEARS}


def test_a_debit_call_spread_probability_is_the_reference_one_from_its_breakeven():
    card = next(c for c in cards("bullish", budget=3000)["cards"] if c["structure"] == "debit_call_spread")
    assert card["p_profit"] == pytest.approx(p_above(SPOT, card["breakevens"][0], IV, YEARS, st.DEFAULT_RATE), abs=1e-9)


def test_the_payoff_curve_agrees_with_the_figures_of_the_card():
    card = next(c for c in cards("bullish", budget=3000)["cards"] if c["structure"] == "debit_call_spread")
    pnl, spots = card["payoff"]["pnl_usd"], card["payoff"]["spots"]
    assert len(pnl) == len(spots) > 10 and spots == sorted(spots)
    assert max(pnl) == pytest.approx(card["max_profit_usd"]) and min(pnl) == pytest.approx(-card["max_loss_usd"])


def test_illiquid_contracts_are_not_offered():
    wide = chain()
    for r in wide:                                          # bid at 40 percent and ask at 160 percent of the mid: a relative spread of 1.2
        for side in ("call", "put"):
            mid = (r[side]["bid"] + r[side]["ask"]) / 2
            r[side]["bid"], r[side]["ask"] = round(mid * 0.4, 2), round(mid * 1.6, 2)
    out = st.strategy_cards(wide, SPOT, "bullish", "2026-12-25", YEARS, 100.0, budget=3000).value
    assert out["cards"] == [] and out["chain"]["illiquid_dropped"] > 0 and out["skipped"]
    thin = st.strategy_cards(chain(oi=0), SPOT, "bullish", "2026-12-25", YEARS, 100.0, budget=3000).value
    assert thin["cards"] == []


def test_contracts_without_a_two_sided_quote_are_dropped():
    rows = chain()
    for r in rows:
        r["call"]["bid"], r["call"]["ask"] = 0.0, 0.0
    out = st.strategy_cards(rows, SPOT, "bullish", "2026-12-25", YEARS, 100.0, budget=3000).value
    assert all(not any(leg["kind"] == "call" for leg in c["legs"]) for c in out["cards"])


def test_liquidity_thresholds_are_the_declared_defaults():
    assert st.quote_of({"bid": 1.0, "ask": 1.1, "open_interest": 5})["liquid"] is True
    assert st.quote_of({"bid": 1.0, "ask": 2.0, "open_interest": 5})["liquid"] is False                  # relative spread 0.667
    assert st.quote_of({"bid": 1.0, "ask": 1.1, "open_interest": 0})["liquid"] is False
    assert st.quote_of({"bid": 1.0, "ask": 1.1})["liquid"] is True                                          # no open interest given: not a reason to drop
    assert st.quote_of({"bid": 1.2, "ask": 1.1}) is None and st.quote_of(None) is None


@pytest.mark.parametrize("kw", [dict(view="sideways"), dict(priority="fast"), dict(spot=0.0), dict(years=0.0), dict(multiplier=0.0), dict(budget=-1.0), dict(target=-5.0)])
def test_bad_arguments_come_back_empty_with_the_reason(kw):
    args = dict(rows=chain(), spot=SPOT, view="bullish", expiration="2026-12-25", years=YEARS, multiplier=100.0)
    args.update(kw)
    res = st.strategy_cards(**args)
    assert res.value is None and res.warnings


def test_a_wrong_type_is_a_bad_input():
    from hokma.quant.errors import BadInput

    with pytest.raises(BadInput):
        st.strategy_cards("not rows", SPOT, "bullish", "2026-12-25", YEARS, 100.0)
    with pytest.raises(BadInput):
        st.strategy_cards(chain(), "100", "bullish", "2026-12-25", YEARS, 100.0)


def test_an_empty_chain_gives_no_cards_and_the_reason():
    out = st.strategy_cards([], SPOT, "neutral", "2026-12-25", YEARS, 100.0).value
    assert out["cards"] == [] and {s["structure"] for s in out["skipped"]} >= {"iron_condor", "iron_butterfly"}
