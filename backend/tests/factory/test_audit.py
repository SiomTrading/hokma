"""Audit of a backtest result (SPEC-020: ECT-006): 24 checks in 6 phases, the sqrt(2 ln N) bound per year, house fill, configurable PBO limits."""
import dataclasses
import math
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from hokma.backtest.results import BacktestResult, EquityCurve, Trade
from hokma.backtest.spec import BacktestSpec, CostModel
from hokma.factory import audit as au
from hokma.factory.params import Param, ParamSchema
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult
from hokma.quant.validation.overfit import expected_max_sharpe

UTC = timezone.utc
START, END = datetime(2010, 1, 4, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC)         # 14 years


def spec(**kw):
    base = dict(strategy_id="trend_channel_swc", strategy_version=1, params={"entry_n": 20, "exit_m": 10}, line="SWC", instruments=("FUT:NQ:2025-03",),
                bar="1d", start=START, end=END, as_of=END, capital_usd=D("100000"), costs=CostModel(fee_table="nq"))
    return BacktestSpec(**{**base, **kw})


def trade(pnl, side="long", day=5):
    return Trade("FUT:NQ:2025-03", side, D(1), datetime(2020, 1, day, tzinfo=UTC), D("25000"), datetime(2020, 1, day + 1, tzinfo=UTC), D("25010"),
                 D(pnl), D("4.5"), D("-20"), D("30"))


def result(trades=None, costs=None, stats=None, status="done"):
    trades = tuple(trades if trades is not None else [trade("100.50"), trade("-114.50", day=8), trade("-49.50", "short", day=12), trade("80", day=15)])
    costs = {"commission": D("8.0"), "slippage": D("20.0"), "swap": D("0"), "impact": D("0")} if costs is None else costs
    stats = {"sharpe": QuantResult("metrics.sharpe", 0.8, "ratio", "sharpe"), "drawdown_duration": QuantResult("x", {"days": 40}, "days", "dd")} if stats is None else stats
    return BacktestResult(1, status, "s" * 8, "d" * 8, "engine", 0, trades, EquityCurve((START, END), (D("100000"), D("101000"))), D("17"), D("17"), costs, 0, {}, stats)


CONFIG = au.AuditConfig(pbo=au.PboThresholds(0.40, 0.60), min_years=5.0, min_trades=1000, min_dsr=0.95, max_neighbor_drop=0.20, neighbor_spread=0.20,
                        min_regime_share=0.5, red_flag_sharpe=3.0, max_top_trade_share=0.8, min_oos_to_is=0.5)
GOOD = au.AuditEvidence(n_trials=10, point_in_time=True, survivorship_free=True, corporate_actions=True, leak_checked=True, gaps_checked=True,
                        in_sample_tuning=True, holdout_untouched=True, impact_modeled=True, borrow_costs_modeled=True, capacity_checked=True,
                        validation_scheme="walk_forward", multiple_testing_applied=True, dsr=0.97, pbo=0.2, is_sharpe=1.0, oos_sharpe=0.8,
                        neighbor_sharpes={"fast-": 0.7, "fast+": 0.75}, regime_sharpes={"bull": 1.0, "bear": 0.2, "flat": -0.1})


def by_key(report):
    return {i.key: i for i in report.items}


def test_the_checklist_has_24_items_in_6_phases_and_every_item_has_its_rule():
    assert len(au.CHECKLIST) == 24 and [c.number for c in au.CHECKLIST] == list(range(1, 25))
    assert au.PHASES == ("data_integrity", "specification", "execution", "statistics", "overfitting", "reporting")
    assert {c.phase for c in au.CHECKLIST} == set(au.PHASES)
    assert {c.key for c in au.CHECKLIST} == set(au._RULES) and len({c.key for c in au.CHECKLIST}) == 24


