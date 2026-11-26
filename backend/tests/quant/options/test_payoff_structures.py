"""Payoff and structures. Rows FT-036 and ECT-098."""
import math
from datetime import date, datetime, timezone
from decimal import Decimal

import numpy as np
import pytest

from hokma.contracts.identity import ContractSheet
from hokma.quant.errors import BadInput
from hokma.quant.options import _lib, structures

from .conftest import assert_empty, call, piece_ids


def sheet(multiplier: str = "100") -> ContractSheet:
    return ContractSheet("OPT:SPY:20261218:C:590", "OPT", "SPY", "SPY", Decimal(multiplier), None, None, date(2026, 12, 18), Decimal("590"), "C", "USD",
                         "test", datetime(2026, 11, 20, tzinfo=timezone.utc))


DEBIT_SPREAD = [{"position": "long", "kind": "call", "strike": 100.0, "premium": 5.0}, {"position": "short", "kind": "call", "strike": 110.0, "premium": 2.0}]
CONDOR = [{"position": "long", "kind": "put", "strike": 90.0, "premium": 0.5}, {"position": "short", "kind": "put", "strike": 95.0, "premium": 1.5},
          {"position": "short", "kind": "call", "strike": 105.0, "premium": 1.5}, {"position": "long", "kind": "call", "strike": 110.0, "premium": 0.5}]


def test_debit_spread_with_the_multiplier_of_the_sheet_FT_036():
    res = call("options.payoff", legs=DEBIT_SPREAD, multiplier=sheet().multiplier, spot=100.0).value
    # hand values: pay 5 - 2 = 3 per share, so 300 dollars for one contract of 100; the width of 10 gives a maximum profit of (10 - 3) * 100
    assert res["net_premium_usd"] == pytest.approx(300.0) and res["premium_type"] == "debit"
    assert res["max_loss_usd"] == pytest.approx(300.0) and res["max_profit_usd"] == pytest.approx(700.0)
    assert res["breakevens"] == [103.0]
    assert not res["unbounded_profit"] and not res["unbounded_loss"]
    # the multiplier comes from the sheet, not from a written 100: a sheet of 10 gives a tenth of the dollars
    small = call("options.payoff", legs=DEBIT_SPREAD, multiplier=sheet("10").multiplier, spot=100.0).value
    assert small["max_profit_usd"] == pytest.approx(70.0) and small["net_premium_usd"] == pytest.approx(30.0)
    # two contracts double it; a multiplier on one leg overrides the general one
    two = call("options.payoff", legs=[{**leg, "quantity": 2} for leg in DEBIT_SPREAD], multiplier=100, spot=100.0).value
    assert two["max_loss_usd"] == pytest.approx(600.0)
    # the grid: pnl at spots below, between and above the strikes
    grid = call("options.payoff", legs=DEBIT_SPREAD, multiplier=100, spots=[90.0, 100.0, 103.0, 105.0, 110.0, 130.0]).value
    assert grid["pnl_usd"] == pytest.approx([-300.0, -300.0, 0.0, 200.0, 700.0, 700.0])


