"""The Hokma risk capacity method: factors, loss capacity, funding, limits and the house charge (SPEC-024)."""
import json
import math
from pathlib import Path

import pytest

from hokma.ips import method

FIXTURES = Path(__file__).parent / "fixtures" / "ips_reference_cases.json"


def base(**blocks):
    """A complete answer set where every factor sits in its top band; blocks override field by field."""
    inputs = {
        "assets": [{"name": "Cash", "type": "cash", "value_usd": 100000}, {"name": "Reserve", "type": "reserve", "value_usd": 6000}],
        "has_debts": False, "debts": [],
        "cashflow": {"monthly_spend_usd": 1000, "reserve_target_months": 6, "monthly_savings_usd": 500},
        "income": {"monthly_income_usd": 5000, "stability": "fixed", "dependents": 0, "withdrawal_habit": "never"},
        "conduct": {"overtrading_days_pct": 0, "stop_moved_pct": 0, "off_rule_size_pct": 0, "revenge_reentry_pct": 0},
        "pockets": {"operating_max_loss_usd": 1000, "operating_from_payouts_usd": None, "speculative_max_loss_usd": 500,
                    "speculative_from_payouts_usd": None},
    }
    return method.merge(inputs, blocks)


def run(**blocks):
    return method.run(base(**blocks))


def results(**blocks):
    return run(**blocks)["results"]


# ---------------------------------------------------------------------------------------------------- 1. factors

@pytest.mark.parametrize("reserve,expected", [(2990, 0), (3000, 1), (5990, 1), (6000, 2)])
def test_c1_months_covered_edges(reserve, expected):
    r = results(assets=[{"name": "Cash", "type": "cash", "value_usd": 100000}, {"name": "Reserve", "type": "reserve", "value_usd": reserve}])
    assert r["c1"] == expected


@pytest.mark.parametrize("payment,expected", [(749.99, 2), (750, 1), (1750, 1), (1750.01, 0)])
def test_c2_debt_ratio_edges(payment, expected):
    # 5000 of income: 15% is 750 and 35% is 1750; from the low cut is 1, above the high cut is 0
    debts = [{"name": "Loan", "balance_usd": 1000, "monthly_payment_usd": payment, "annual_rate_pct": 10, "funds_trading": False}]
    assert results(has_debts=True, debts=debts)["c2"] == expected


@pytest.mark.parametrize("savings,expected", [(499.99, 0), (500, 1), (1250, 1), (1250.01, 2)])
def test_c5_savings_rate_edges(savings, expected):
    assert results(cashflow={"monthly_savings_usd": savings})["c5"] == expected


@pytest.mark.parametrize("dependents,expected", [(0, 2), (1, 1), (2, 1), (3, 0)])
def test_c4_dependents_edges(dependents, expected):
    assert results(income={"dependents": dependents})["c4"] == expected


@pytest.mark.parametrize("pct,expected", [(4.99, 2), (5, 1), (20, 1), (20.01, 0)])
def test_w1_to_w4_conduct_edges(pct, expected):
    r = results(conduct={"overtrading_days_pct": pct, "stop_moved_pct": pct, "off_rule_size_pct": pct, "revenge_reentry_pct": pct})
    assert [r[f"w{i}"] for i in range(1, 5)] == [expected] * 4


@pytest.mark.parametrize("habit,expected", [("never", 0), ("irregular", 1), ("regular", 2)])
def test_w5_withdrawal_habit(habit, expected):
    assert results(income={"withdrawal_habit": habit})["w5"] == expected


@pytest.mark.parametrize("stability,expected", [("none", 0), ("variable", 1), ("fixed", 2)])
def test_c3_stability(stability, expected):
    assert results(income={"stability": stability})["c3"] == expected


def test_c3_becomes_variable_when_trading_is_more_than_half_of_the_income():
    prop = {"own_accounts_net_12m_usd": 120000}              # 10000 a month against 5000 of income: share 2/3
    r = results(prop=prop)
    assert r["trading_share"] > 0.5 and r["stability_used"] == "variable" and r["c3"] == 1
    prop = {"own_accounts_net_12m_usd": 60000}               # 5000 a month against 5000: share exactly 0.5 is not more than half
    r = results(prop=prop)
    assert r["trading_share"] == 0.5 and r["stability_used"] == "fixed" and r["c3"] == 2