def test_the_bound_is_divided_by_the_root_of_the_years_by_hand():
    """sqrt(2 ln 10) = 2.146, sqrt(2 ln 100) = 3.035, sqrt(2 ln 1000) = 3.717 standard deviations; over 14 years the first is 0.57 a year."""
    assert [round(au.chance_sd_bound(n), 2) for n in (10, 100, 1000)] == [2.15, 3.03, 3.72]
    assert au.chance_hurdle_annual(10, 14) == pytest.approx(math.sqrt(2 * math.log(10)) / math.sqrt(14))
    assert round(au.chance_hurdle_annual(10, 14), 2) == 0.57
    assert au.chance_hurdle_annual(1, 14) == 0.0
    assert au.chance_sd_bound(10) >= expected_max_sharpe(10, 1.0)          # the bound sits above the expected best of 10 skill-less trials
    for bad in (0, -1):
        with pytest.raises(BadInput):
            au.chance_sd_bound(bad)
    with pytest.raises(BadInput):
        au.chance_hurdle_annual(10, 0)


def test_a_complete_good_audit_is_go_with_all_24_checks_run():
    report = au.audit(result(), spec(), GOOD, CONFIG)
    assert len(report.items) == 24 and report.verdict == "go"
    assert [p.phase for p in report.phases] == list(au.PHASES) and all(p.status in ("pass", "na") for p in report.phases)
    items = by_key(report)
    assert items["chance_hurdle"].status == "pass"          # Sharpe 0.8 is above 0.57 a year; the misread bound 2.15 would have failed it
    assert "0.574" in items["chance_hurdle"].note and "sqrt(14.00)" in items["chance_hurdle"].note
    assert items["house_fill"].status == "pass" and "next open plus 1 tick" in items["house_fill"].note
    assert items["history_length"].status == "pass" and items["costs_charged"].status == "pass"
    assert report.oos_to_is == pytest.approx(0.8)


def test_missing_evidence_is_unknown_never_a_pass():
    report = au.audit(result(), spec(), au.AuditEvidence(), CONFIG)
    unknown = {i.key for i in report.items if i.status == "unknown"}
    assert report.verdict == "needs_evidence"
    assert unknown >= {"point_in_time", "survivorship_free", "trials_counted", "validation_scheme", "chance_hurdle", "deflated_sharpe", "pbo",
                       "parameter_neighbors", "regimes", "capacity", "multiple_testing"}
    assert by_key(report)["house_fill"].status == "pass" and by_key(report)["gross_and_net"].status == "pass"


def test_a_failed_check_makes_the_verdict_no_go():
    report = au.audit(result(costs={"commission": D("8"), "slippage": D("0"), "swap": D("0"), "impact": D("0")}), spec(), GOOD, CONFIG)
    assert by_key(report)["costs_charged"].status == "fail" and report.verdict == "no_go"
    bad = dataclasses.replace(GOOD, in_sample_tuning=False)
    assert by_key(au.audit(result(), spec(), bad, CONFIG))["in_sample_tuning"].status == "fail"


def test_the_audited_fill_is_the_house_fill():
    assert by_key(au.audit(result(), spec(), GOOD, CONFIG))["house_fill"].status == "pass"
    for field, value in (("model", "probabilistic"), ("next_open_ticks", 0), ("trade_on_close", True), ("stop_first", False), ("gap_fills_at_open", False)):
        mutated = spec()                                   # a spec built with the forbidden rule cannot exist; one changed afterwards is still caught
        object.__setattr__(mutated.fill, field, value)
        assert by_key(au.audit(result(), mutated, GOOD, CONFIG))["house_fill"].status == "fail", field


@pytest.mark.parametrize("thresholds,expected", [((0.40, 0.60), "warn"), ((0.50, 0.70), "pass"), ((0.20, 0.40), "fail")])
def test_pbo_limits_are_configured_not_fixed(thresholds, expected):
    config = dataclasses.replace(CONFIG, pbo=au.PboThresholds(*thresholds))
    report = au.audit(result(), spec(), dataclasses.replace(GOOD, pbo=0.45), config)
    assert by_key(report)["pbo"].status == expected
    assert report.verdict == {"warn": "review", "pass": "go", "fail": "no_go"}[expected]


def test_the_config_has_no_defaults_and_rejects_bad_limits():
    with pytest.raises(TypeError):
        au.AuditConfig()                      # type: ignore[call-arg]
    with pytest.raises(TypeError):
        au.PboThresholds()                    # type: ignore[call-arg]
    for warn, reject in ((0.6, 0.4), (0.0, 0.5), (0.3, 1.0)):
        with pytest.raises(BadInput):
            au.PboThresholds(warn, reject)
    with pytest.raises(BadInput):
        dataclasses.replace(CONFIG, min_dsr=1.5)
    with pytest.raises(BadInput):
        dataclasses.replace(CONFIG, min_trades=0)