def test_condor_and_other_shapes_FT_036(chain):
    res = call("options.payoff", legs=CONDOR, multiplier=100, spot=100.0).value
    # hand values: credit 1.5 + 1.5 - 0.5 - 0.5 = 2 per share; both wings are 5 wide, so the loss is (5 - 2) * 100
    assert res["premium_type"] == "credit" and res["net_premium_usd"] == pytest.approx(-200.0)
    assert res["max_profit_usd"] == pytest.approx(200.0) and res["max_loss_usd"] == pytest.approx(300.0)
    assert res["breakevens"] == pytest.approx([93.0, 107.0])
    # unbounded cases: a naked short call loses without limit; a long straddle gains without limit
    naked = call("options.payoff", legs=[{"position": "short", "kind": "call", "strike": 100.0, "premium": 3.0}], multiplier=100, spot=100.0).value
    assert naked["unbounded_loss"] and naked["max_loss_usd"] is None and naked["max_profit_usd"] == pytest.approx(300.0)
    straddle = [{"position": "long", "kind": "call", "strike": 100.0, "premium": 3.0}, {"position": "long", "kind": "put", "strike": 100.0, "premium": 3.0}]
    both = call("options.payoff", legs=straddle, multiplier=100, spot=100.0).value
    assert both["unbounded_profit"] and both["max_profit_usd"] is None and both["max_loss_usd"] == pytest.approx(600.0)
    assert both["breakevens"] == pytest.approx([94.0, 106.0])
    # shares: a covered call (100 shares bought at 100, call sold at 4) is capped at the strike and loses with the shares
    covered = call("options.payoff", legs=[{"instrument": "stock", "position": "long", "premium": 100.0, "quantity": 100},
                                         {"position": "short", "kind": "call", "strike": 105.0, "premium": 4.0}], multiplier=100, spot=100.0).value
    assert covered["max_profit_usd"] == pytest.approx(900.0) and covered["max_loss_usd"] == pytest.approx(9600.0) and covered["breakevens"] == pytest.approx([96.0])
    # parity with the library payoff of the same legs, per share
    spots = np.linspace(80, 120, 9)
    lib = _lib.ft_tree.get_strategy_payoff(spots, [{"position": "long", "strike_price": 100.0, "put_option": False, "premium": 5.0},
                                                   {"position": "short", "strike_price": 110.0, "put_option": False, "premium": 2.0}])
    mine = call("options.payoff", legs=DEBIT_SPREAD, multiplier=1, spots=spots.tolist()).value["pnl_usd"]
    assert mine == pytest.approx(list(lib), abs=1e-8)
    # a spread built from the recorded chain: bounded by its width
    rows = {(r["type"], r["strike"]): r for r in chain["contracts"] if r["expiration"] == "2027-01-15"}
    long_c, short_c = rows[("call", 590.0)], rows[("call", 610.0)]
    legs = [{"position": "long", "kind": "call", "strike": 590.0, "premium": long_c["ask"]}, {"position": "short", "kind": "call", "strike": 610.0, "premium": short_c["bid"]}]
    built = call("options.payoff", legs=legs, multiplier=chain["multiplier"], spot=chain["underlying_price"]).value
    debit = (long_c["ask"] - short_c["bid"]) * 100
    assert built["max_loss_usd"] == pytest.approx(debit, abs=1e-6) and built["max_profit_usd"] == pytest.approx(2000 - debit, abs=1e-6)
    assert built["breakevens"] == pytest.approx([590.0 + debit / 100], abs=1e-6)


def test_payoff_bad_input_FT_036():
    assert_empty(call("options.payoff", legs=DEBIT_SPREAD, spot=100.0), "multiplier")
    assert_empty(call("options.payoff", legs=DEBIT_SPREAD, multiplier=0.0, spot=100.0), "positive")
    assert_empty(call("options.payoff", legs=DEBIT_SPREAD, multiplier=float("nan"), spot=100.0))
    assert_empty(call("options.payoff", legs=[], multiplier=100, spot=100.0), "at least one")
    assert call("options.payoff", legs=DEBIT_SPREAD, multiplier=100).value["max_loss_usd"] == pytest.approx(300.0)   # the strikes give the grid
    assert_empty(call("options.payoff", legs=[{"instrument": "stock", "position": "long", "premium": 100.0, "quantity": 10}], multiplier=100), "spots")
    assert_empty(call("options.payoff", legs=DEBIT_SPREAD, multiplier=100, spots=[float("nan")]))
    assert_empty(call("options.payoff", legs=[{"position": "up", "kind": "call", "strike": 100.0}], multiplier=100, spot=100.0), "position")
    assert_empty(call("options.payoff", legs=[{"position": "long", "kind": "warrant", "strike": 100.0}], multiplier=100, spot=100.0), "kind")
    assert_empty(call("options.payoff", legs=[{"position": "long", "kind": "call", "strike": -1.0}], multiplier=100, spot=100.0))
    assert_empty(call("options.payoff", legs=[{"position": "long", "kind": "call", "strike": 100.0, "quantity": 0}], multiplier=100, spot=100.0), "quantity")
    with pytest.raises(BadInput):
        call("options.payoff", legs="long call", multiplier=100, spot=100.0)
    with pytest.raises(BadInput):
        call("options.payoff", legs=[[1, 2]], multiplier=100, spot=100.0)
    with pytest.raises(BadInput):
        call("options.payoff", legs=DEBIT_SPREAD, multiplier="100", spot=100.0)
    with pytest.raises(BadInput):
        call("options.payoff", legs=DEBIT_SPREAD, multiplier=100, spot=100.0, grid_points=5000)
    with pytest.raises(BadInput):
        call("options.payoff", legs=DEBIT_SPREAD * 11, multiplier=100, spot=100.0)
    assert "FT-036" in piece_ids("options.payoff")