def test_c2_and_c5_with_no_income():
    assert results(income={"monthly_income_usd": 0})["c2"] == 2                              # nothing to pay
    r = results(income={"monthly_income_usd": 0}, has_debts=True,
                debts=[{"name": "Loan", "balance_usd": 10, "monthly_payment_usd": 5, "annual_rate_pct": 1, "funds_trading": False}])
    assert r["c2"] == 0 and r["c5"] == 0 and r["debt_ratio"] == "no_data" and r["savings_rate"] == "no_data"
    assert r["trading_share"] == "no_data" and r["loss_method"] == "percent_of_investable"
    assert results(income={"monthly_income_usd": 0})["c5"] == 0


def test_levels_and_the_lower_side_governs():
    r = results()
    assert (r["capacity_score"], r["capacity_level"]) == (9, "high")
    assert (r["willingness_score"], r["willingness_level"]) == (8, "high") and r["risk_level"] == "high"
    assert (r["recovery_months"], r["fallback_pct"]) == (24, 15)
    r = results(conduct={"overtrading_days_pct": 30, "stop_moved_pct": 30, "off_rule_size_pct": 30, "revenge_reentry_pct": 30})
    assert (r["willingness_score"], r["willingness_level"]) == (0, "low") and r["risk_level"] == "low"
    assert (r["recovery_months"], r["fallback_pct"]) == (6, 5)
    r = results(conduct={"overtrading_days_pct": 30, "stop_moved_pct": 30, "off_rule_size_pct": 5, "revenge_reentry_pct": 5},
                income={"withdrawal_habit": "regular"})
    assert (r["willingness_score"], r["willingness_level"]) == (4, "medium") and r["risk_level"] == "medium"


# ---------------------------------------------------------------------------------------------------- 2. loss capacity

def test_loss_capacity_savings_route():
    r = results()
    assert r["loss_method"] == "savings_x_months" and r["loss_capacity"] == 500 * 24


def test_loss_capacity_percent_route_without_savings_or_income():
    r = results(cashflow={"monthly_savings_usd": 0})
    assert r["loss_method"] == "percent_of_investable" and r["investable_capital"] == 100000
    assert r["loss_capacity"] == round(100000 * r["fallback_pct"] / 100, 2)
    r = results(cashflow={"monthly_savings_usd": 0}, income={"monthly_income_usd": 0})
    assert r["loss_method"] == "percent_of_investable" and r["loss_capacity"] > 0


def test_loss_capacity_is_capped_by_the_investable_capital():
    r = results(cashflow={"monthly_savings_usd": 100000})
    assert r["investable_capital"] == 100000 and r["loss_capacity"] == 100000


def test_loss_capacity_is_zero_with_a_negative_investable_capital():
    debts = [{"name": "Loan", "balance_usd": 500000, "monthly_payment_usd": 0, "annual_rate_pct": 1, "funds_trading": False}]
    r = results(has_debts=True, debts=debts)
    assert r["investable_capital"] < 0 and r["loss_capacity"] == 0


def test_loss_capacity_is_null_when_the_savings_are_not_answered():
    r = run(cashflow={"monthly_savings_usd": None})
    assert r["results"]["loss_capacity"] is None and "loss_capacity" in r["missing"]


# ---------------------------------------------------------------------------------------------------- 3. funding

REAL = {"history": {"paid_12m_usd": 1000, "evaluations": 10, "funded": 4, "paid_out_accounts": 2, "payouts_12m_usd": 3000, "reinvest_pct": 50}}


def test_real_cost_mode_and_diagnostics():
    r = results(prop=REAL)
    assert r["cost_mode"] == "real" and r["cost_per_attempt"] == 100
    assert r["pass_rate"] == 0.4 and r["payout_conversion"] == 0.5 and r["net_prop_result"] == 2000
    assert r["prop_multiple"] == 3 and r["avg_payout_per_account"] == 1500 and r["prop_signal"] == "green"
    assert r["cost_per_funded_account"] == 250 and r["cost_per_paid_out_account"] == 500
    assert r["value_per_evaluation"] == 200 and r["evaluations_paid_by_payout_account"] == 15
    assert str(r["expanded_attempts"]) in r["prop_action"]


def test_estimated_cost_mode():
    est = {"evaluation_usd": 100, "reset_usd": 50, "activation_usd": 80, "resets_per_attempt": 2}
    r = results(prop={"estimate": est})
    assert r["cost_mode"] == "estimated" and r["cost_per_attempt"] == 100 + 80 + 2 * 50
    assert r["value_per_evaluation"] == "no_data"


def test_prop_multiple_of_exactly_one_is_amber_and_below_is_red():
    both = lambda payouts: results(prop={"history": {**REAL["history"], "payouts_12m_usd": payouts}})
    assert both(1000)["prop_signal"] == "amber" and both(999.99)["prop_signal"] == "red"
    assert both(2000)["prop_signal"] == "green"


