"""Binomial trees. Rows ECT-045 and FT-035, with the figures that the source skill got wrong written correctly."""
import math

import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import _lib, trees

from .conftest import assert_empty, call, piece_ids

# The three step example of the skill: S=100, K=100, r=5 percent, sigma=30 percent, T=0.75 year.
EX = {"spot": 100.0, "strike": 100.0, "rate": 0.05, "volatility": 0.30, "time_to_expiration": 0.75}


def test_three_step_example_with_the_corrected_figures_ECT_045():
    eu_call = call("options.binomial_tree", **EX, kind="call", n_steps=3, method="crr", style="european", show_nodes=True).value
    # corrected figures: the risk-neutral probability is 0.5043 (the skill said 0.5073)
    assert eu_call["p"] == pytest.approx(0.5043, abs=5e-5)
    # the top node at t=0.5 is worth 36.23 (the skill said 36.74), and the tree price is 12.92 (the skill said 13.18)
    assert eu_call["node_values"][2][-1] == pytest.approx(36.23, abs=5e-3)
    assert eu_call["price"] == pytest.approx(12.92, abs=5e-3)
    # the Black-Scholes value is 12.09 (the skill said 13.15): three steps are 7 percent off, not close
    assert eu_call["bsm_price"] == pytest.approx(12.09, abs=5e-3)
    assert (eu_call["price"] - eu_call["bsm_price"]) / eu_call["bsm_price"] == pytest.approx(0.068, abs=0.002)
    # put: European 9.24, American 9.54 and a premium of 0.30 (the skill said 0.35)
    eu_put = call("options.binomial_tree", **EX, kind="put", n_steps=3, style="european").value
    am_put = call("options.binomial_tree", **EX, kind="put", n_steps=3, style="american", show_nodes=True).value
    assert eu_put["price"] == pytest.approx(9.24, abs=5e-3)
    assert am_put["price"] == pytest.approx(9.54, abs=5e-3)
    assert am_put["early_exercise_premium"] == pytest.approx(0.30, abs=5e-3)
    assert am_put["european_tree"] == pytest.approx(eu_put["price"], abs=1e-12)
    # parity of the tree prices: C - P = S - K * (the discounted strike of the tree) is 3.68 = 100 - 96.32 with the tree's own values
    assert eu_call["price"] - eu_put["price"] == pytest.approx(3.68, abs=5e-3)
    assert 100.0 - 100.0 * math.exp(-0.05 * 0.75) == pytest.approx(3.68, abs=5e-3)
    # the node S=100 at t=0.5 has a continuation value of 6.82 (the skill said 6.99): the American value there is the larger of it and the exercise value
    middle = am_put["node_prices"][2][1]
    assert middle == pytest.approx(100.0, abs=1e-9)
    disc, p = math.exp(-0.05 * 0.25), am_put["p"]
    cont = disc * (p * am_put["node_values"][3][2] + (1 - p) * am_put["node_values"][3][1])
    assert cont == pytest.approx(6.82, abs=5e-3)
    assert "ECT-045" in piece_ids("options.binomial_tree") and "FT-035" in piece_ids("options.binomial_tree")