def test_matrix_offers_only_purchases_and_debit_spreads_while_the_switch_is_closed_ECT_098():
    names = lambda r: [x["structure"] for x in r.value["offered"]]  # noqa: E731
    closed = call("options.structure_matrix", direction="bullish", vol_view="falling")
    # hand value: the matrix of the skill lists short puts and credit spreads for a bullish view with falling volatility: they are blocked
    assert names(closed) == ["debit_call_spread"]
    assert {x["structure"] for x in closed.value["blocked"]} == {"credit_put_spread", "short_put", "cash_secured_put", "covered_call"}
    assert all(x["reason"] == "premium_selling_closed" and x["premium_class"] == "credit" for x in closed.value["blocked"])
    opened = call("options.structure_matrix", direction="bullish", vol_view="falling", premium_selling_open=True)
    assert names(opened) == ["debit_call_spread", "credit_put_spread", "short_put", "cash_secured_put", "covered_call"] and opened.value["blocked"] == []
    for (direction, vol), expected in structures.MATRIX.items():
        for switch in (False, True):
            res = call("options.structure_matrix", direction=direction, vol_view=vol, premium_selling_open=switch).value
            offered = {x["structure"] for x in res["offered"]}
            if not switch:
                assert all(structures.STRUCTURES[n]["class"] == "debit" for n in offered), (direction, vol)
            assert offered | {x["structure"] for x in res["blocked"]} == set(expected)
    assert call("options.structure_matrix", direction="bullish", vol_view="rising").value["offered"][0]["structure"] == "long_call"
    assert call("options.structure_matrix", direction="bearish", vol_view="rising").value["offered"][0]["structure"] == "long_put"
    # a neutral view with rising volatility has no structure at all; a neutral view with the switch closed has none either
    assert call("options.structure_matrix", direction="neutral", vol_view="rising").value["offered"] == []
    flat = call("options.structure_matrix", direction="neutral", vol_view="flat")
    assert flat.value["offered"] == [] and "stay out" in flat.value["note"]
    assert_empty(call("options.structure_matrix", direction="sideways", vol_view="flat"), "direction")
    assert_empty(call("options.structure_matrix", direction="bullish", vol_view="spiky"), "vol_view")
    with pytest.raises(BadInput):
        call("options.structure_matrix", direction=1, vol_view="flat")
    with pytest.raises(BadInput):
        call("options.structure_matrix", direction="bullish", vol_view="flat", premium_selling_open="yes")
    assert "ECT-098" in piece_ids("options.structure_matrix")