def test_without_payouts_the_signal_has_no_data():
    r = results(prop={"history": {**REAL["history"], "payouts_12m_usd": None}})
    assert r["prop_multiple"] == "no_data" and r["prop_signal"] == "no_data" and r["prop_action"] == "no_data"


def test_division_by_zero_is_no_data_and_a_missing_input_is_null():
    out = run(prop={"history": {"paid_12m_usd": 0, "evaluations": 0, "funded": 0, "paid_out_accounts": 0, "payouts_12m_usd": 0, "reinvest_pct": 0}})
    r = out["results"]
    assert r["cost_mode"] == "no_data" and r["pass_rate"] == "no_data" and r["cost_per_attempt"] == "no_data"
    assert r["cost_per_funded_account"] == "no_data" and r["affordable_attempts"] == "no_data"
    assert not {"cost_mode", "pass_rate", "cost_per_attempt", "affordable_attempts"} & set(out["missing"])
    out = run()
    assert out["results"]["cost_mode"] is None and "cost_mode" in out["missing"] and "affordable_attempts" in out["missing"]


def test_expanded_budget_is_zero_when_there_is_nothing_investable():
    debts = [{"name": "Loan", "balance_usd": 500000, "monthly_payment_usd": 0, "annual_rate_pct": 1, "funds_trading": False}]
    r = results(prop=REAL, has_debts=True, debts=debts)
    assert r["funding_budget"] == 0 and r["expanded_budget"] == 0


def test_attempts_round_to_nine_decimals_before_the_floor():
    # 3 of investable at a 10% fallback gives a loss capacity of 0.3, and 0.3 / 0.1 is 2.9999999999999996 in binary
    r = results(assets=[{"name": "Cash", "type": "cash", "value_usd": 3}], cashflow={"monthly_spend_usd": 1000, "reserve_target_months": 0, "monthly_savings_usd": 0},
                pockets={"operating_max_loss_usd": 0, "speculative_max_loss_usd": 0},
                prop={"estimate": {"evaluation_usd": 0.1, "reset_usd": 0, "activation_usd": 0, "resets_per_attempt": 0}})
    assert r["risk_level"] == "medium" and r["funding_budget"] == 0.3 and r["cost_per_attempt"] == 0.1
    assert 0.3 / 0.1 < 3 and r["affordable_attempts"] == 3


def test_funding_spend_test_and_coherence():
    r = results(prop={"history": {**REAL["history"], "paid_12m_usd": 20000, "payouts_12m_usd": 3000}})
    assert r["house_spend"] == 17000 and r["funding_spend_test"] == "fail"
    assert r["funding_spend_excess"] == round(17000 - r["funding_budget"], 2)
    r = results(prop=REAL)
    assert r["funding_spend_test"] == "pass" and r["funding_spend_excess"] == 0
    assert r["coherence_status"] == "pass" and r["coherence_sum"] == r["loss_capacity"]
    r = results(pockets={"operating_max_loss_usd": 50000})
    assert r["coherence_status"] == "fail" and r["coherence_slack"] < 0


# ---------------------------------------------------------------------------------------------------- 4. net worth and nulls

def test_prop_accounts_never_add_to_the_net_worth():
    plain = results()["net_worth"]
    with_prop = results(assets=base()["assets"] + [{"name": "Funded", "type": "prop_account", "value_usd": 50000}])
    assert with_prop["net_worth"] == plain and with_prop["prop_excluded"] == 50000


def test_a_missing_factor_leaves_its_side_null():
    out = run(income={"dependents": None})
    r = out["results"]
    assert r["c4"] is None and r["capacity_score"] is None and r["capacity_level"] is None
    assert r["willingness_level"] == "high" and r["risk_level"] is None and r["loss_capacity"] is None
    assert {"c4", "capacity_level", "risk_level", "loss_capacity"} <= set(out["missing"])


def test_input_validations():
    bad = [
        ({"prop": {"history": {"evaluations": 3, "funded": 4}}}, "cannot be more than the evaluations"),
        ({"prop": {"history": {"funded": 3, "paid_out_accounts": 4}}}, "cannot be more than the accounts that passed"),
        ({"prop": {"history": {"funded": 3, "paid_out_accounts": 2, "payouts_12m_usd": 0}}}, "payouts received are 0"),
        ({"has_debts": False, "debts": [{"name": "Loan", "balance_usd": 5}]}, "no debts"),
        ({"conduct": {"stop_moved_pct": 101}}, "between 0 and"),
        ({"prop": {"history": {"reinvest_pct": -1}}}, "cannot be negative"),
        ({"cashflow": {"monthly_spend_usd": -5}}, "cannot be negative"),
        ({"assets": [{"name": "X", "type": "boat", "value_usd": 5}]}, "not one of"),
    ]
    for blocks, text in bad:
        with pytest.raises(method.IpsInputError, match=text):
            method.run(base(**blocks))
    assert method.run(base(prop={"own_accounts_net_12m_usd": -500}))["results"]["trading_net_12m"] == -500