def test_parameters_moved_by_20_percent_by_hand():
    neighbors = au.parameter_neighbors({"fast": 10, "slow": 50, "scale": 0.01, "on": True, "name": "x"}, 0.2)
    assert [(n.label, n.params["fast"], n.params["slow"], n.params["scale"]) for n in neighbors] == [
        ("fast-", 8, 50, 0.01), ("fast+", 12, 50, 0.01), ("scale-", 10, 50, pytest.approx(0.008)), ("scale+", 10, 50, pytest.approx(0.012)),
        ("slow-", 10, 40, 0.01), ("slow+", 10, 60, 0.01)]
    assert all(n.params["on"] is True and n.params["name"] == "x" for n in neighbors)
    one = au.parameter_neighbors({"n": 1}, 0.2)                    # 0.8 and 1.2 round back to 1: no neighbor
    assert one == []
    schema = ParamSchema((Param("fast", "int", 10, 9, 11),))
    assert [n.params["fast"] for n in au.parameter_neighbors({"fast": 10}, 0.2, schema)] == [9, 11]
    for bad in (0, 1, True, "x"):
        with pytest.raises(BadInput):
            au.parameter_neighbors({"fast": 10}, bad)


def test_neighbor_runs_decide_the_sensitivity_check():
    weak = dataclasses.replace(GOOD, neighbor_sharpes={"fast-": 0.7, "slow+": 0.4})         # 0.4 loses 50 percent of 0.8
    item = by_key(au.audit(result(), spec(), weak, CONFIG))["parameter_neighbors"]
    assert item.status == "fail" and "slow+" in item.note and "50.0%" in item.note
    assert by_key(au.audit(result(), spec(), GOOD, CONFIG))["parameter_neighbors"].status == "pass"        # 0.7 loses 12.5 percent


def test_regimes_and_red_flags():
    one_regime = dataclasses.replace(GOOD, regime_sharpes={"bull": 1.0, "bear": -0.5, "flat": -0.2})
    assert by_key(au.audit(result(), spec(), one_regime, CONFIG))["regimes"].status == "fail"
    absurd = result(stats={"sharpe": QuantResult("metrics.sharpe", 3.5, "ratio", "sharpe")})
    flag = by_key(au.audit(absurd, spec(), GOOD, CONFIG))["red_flags"]
    assert flag.status == "fail" and "3.50 is above 3.0" in flag.note
    carried = result(trades=[trade("1000"), trade("20", day=8), trade("-10", day=9)])
    assert "one trade carries 98%" in by_key(au.audit(carried, spec(), GOOD, CONFIG))["red_flags"].note        # 1000 of 1020
    degraded = dataclasses.replace(GOOD, is_sharpe=1.0, oos_sharpe=0.3)
    assert "30% of in-sample" in by_key(au.audit(result(), spec(), degraded, CONFIG))["red_flags"].note


def test_multiple_testing_and_financing_rules():
    items = by_key(au.audit(result(), spec(), dataclasses.replace(GOOD, n_trials=1, multiple_testing_applied=None), CONFIG))
    assert items["multiple_testing"].status == "na"
    many = by_key(au.audit(result(), spec(), dataclasses.replace(GOOD, multiple_testing_applied=False), CONFIG))
    assert many["multiple_testing"].status == "fail"
    longs = result(trades=[trade("100"), trade("-10", day=8)])
    none_needed = by_key(au.audit(longs, spec(), dataclasses.replace(GOOD, borrow_costs_modeled=None), CONFIG))
    assert none_needed["financing_and_borrow"].status == "na"
    shorts = by_key(au.audit(result(), spec(), dataclasses.replace(GOOD, borrow_costs_modeled=False), CONFIG))
    assert shorts["financing_and_borrow"].status == "fail"


def test_a_run_that_did_not_finish_is_not_audited_and_the_report_serializes():
    with pytest.raises(BadInput):
        au.audit(result(status="failed"), spec(), GOOD, CONFIG)
    data = au.audit(result(), spec(), GOOD, CONFIG).to_dict()
    assert data["verdict"] == "go" and [p["phase"] for p in data["phases"]] == list(au.PHASES)
    assert sum(len(p["items"]) for p in data["phases"]) == 24