def test_classification_credit_or_debit_for_the_switch_ECT_098():
    classify = lambda legs: call("options.classify_structure", legs=legs).value  # noqa: E731
    assert classify([{"position": "long", "kind": "call", "premium": 5.0}])["premium_selling"] is False
    spread = classify([{"position": "long", "kind": "call", "premium": 5.0, "expiration": "2026-12-18"},
                       {"position": "short", "kind": "call", "premium": 2.0, "expiration": "2026-12-18"}])
    assert spread["premium_type"] == "debit" and spread["premium_selling"] is False and spread["uncovered_short_legs"] == []
    condor = classify(CONDOR)
    assert condor["premium_type"] == "credit" and condor["premium_selling"] is True
    naked = classify([{"position": "short", "kind": "put", "premium": 1.0}])
    assert naked["premium_selling"] is True and len(naked["uncovered_short_legs"]) == 1
    # a short leg covered by a long leg of the same kind and quantity with a later expiry is not uncovered, even if the net is a debit
    calendar = classify([{"position": "long", "kind": "call", "premium": 8.0, "expiration": "2027-01-15", "quantity": 1},
                         {"position": "short", "kind": "call", "premium": 3.0, "expiration": "2026-12-18", "quantity": 1}])
    assert calendar["premium_selling"] is False and calendar["premium_type"] == "debit"
    # an earlier long leg does not cover a later short leg; a smaller long quantity does not cover a larger short quantity; another kind does not cover
    assert classify([{"position": "long", "kind": "call", "premium": 8.0, "expiration": "2026-12-18"},
                     {"position": "short", "kind": "call", "premium": 3.0, "expiration": "2027-01-15"}])["premium_selling"] is True
    assert classify([{"position": "long", "kind": "call", "premium": 8.0, "quantity": 1}, {"position": "short", "kind": "call", "premium": 3.0, "quantity": 2}])["premium_selling"] is True
    assert classify([{"position": "long", "kind": "put", "premium": 8.0}, {"position": "short", "kind": "call", "premium": 3.0}])["premium_selling"] is True
    # a covered call sells premium (its leg is covered by shares, not by an option) and so does a net credit with no short leg left uncovered
    assert classify([{"instrument": "stock", "position": "long", "quantity": 100}, {"position": "short", "kind": "call", "premium": 3.0}])["premium_selling"] is True
    assert_empty(call("options.classify_structure", legs=[]), "at least one")
    assert_empty(call("options.classify_structure", legs=[{"position": "sideways", "kind": "call", "premium": 1.0}]), "position")
    assert_empty(call("options.classify_structure", legs=[{"position": "long", "kind": "call", "premium": float("nan")}]))
    assert_empty(call("options.classify_structure", legs=[{"position": "long", "kind": "call", "premium": 1.0, "quantity": 0}]), "quantity")
    with pytest.raises(BadInput):
        call("options.classify_structure", legs="long call")
    with pytest.raises(BadInput):
        call("options.classify_structure", legs=["long"])


# structure: (strikes, premiums per option leg, maximum profit, maximum loss, breakevens, premium class), one contract of 100
STRIKES_PREMIUMS = {
    "long_call": ([100.0], [3.0], None, 300.0, [103.0], "debit"),
    "long_put": ([100.0], [3.0], 9700.0, 300.0, [97.0], "debit"),
    "debit_call_spread": ([100.0, 110.0], [5.0, 2.0], 700.0, 300.0, [103.0], "debit"),
    "debit_put_spread": ([100.0, 110.0], [6.0, 3.0], 700.0, 300.0, [107.0], "debit"),
    "short_put": ([100.0], [3.0], 300.0, 9700.0, [97.0], "credit"),
    "short_call": ([100.0], [3.0], 300.0, None, [103.0], "credit"),
    "credit_call_spread": ([100.0, 110.0], [5.0, 2.0], 300.0, 700.0, [103.0], "credit"),
    "credit_put_spread": ([100.0, 110.0], [6.0, 3.0], 300.0, 700.0, [107.0], "credit"),
    "iron_condor": ([90.0, 95.0, 105.0, 110.0], [0.5, 1.5, 1.5, 0.5], 200.0, 300.0, [93.0, 107.0], "credit"),
    "short_straddle": ([100.0], [3.0, 3.0], 600.0, None, [94.0, 106.0], "credit"),
    "short_strangle": ([95.0, 105.0], [2.0, 2.0], 400.0, None, [91.0, 109.0], "credit"),
    "cash_secured_put": ([100.0], [3.0], 300.0, 9700.0, [97.0], "credit"),
}


@pytest.mark.parametrize("structure", sorted(STRIKES_PREMIUMS))
def test_profile_of_every_structure_ECT_098(structure):
    strikes, premiums, max_profit, max_loss, breakevens, klass = STRIKES_PREMIUMS[structure]
    res = call("options.structure_profile", structure=structure, strikes=strikes, premiums=premiums, quantity=1, multiplier=100, spot=100.0)
    value = res.value
    assert value["premium_type"] == klass == value["expected_class"]
    for key, expected in (("max_profit_usd", max_profit), ("max_loss_usd", max_loss)):
        if expected is None:
            assert value[key] is None, key
        else:
            assert value[key] == pytest.approx(expected), key
    assert value["unbounded_profit"] is (max_profit is None) and value["unbounded_loss"] is (max_loss is None)
    assert value["breakevens"] == pytest.approx(breakevens)
    assert not res.warnings