def test_fractions_of_one_get_a_warning():
    out = method.run(base(conduct={"overtrading_days_pct": 0.05, "stop_moved_pct": 0.1, "off_rule_size_pct": 0, "revenge_reentry_pct": 0.02}))
    assert out["warnings"] and "5% is written 5" in out["warnings"][0]
    assert not method.run(base())["warnings"]


# ---------------------------------------------------------------------------------------------------- 12. reference cases

@pytest.mark.parametrize("name", ["full", "estimated_no_income", "no_evaluations"])
def test_same_numbers_as_the_reference_method_key_by_key(name):
    case = json.loads(FIXTURES.read_text(encoding="utf-8"))[name]
    got = method.run(case["inputs"])["results"]
    for key, expected in case["expected"].items():
        assert got[key] == expected, (key, got[key], expected)


def test_reference_case_funding_action_names_the_expanded_attempts():
    case = json.loads(FIXTURES.read_text(encoding="utf-8"))["full"]
    got = method.run(case["inputs"])["results"]
    assert got["prop_signal"] == "green" and got["prop_action"] == method.METHOD_TABLES["signal_actions"]["green"].replace("{N}", str(got["expanded_attempts"]))


def test_green_action_variants():
    actions = method.METHOD_TABLES["signal_actions"]
    # no reinvestment percentage: the expanded budget has no data, so no number of attempts is quoted
    r = results(prop={"history": {**REAL["history"], "reinvest_pct": None}})
    assert r["expanded_attempts"] == "no_data" and r["prop_action"] == actions["green_no_attempts_yet"]
    # no budget left for attempts: the alternative text does not suggest scaling
    debts = [{"name": "Loan", "balance_usd": 500000, "monthly_payment_usd": 0, "annual_rate_pct": 1, "funds_trading": False}]
    r = results(prop=REAL, has_debts=True, debts=debts)
    assert r["expanded_attempts"] == 0 and r["prop_action"] == actions["green_zero_attempts"] and "scale" not in r["prop_action"]


# ---------------------------------------------------------------------------------------------------- 13. max loss

def limits(**blocks):
    return run(**blocks)["limits"]


def test_max_loss_is_the_lower_of_loss_capacity_and_the_house_pockets():
    out = run()                                                # pockets fit: 1000 + 500 + a budget that fills the rest
    lim, r = out["limits"], out["results"]
    assert lim["pockets_house_total_usd"] == r["loss_capacity"] == 12000 and lim["max_loss_usd"] == 12000
    assert lim["capital_floor_usd"] == max(0, r["investable_capital"] - 12000)
    assert lim["max_drawdown_pct"] == 12000 / r["investable_capital"] * 100

    out = run(pockets={"operating_max_loss_usd": 20000})       # pockets exceed the loss capacity: it governs and a check fails
    assert out["limits"]["pockets_house_total_usd"] == 20500 and out["limits"]["max_loss_usd"] == 12000
    assert {c["id"]: c["status"] for c in out["checks"]}["pockets_within_loss_capacity"] == "fail"

    out = run(pockets={"speculative_max_loss_usd": None})      # a pocket left empty: nothing is invented
    lim = out["limits"]
    assert lim["max_loss_usd"] is None and lim["capital_floor_usd"] is None and lim["max_drawdown_pct"] is None
    assert lim["pockets_house_total_usd"] is None


def test_pockets_funded_by_payouts_do_not_count_against_the_house():
    r = results(pockets={"operating_max_loss_usd": 1000, "operating_from_payouts_usd": 400, "speculative_max_loss_usd": 500, "speculative_from_payouts_usd": 800})
    assert r["house_loss_operating"] == 600 and r["house_loss_speculative"] == 0      # the excess of one never offsets the other
    assert r["payouts_in_pockets"] == 1200


def test_drawdown_has_no_data_without_investable_capital():
    debts = [{"name": "Loan", "balance_usd": 500000, "monthly_payment_usd": 0, "annual_rate_pct": 1, "funds_trading": False}]
    lim = limits(has_debts=True, debts=debts)
    assert lim["max_loss_usd"] == 0 and lim["max_drawdown_pct"] == "no_data" and lim["capital_floor_usd"] == 0