def test_crr_matches_the_library_tree_FT_035(atm):
    for kind, style in (("call", "european"), ("put", "european"), ("put", "american"), ("call", "american")):
        res = call("options.binomial_tree", spot=100.0, strike=105.0, rate=0.04, volatility=0.30, time_to_expiration=0.5, dividend_yield=0.012,
                   kind=kind, n_steps=40, method="crr", style=style)
        origin = _lib.ft_tree.get_option_payoffs(100.0, 105.0, 0.5, 40, 0.04, 0.30, 0.012, kind == "put", style == "american").iloc[0, 0]
        assert res.value["price"] == pytest.approx(origin, abs=1e-8), (kind, style)
    # hand value: the tree martingale property holds for every parametrization except the Jarrow-Rudd approximation
    n, t = 60, 0.5
    for method in ("crr", "tian", "leisen_reimer"):
        u, d, p = trees.tree_parameters(method, 100.0, 105.0, 0.04, 0.30, t, 0.012, n + (n % 2 == 0 and method == "leisen_reimer"))
        dt = t / (n + (n % 2 == 0 and method == "leisen_reimer"))
        assert p * u + (1 - p) * d == pytest.approx(math.exp((0.04 - 0.012) * dt), abs=1e-12), method
    args = {k: atm[k] for k in ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield")}
    chain_tree = call("options.binomial_tree", **args, kind="call", n_steps=800, method="leisen_reimer", style="european")
    assert chain_tree.value["price"] == pytest.approx(atm["row"]["mid"], abs=2e-3)


def test_parametrizations_and_convergence_ECT_045():
    base = {"spot": 100.0, "strike": 100.0, "rate": 0.05, "volatility": 0.30, "time_to_expiration": 0.75, "dividend_yield": 0.01}
    bsm = c.bs_price(100, 100, 0.05, 0.30, 0.75, 0.01, True)
    errors = {}
    for method in trees.METHODS:
        res = call("options.binomial_tree", **base, kind="put", n_steps=501, method=method, style="european").value
        errors[method] = abs(res["price"] - bsm)
        assert res["bsm_price"] == pytest.approx(bsm, abs=1e-12)
        assert res["european_error_vs_bsm"] == pytest.approx(res["price"] - bsm, abs=1e-12)
        assert errors[method] < 6e-3, method
    assert errors["leisen_reimer"] < 1e-4 < errors["crr"], errors            # Leisen-Reimer converges fastest
    jr = trees.tree_parameters("jarrow_rudd", 100, 100, 0.05, 0.30, 0.75, 0.01, 100)
    assert jr[2] == 0.5 and jr[0] * jr[1] == pytest.approx(math.exp(2 * (0.05 - 0.01 - 0.5 * 0.09) * 0.0075), abs=1e-12)
    table = call("options.tree_convergence", **base, steps=[10, 50, 250, 500], kind="call", method="crr", style="european").value
    errs = [abs(r["error"]) for r in table["rows"]]
    assert errs[0] > errs[-1] and errs[-1] < 1e-2
    assert table["rows"][1]["price"] == pytest.approx(call("options.binomial_tree", **base, kind="call", n_steps=50, style="european").value["price"], abs=1e-12)
    even = call("options.binomial_tree", **base, kind="call", n_steps=100, method="leisen_reimer", style="european")
    assert even.value is not None and any("odd" in w for w in even.warnings)
    assert even.params["n_steps"] == 101
    assert_empty(call("options.tree_convergence", **base, steps=[], kind="call"))
    assert_empty(call("options.tree_convergence", **base, steps=[10], kind="call", method="binomial"), "method")
    with pytest.raises(BadInput):
        call("options.tree_convergence", **base, steps=list(range(10, 31)), kind="call")
    with pytest.raises(BadInput):
        call("options.tree_convergence", **base, steps=[10, 5001], kind="call")
    assert "ECT-045" in piece_ids("options.tree_convergence")


def test_exercise_boundary_grows_toward_the_strike_ECT_045():
    res = call("options.binomial_tree", **EX, kind="put", n_steps=200, style="american").value
    points = [b for b in res["exercise_boundary"] if b["boundary"] is not None]
    assert len(points) > 20
    # the exercise boundary of a put rises toward the strike as the expiry gets closer (the skill said it falls)
    early = sum(b["boundary"] for b in points[:10]) / 10
    late = sum(b["boundary"] for b in points[-10:]) / 10
    assert late > early and late < 100.0 and points[-1]["boundary"] > 0.9 * 100.0
    assert points[0]["time"] < points[-1]["time"] and points[-1]["time"] < 0.75
    # a European option has no boundary; a call without dividends is never exercised early
    assert call("options.binomial_tree", **EX, kind="put", n_steps=20, style="european").value["exercise_boundary"] == []
    nodiv = call("options.binomial_tree", **EX, kind="call", n_steps=100, style="american").value
    assert nodiv["early_exercise_premium"] == pytest.approx(0.0, abs=1e-9)
    assert all(b["boundary"] is None for b in nodiv["exercise_boundary"])


def test_discrete_dividends_ECT_045():
    base = {"spot": 100.0, "strike": 95.0, "rate": 0.05, "volatility": 0.30, "time_to_expiration": 0.5}
    divs = [{"time": 0.25, "amount": 2.0}]
    res = call("options.binomial_tree", **base, kind="call", n_steps=800, method="leisen_reimer", style="european", dividends=divs).value
    # hand value: the European call with one cash dividend is Black-Scholes on spot minus the present value of the dividend
    pv = 2.0 * math.exp(-0.05 * 0.25)
    assert res["pv_dividends"] == pytest.approx(pv, abs=1e-12)
    assert res["price"] == pytest.approx(c.bs_price(100.0 - pv, 95.0, 0.05, 0.30, 0.5, 0.0, False), abs=2e-3)
    # an American call is worth more than the European one when a dividend falls before expiry, and exactly the same without it
    am = call("options.binomial_tree", **base, kind="call", n_steps=400, style="american", dividends=[{"time": 0.499, "amount": 4.0}]).value
    assert am["early_exercise_premium"] > 0.01
    assert any(b["boundary"] is not None for b in am["exercise_boundary"])
    same = call("options.binomial_tree", **base, kind="call", n_steps=200, style="american", dividends=[]).value
    assert same["early_exercise_premium"] == pytest.approx(0.0, abs=1e-9)
    assert any("escrowed" in w for w in call("options.binomial_tree", **base, kind="call", n_steps=20, dividends=divs).warnings)
    assert_empty(call("options.binomial_tree", **base, kind="call", n_steps=20, dividends=[{"time": 0.7, "amount": 1.0}]), "dividends[0]")
    assert_empty(call("options.binomial_tree", **base, kind="call", n_steps=20, dividends=[{"time": 0.1, "amount": 500.0}]), "dividends")
    with pytest.raises(BadInput):
        call("options.binomial_tree", **base, kind="call", n_steps=20, dividends="2.0 at 0.25")
    with pytest.raises(BadInput):
        call("options.binomial_tree", **base, kind="call", n_steps=20, dividends=[{"time": "0.2", "amount": 1.0}])


def test_bad_input_and_limits_FT_035():
    base = {**EX, "kind": "call"}
    assert_empty(call("options.binomial_tree", **{**base, "volatility": float("nan")}, n_steps=10))
    assert_empty(call("options.binomial_tree", **{**base, "volatility": 0.0}, n_steps=10))
    assert_empty(call("options.binomial_tree", **{**base, "spot": -1.0}, n_steps=10))
    assert_empty(call("options.binomial_tree", **base, n_steps=10, method="trinomial"), "method")
    assert_empty(call("options.binomial_tree", **base, n_steps=10, style="bermudan"), "style")
    assert_empty(call("options.binomial_tree", **{**base, "kind": "x"}, n_steps=10), "kind")
    assert_empty(call("options.binomial_tree", **base, n_steps=20, show_nodes=True), "show_nodes")
    # a volatility too low for the step breaks the probability: None with the reason, not an exception
    assert_empty(call("options.binomial_tree", spot=100.0, strike=100.0, rate=0.5, volatility=0.001, time_to_expiration=1.0, n_steps=5), "probability")
    with pytest.raises(BadInput):
        call("options.binomial_tree", **base, n_steps=5001)
    with pytest.raises(BadInput):
        call("options.binomial_tree", **base, n_steps=10.5)
    with pytest.raises(BadInput):
        call("options.binomial_tree", **base, n_steps=0)
    with pytest.raises(BadInput):
        call("options.binomial_tree", **{**base, "strike": "100"}, n_steps=10)
    assert registry.describe("options.binomial_tree").inputs["n_steps"]["default"] == 200


def test_early_assignment_rule_before_an_ex_dividend_date_ECT_045():
    # threshold = K * (1 - exp(-r * tau)) = 95 * (1 - exp(-0.05 * 0.25)) = 1.1801...
    low = call("options.early_assignment_risk", spot=100.0, strike=95.0, rate=0.05, dividend=1.0, years_to_next_payment=0.25, call_price=5.6)
    assert low.value["threshold"] == pytest.approx(95.0 * (1 - math.exp(-0.0125)), abs=1e-12)
    assert low.value["necessary_condition"] is False and low.value["at_risk"] is False        # dividend below the threshold: no reason to exercise
    # dividend above the threshold and little extrinsic value left: at risk
    risky = call("options.early_assignment_risk", spot=100.0, strike=95.0, rate=0.05, dividend=2.0, years_to_next_payment=0.25, call_price=5.5)
    assert risky.value["necessary_condition"] is True and risky.value["extrinsic"] == pytest.approx(0.5, abs=1e-12)
    assert risky.value["extrinsic_below_dividend"] is True and risky.value["at_risk"] is True
    # the rule is necessary, not sufficient (the skill had it backwards): a large dividend with a large extrinsic value is not at risk
    big = call("options.early_assignment_risk", spot=100.0, strike=95.0, rate=0.05, dividend=2.0, years_to_next_payment=0.25, call_price=9.0)
    assert big.value["necessary_condition"] is True and big.value["at_risk"] is False
    assert any("necessary" in w for w in big.warnings)
    # an out-of-the-money call is not at risk whatever the dividend
    otm = call("options.early_assignment_risk", spot=90.0, strike=95.0, rate=0.05, dividend=3.0, years_to_next_payment=0.25, call_price=1.0)
    assert otm.value["in_the_money"] is False and otm.value["at_risk"] is False
    priced = call("options.early_assignment_risk", spot=100.0, strike=95.0, rate=0.05, dividend=1.0, years_to_next_payment=0.25, volatility=0.3, time_to_expiration=0.2)
    assert priced.value["extrinsic"] == pytest.approx(c.bs_price(100, 95, 0.05, 0.3, 0.2, 0.0, False) - 5.0, abs=1e-12)
    assert_empty(call("options.early_assignment_risk", spot=100.0, strike=95.0, rate=0.05, dividend=1.0, years_to_next_payment=0.25), "call_price")
    assert_empty(call("options.early_assignment_risk", spot=float("nan"), strike=95.0, rate=0.05, dividend=1.0, years_to_next_payment=0.25, call_price=5.0))
    assert_empty(call("options.early_assignment_risk", spot=100.0, strike=95.0, rate=0.05, dividend=-1.0, years_to_next_payment=0.25, call_price=5.0))
    assert_empty(call("options.early_assignment_risk", spot=100.0, strike=95.0, rate=0.05, dividend=1.0, years_to_next_payment=0.0, call_price=5.0))
    with pytest.raises(BadInput):
        call("options.early_assignment_risk", spot="100", strike=95.0, rate=0.05, dividend=1.0, years_to_next_payment=0.25, call_price=5.0)