def test_covered_call_and_greeks_of_a_structure_ECT_098(chain):
    res = call("options.structure_profile", structure="covered_call", strikes=[105.0], premiums=[4.0], quantity=1, multiplier=100, spot=100.0).value
    assert res["premium_type"] == "credit" and res["max_profit_usd"] == pytest.approx(900.0) and res["max_loss_usd"] == pytest.approx(9600.0)
    assert [leg["instrument"] for leg in res["legs"]] == ["stock", "option"]
    # greeks of the structure by the module's own functions: a bull call spread has positive delta and a short iron condor negative gamma
    spread = call("options.structure_profile", structure="debit_call_spread", strikes=[590.0, 610.0], premiums=[10.0, 4.0], quantity=2, multiplier=100,
                  spot=590.0, rate=chain["rate"], volatility=0.16, time_to_expiration=0.1, dividend_yield=chain["dividend_yield"]).value
    assert spread["greeks"]["delta_shares"] > 0
    condor = call("options.structure_profile", structure="iron_condor", strikes=[560.0, 570.0, 610.0, 620.0], premiums=[0.5, 1.5, 1.5, 0.5], quantity=1,
                  multiplier=100, spot=590.0, rate=chain["rate"], volatility=0.16, time_to_expiration=0.1).value
    assert condor["greeks"]["gamma_per_dollar"] < 0 and condor["greeks"]["vega_usd_per_point"] < 0 and condor["greeks"]["theta_usd_per_day"] > 0


def numeric_mean(legs, spot, rate, vol, years):
    """The mean payoff by brute force: the integral of the dollar payoff against the lognormal density, on a fine grid of log prices."""
    import numpy as np

    from hokma.quant.options import payoff as pay

    mu, sd = math.log(spot) + (rate - vol * vol / 2) * years, vol * math.sqrt(years)
    z = np.linspace(-9.0, 9.0, 600_001)
    prices = np.exp(mu + sd * z)
    weights = np.exp(-z * z / 2) / math.sqrt(2 * math.pi)
    return float(np.trapezoid(pay.pnl_at(legs, prices) * weights, z))


EV_CASES = {
    "iron_condor": ([560.0, 570.0, 610.0, 620.0], [0.5, 1.5, 1.5, 0.5]),
    "iron_butterfly": ([95.0, 100.0, 105.0], [1.0, 3.0, 3.2, 1.1]),
    "credit_put_spread": ([95.0, 100.0], [1.2, 3.1]),
    "cash_secured_put": ([100.0], [3.0]),
    "debit_call_spread": ([100.0, 110.0], [5.0, 2.0]),
    "covered_call": ([105.0], [4.0]),
}


@pytest.mark.parametrize("structure", sorted(EV_CASES))
def test_expected_value_is_the_exact_lognormal_mean_of_the_payoff_ECT_098(structure):
    from hokma.quant.options.structures import build_legs

    strikes, premiums = EV_CASES[structure]
    spot = 590.0 if structure == "iron_condor" else 100.0
    res = call("options.structure_profile", structure=structure, strikes=strikes, premiums=premiums, quantity=2, multiplier=100, spot=spot,
               rate=0.04, volatility=0.25, time_to_expiration=0.1)
    legs = build_legs(structure, strikes, premiums, 2, 100, spot)
    assert res.value["expected_value_usd"] == pytest.approx(numeric_mean(legs, spot, 0.04, 0.25, 0.1), abs=1e-3)