# ---------------------------------------------------------------------------------------------------- 14. house and funding

FUNDED = {"desk": "Futures", "capital_source": "funded", "funding_budget_usd": 1000, "avg_account_cost_usd": 50, "prop_max_drawdown_per_account_usd": 2000}


def desk_rows(*desks):
    return run(declared_limits={"desk_allocation": list(desks)})["limits"]["desk_allocation"]


def test_a_funded_desk_counts_what_the_house_paid_for_accounts():
    row = desk_rows(FUNDED)[0]
    assert row["max_accounts"] == 20 and row["house_limit_usd"] == 1000 and row["max_loss_usd"] is None
    assert method.house_charge(FUNDED, {"kind": "market_loss", "amount_usd": 2000}) == 0
    assert method.house_charge(FUNDED, {"kind": "account_cost", "amount_usd": 50}) == 50
    assert row["house_limit_usd"] - 50 == 950 and row["max_accounts"] - 1 == 19
    assert method.house_charge({"capital_source": "own"}, {"kind": "market_loss", "amount_usd": 2000}) == 2000
    assert method.house_charge(FUNDED, {"kind": "account_cost", "amount_usd": 50, "paid_with": "reinvested_payouts"}) == 0


def test_the_drawdown_rule_of_the_funding_firm_never_changes_the_house_limit():
    other = {**FUNDED, "prop_max_drawdown_per_account_usd": 9999}
    assert desk_rows(FUNDED)[0]["house_limit_usd"] == desk_rows(other)[0]["house_limit_usd"] == 1000


def test_a_desk_that_mixes_the_two_kinds_of_capital_is_an_error():
    with pytest.raises(method.IpsInputError, match="an own-capital desk declares only max_loss_usd"):
        desk_rows({"desk": "Own", "capital_source": "own", "max_loss_usd": 500, "funding_budget_usd": 100})
    with pytest.raises(method.IpsInputError, match="a funded desk declares funding_budget_usd"):
        desk_rows({**FUNDED, "max_loss_usd": 5})
    with pytest.raises(method.IpsInputError, match="capital_source is own or funded"):
        desk_rows({"desk": "X"})


def test_accounts_with_a_zero_cost_have_no_data():
    assert desk_rows({**FUNDED, "avg_account_cost_usd": 0})[0]["max_accounts"] == "no_data"
    assert desk_rows({**FUNDED, "avg_account_cost_usd": None})[0]["max_accounts"] is None


# ---------------------------------------------------------------------------------------------------- 15. desks within the fund

LOW_RISK = {"conduct": {"overtrading_days_pct": 30, "stop_moved_pct": 30, "off_rule_size_pct": 30, "revenge_reentry_pct": 30},
            "assets": [{"name": "Cash", "type": "cash", "value_usd": 20000}, {"name": "Reserve", "type": "reserve", "value_usd": 6000}],
            "cashflow": {"monthly_savings_usd": 0}}      # low level, 5% of 20000 investable: a max loss of exactly 1000


def test_house_limits_of_the_desks_stay_within_the_max_loss():
    own = lambda amount: {"desk": "Own", "capital_source": "own", "max_loss_usd": amount}
    funded = {"desk": "Funded", "capital_source": "funded", "funding_budget_usd": 400, "avg_account_cost_usd": 40}
    out = run(declared_limits={"desk_allocation": [own(600), funded]}, **LOW_RISK)
    assert out["limits"]["max_loss_usd"] == 1000
    assert {c["id"]: c for c in out["checks"]}["desk_house_limits_within_max_loss"]["status"] == "pass"
    out = run(declared_limits={"desk_allocation": [own(700), funded]}, **LOW_RISK)
    check = {c["id"]: c for c in out["checks"]}["desk_house_limits_within_max_loss"]
    assert check["status"] == "fail" and "1,100.00" in check["numbers"] and "1,000.00" in check["numbers"]


def test_house_charge_rejects_what_it_does_not_know():
    with pytest.raises(ValueError):
        method.house_charge({"capital_source": "other"}, {"kind": "market_loss", "amount_usd": 1})
    with pytest.raises(ValueError):
        method.house_charge(FUNDED, {"kind": "gift", "amount_usd": 1})
    with pytest.raises(ValueError):
        method.house_charge(FUNDED, {"kind": "market_loss", "amount_usd": -1})


def test_round2_is_half_up_over_fifteen_digits():
    assert method.round2(2.675) == 2.68 and method.round2(1.005) == 1.01 and method.round2(0.125) == 0.13
    assert method.round2(2.5, 0) == 3 and math.isclose(method.round2(-0.125), -0.13)