def test_expected_value_of_fairly_priced_legs_is_zero_not_the_two_point_average_ECT_098():
    # at rate 0 the Black-Scholes price of each leg is its mean payoff, so any structure built from them has a mean of exactly zero. The old figure
    # p * max_profit - (1 - p) * max_loss gave +11 dollars for the condor of Vera's check.
    from statistics import NormalDist

    n = NormalDist().cdf
    spot, vol, years = 590.0, 0.16, 0.1
    sd = vol * math.sqrt(years)

    def fair(kind, k):
        d1 = (math.log(spot / k) + 0.5 * sd * sd) / sd
        d2 = d1 - sd
        return spot * n(d1) - k * n(d2) if kind == "call" else k * n(-d2) - spot * n(-d1)

    strikes = [560.0, 570.0, 610.0, 620.0]
    premiums = [fair("put", 560.0), fair("put", 570.0), fair("call", 610.0), fair("call", 620.0)]
    res = call("options.structure_profile", structure="iron_condor", strikes=strikes, premiums=premiums, quantity=1, multiplier=100, spot=spot,
               rate=0.0, volatility=vol, time_to_expiration=years).value
    assert res["expected_value_usd"] == pytest.approx(0.0, abs=1e-6)


def test_expected_value_needs_spot_rate_volatility_and_time_and_is_omitted_without_them_ECT_098():
    base = dict(structure="iron_condor", strikes=[560.0, 570.0, 610.0, 620.0], premiums=[0.5, 1.5, 1.5, 0.5], quantity=1, multiplier=100)
    assert "expected_value_usd" not in call("options.structure_profile", **base, spot=590.0).value
    assert "expected_value_usd" not in call("options.structure_profile", **base, spot=590.0, rate=0.04, volatility=0.2).value
    assert "expected_value_usd" in call("options.structure_profile", **base, spot=590.0, rate=0.04, volatility=0.2, time_to_expiration=0.1).value


def test_negative_expected_value_warns_and_strike_distance_is_reported_ECT_098():
    res = call("options.structure_profile", structure="short_strangle", strikes=[95.0, 105.0], premiums=[0.2, 0.2], quantity=1, multiplier=100,
               spot=100.0, rate=0.04, volatility=0.4, time_to_expiration=0.25)
    assert res.value["expected_value_usd"] < 0 and any("expected value is" in w for w in res.warnings)
    # the strikes of that condor were called "beyond 1 sigma" at 4.4 percent over a 5.6 percent move: that is 0.79 sigma, and it is reported as such
    sigma = call("options.structure_profile", structure="short_strangle", strikes=[100.0 * math.exp(-0.044), 100.0 * math.exp(0.044)], premiums=[1.0, 1.0],
                 quantity=1, multiplier=100, spot=100.0, volatility=0.28, time_to_expiration=0.04).value
    assert [x["sigma"] for x in sigma["short_strike_distance_sigma"]] == pytest.approx([0.044 / 0.056] * 2, abs=1e-12)
    assert sigma["short_strike_distance_sigma"][0]["sigma"] < 1.0
    # the premiums are checked against the class of the structure
    odd = call("options.structure_profile", structure="debit_call_spread", strikes=[100.0, 110.0], premiums=[2.0, 5.0], quantity=1, multiplier=100)
    assert odd.value["premium_type"] == "credit" and any("normally a debit" in w for w in odd.warnings)


def test_profile_bad_input_ECT_098():
    base = {"structure": "iron_condor", "strikes": [90.0, 95.0, 105.0, 110.0], "premiums": [0.5, 1.5, 1.5, 0.5], "quantity": 1, "multiplier": 100}
    assert_empty(call("options.structure_profile", **{**base, "structure": "butterfly"}), "structure")
    assert_empty(call("options.structure_profile", **{**base, "strikes": [90.0, 95.0, 105.0]}), "needs 4 strike")
    assert_empty(call("options.structure_profile", **{**base, "strikes": [90.0, 105.0, 95.0, 110.0]}), "increasing")
    assert_empty(call("options.structure_profile", **{**base, "premiums": [0.5, 1.5]}), "premium")
    assert_empty(call("options.structure_profile", **{**base, "premiums": [0.5, float("nan"), 1.5, 0.5]}))
    assert_empty(call("options.structure_profile", **{**base, "multiplier": 0}), "positive")
    assert_empty(call("options.structure_profile", **{**base, "quantity": float("nan")}))
    assert_empty(call("options.structure_profile", structure="covered_call", strikes=[105.0], premiums=[4.0], quantity=1, multiplier=100), "spot")
    with pytest.raises(BadInput):
        call("options.structure_profile", **{**base, "strikes": "90,95"})
    with pytest.raises(BadInput):
        call("options.structure_profile", **{**base, "multiplier": "100"})
    assert "ECT-098" in piece_ids("options.structure_profile")


def test_management_plan_written_before_entry_ECT_098():
    res = call("options.management_plan", structure="iron_condor", net_premium_usd=-200.0, max_profit_usd=200.0, max_loss_usd=800.0,
               days_to_expiration=30.0, equity_usd=100_000.0)
    v = res.value
    # hand values: close at 50 percent of the 200 dollar profit; exit when the loss is twice the 200 dollar credit; size by the 800 dollar loss
    # against 2 percent of 100,000 (2,000 dollars): two contracts
    assert v["profit_target_usd"] == pytest.approx(100.0) and v["stop_loss_usd"] == pytest.approx(400.0)
    assert v["close_at_or_below_dte"] == 7.0 and v["must_close_now"] is False
    assert v["roll_rule"] == {"roll_threatened_side_when_short_abs_delta_above": 0.30}
    assert v["risk_budget_usd"] == pytest.approx(2000.0) and v["max_contracts"] == 2 and v["size_ok"]
    assert v["short_vega_limit_usd_per_point"] == pytest.approx(500.0)               # 0.5 percent of equity per point
    assert set(v["rules_source"].values()) == {"default_uncalibrated"}
    assert any("not backtested" in w for w in res.warnings)
    # the desk's own thresholds replace the defaults and are marked as the desk's
    own = call("options.management_plan", structure="iron_condor", net_premium_usd=-200.0, max_profit_usd=200.0, max_loss_usd=800.0,
               days_to_expiration=5.0, equity_usd=100_000.0, risk_pct=0.01, profit_target=0.25, close_dte=10.0, stop_multiple=1.5, roll_delta=0.25).value
    assert own["profit_target_usd"] == pytest.approx(50.0) and own["stop_loss_usd"] == pytest.approx(300.0) and own["max_contracts"] == 1
    assert own["must_close_now"] is True and own["rules_source"]["risk_pct"] == "desk" and own["rules_source"]["debit_stop"] == "default_uncalibrated"
    # a debit structure: half of the premium paid is the stop, no roll rule, no short vega ceiling
    debit = call("options.management_plan", structure="debit_call_spread", net_premium_usd=300.0, max_profit_usd=700.0, max_loss_usd=300.0,
                 days_to_expiration=20.0, equity_usd=10_000.0).value
    assert debit["stop_loss_usd"] == pytest.approx(150.0) and debit["roll_rule"] is None and debit["short_vega_limit_usd_per_point"] is None
    assert debit["max_contracts"] == 0 and debit["size_ok"] is False                  # 200 dollars of budget against a 300 dollar loss
    assert_empty(call("options.management_plan", structure="iron_condor", net_premium_usd=-200.0, max_profit_usd=200.0, max_loss_usd=800.0,
                      days_to_expiration=30.0, equity_usd=100_000.0, risk_pct=0.06), "risk_pct")
    assert_empty(call("options.management_plan", structure="iron_condor", net_premium_usd=-200.0, max_profit_usd=200.0, max_loss_usd=0.0,
                      days_to_expiration=30.0, equity_usd=100_000.0), "positive")
    assert_empty(call("options.management_plan", structure="iron_condor", net_premium_usd=float("nan"), max_profit_usd=200.0, max_loss_usd=800.0,
                      days_to_expiration=30.0, equity_usd=100_000.0))
    assert_empty(call("options.management_plan", structure="butterfly", net_premium_usd=-200.0, max_profit_usd=200.0, max_loss_usd=800.0,
                      days_to_expiration=30.0, equity_usd=100_000.0), "structure")
    assert_empty(call("options.management_plan", structure="iron_condor", net_premium_usd=-200.0, max_profit_usd=200.0, max_loss_usd=800.0,
                      days_to_expiration=30.0, equity_usd=100_000.0, roll_delta=1.5), "roll_delta")
    with pytest.raises(BadInput):
        call("options.management_plan", structure="iron_condor", net_premium_usd="-200", max_profit_usd=200.0, max_loss_usd=800.0, days_to_expiration=30.0, equity_usd=1.0)
    assert "ECT-098" in piece_ids("options.management_plan")
