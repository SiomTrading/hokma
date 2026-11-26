"""Criterion 6 of SPEC-020: the calibration of the premium selling threshold opens and closes the switch of SPEC-019 only with the evidence of a backtest
(section 4.9, decision 1260). Everything here goes through the real `premium_selling.switch_state`, the real tables and the real options rules.

THE CHAIN HISTORY IS SYNTHETIC (see `backtest/calibration_support.py`): the repo has no recorded history of an option chain, only one day. The tests show that the
pipeline opens, refuses and closes by the numbers; they say nothing about a real market. The missing input is a long recorded history of the real chain."""
import dataclasses
import inspect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from backtest.calibration_support import alpaca_day, bars_frame, fair_history, rich_history
from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma.backtest.calibration import gate as gate_mod
from hokma.backtest.calibration import premium
from hokma.backtest.calibration.premium import ChainHistory
from hokma.exec.guards.premium_selling import calibrated_rows, switch_state

AS_OF = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)


@pytest.fixture
def swo(ready):
    with ready.db.write() as u:
        u.session.update("desk_profile", {"desk_id": ready.desk_id}, {"line": "SWO"})
        u.session.update("desk_limits", {"desk_id": ready.desk_id}, {"config": json.dumps({"max_drawdown_pct": 15})})     # the desk is born with its row (1.1.0)
    return ready


def run(swo, history, **kw):
    return premium.calibrate(swo.desk_id, "SPY", as_of=AS_OF, source=lambda name, as_of: history, db=swo.db, **kw)


def rows(swo, sql="SELECT id, status, backtest_run_ref, evidence_json FROM premium_thresholds ORDER BY id"):
    with swo.db.read() as u:
        return u.session.query(sql)


def runs(swo):
    with swo.db.read() as u:
        return u.session.query("SELECT id, kind, status, summary_json, data_hash FROM backtest_runs WHERE kind = 'calibration' ORDER BY id")


# ---- no history: data_missing and nothing is written -----------------------------------------------------------------------

def test_without_a_history_of_the_chain_the_status_is_data_missing_and_nothing_is_written(swo):
    for source in (None, lambda name, as_of: None):                    # no provider has registered one; or a provider that has no data
        found = premium.calibrate(swo.desk_id, "SPY", as_of=AS_OF, source=source, db=swo.db)
        assert (found.status, found.reasons) == ("data_missing", ("no_chain_history",))
        assert found.threshold is None and found.run_id is None and found.backtest_run_ref is None
    assert rows(swo) == [] and runs(swo) == []
    assert not switch_state(swo.desk_id, "SPY", db=swo.db).is_open


def test_the_one_recorded_day_of_the_repo_is_not_a_history(swo):
    day = alpaca_day()
    one_day = ChainHistory("AAPL", (day,), bars_frame([day.day], [float(day.spot)]))
    found = premium.calibrate(swo.desk_id, "AAPL", as_of=AS_OF, source=lambda n, a: one_day, db=swo.db)
    assert found.status == "data_missing" and found.reasons == ("chain_history_too_short:1<504",)
    assert rows(swo) == [] and runs(swo) == []


def test_days_after_the_cut_in_time_are_never_read(swo):
    """A history that ends in the future of `as_of` is cut at `as_of`: with the cut at day 300 there are too few days, though the whole file has 2,319."""
    early = datetime.combine(rich_history().days[300].day, datetime.min.time(), tzinfo=timezone.utc)
    found = premium.calibrate(swo.desk_id, "SPY", as_of=early, source=lambda n, a: rich_history(), db=swo.db)
    assert (found.status, found.reasons) == ("data_missing", ("chain_history_too_short:301<504",))


def test_a_history_too_short_for_the_windows_of_the_gate_is_data_missing_and_writes_nothing(swo):
    short = rich_history().until(rich_history().days[599].day)
    many_windows = gate_mod.tightened(gate_mod.load_default(), folds=20)                  # 600 days cut in 21 pieces of 28 days: less than the purge of 60 days
    found = run(swo, short, gate=many_windows)
    assert found.status == "data_missing" and found.reasons[0].startswith("walk_forward_impossible:")
    assert rows(swo) == [] and runs(swo) == []


def test_a_history_with_no_implied_volatility_has_no_metric_and_is_data_missing(swo):
    bare = ChainHistory("SPY", tuple(dataclasses.replace(d, quotes=()) for d in rich_history().days[:600]), rich_history().bars)
    found = run(swo, bare)
    assert found.status == "data_missing" and found.reasons[0].startswith("metric_history_too_short")
    assert rows(swo) == []


# ---- a history that passes the gate: the row is written by the code, with the evidence of the backtest ---------------------------

def test_with_a_recorded_history_that_passes_the_gate_the_switch_opens_with_the_evidence_of_the_run(swo):
    before = switch_state(swo.desk_id, "SPY", db=swo.db)
    assert not before.is_open and before.reason == "no_calibrated_threshold"
    found = run(swo, rich_history())
    assert found.status == "calibrated" and found.reasons == ()
    assert found.backtest_run_ref == f"calibration-{found.run_id}" and found.threshold == Decimal(str(found.measures["chosen"]["threshold"]))
    state = switch_state(swo.desk_id, "SPY", db=swo.db)
    assert state.is_open and (state.metric, state.threshold, state.backtest_run_ref, state.row_id) == ("iv_rv_ratio", found.threshold, found.backtest_run_ref, found.threshold_id)
    (row,) = rows(swo)
    assert (row["status"], row["backtest_run_ref"]) == ("calibrated", found.backtest_run_ref)
    evidence = json.loads(row["evidence_json"])
    assert evidence["leakage_free"] is True and evidence["n_trials"] == 18 == found.measures["n_trials"]        # 9 thresholds x 2 structures
    assert evidence["dsr"] >= 0.95 and evidence["pbo"] <= 0.50 and evidence["content_hash"]
    shown = evidence["parameters"]["premium_calibration"]
    assert shown["gate"]["min_oos_trades"] == 30 and shown["measures"]["oos_trades"] >= 30 and shown["measures"]["wf_efficiency"] >= 0.5
    (saved,) = runs(swo)
    assert saved["id"] == found.run_id and saved["status"] == "done" and saved["data_hash"] == rich_history().until(AS_OF.date()).data_hash == found.measures["data_hash"]
    body = json.loads(saved["summary_json"])
    assert body["reasons"] == [] and body["measures"]["rules"] == "quant.options.margin+assignment" and body["gate"]["fee_table"] == "ibkr_options"


def test_the_measures_of_the_pass_are_the_ones_the_gate_asks_for(swo):
    found = run(swo, rich_history())
    m = found.measures
    assert m["oos_trades"] >= 30 and m["dsr"] >= 0.95 and m["pbo"] <= 0.50 and m["wf_efficiency"] >= 0.5 and m["max_drawdown_pct"] <= m["drawdown_limit_pct"] == 15.0
    assert m["expectancy_per_margin_dollar"]["mean"] > 0 and m["expectancy_per_margin_dollar"]["ci_low"] <= m["expectancy_per_margin_dollar"]["mean"]
    assert m["thresholds"] == sorted(m["thresholds"]) and 1 <= len(m["thresholds"]) <= 9 and m["structures"] == ["cash_secured_put", "credit_put_spread"]
    assert found.measures["chosen"]["threshold"] in m["thresholds"]


def test_the_same_history_gives_the_same_result_byte_for_byte(swo):
    first, second = run(swo, rich_history()), run(swo, rich_history())
    assert first.measures == second.measures and first.threshold == second.threshold
    a, b = runs(swo)
    assert a["summary_json"] == b["summary_json"] and a["data_hash"] == b["data_hash"]


# ---- a history that does not pass: no row, and every reason -------------------------------------------------------------------

def test_a_history_that_does_not_pass_writes_no_row_and_lists_every_reason(swo):
    found = run(swo, fair_history())
    assert found.status == "not_passed" and found.threshold is None and found.threshold_id is None
    names = [r.split(":")[0] for r in found.reasons]
    assert names == ["dsr_below_min", "drawdown_above_limit"], found.reasons                  # the premium is not there: the deflated Sharpe fails and the loss is above the 15 percent of the desk
    assert found.measures["dsr"] < 0.95 and found.measures["max_drawdown_pct"] > 15.0
    assert rows(swo) == [] and not switch_state(swo.desk_id, "SPY", db=swo.db).is_open
    (saved,) = runs(swo)                                              # the evidence of the refusal is kept: a recalibration quotes it
    assert saved["id"] == found.run_id and json.loads(saved["summary_json"])["reasons"] == list(found.reasons)


def test_a_desk_with_no_limit_of_drawdown_cannot_pass_and_says_why(swo):
    with swo.db.write() as u:
        u.session.execute("DELETE FROM desk_limits WHERE desk_id = :d", {"d": swo.desk_id})
    found = run(swo, rich_history())
    assert found.status == "not_passed" and found.reasons == ("drawdown_limit_missing",)
    assert rows(swo) == []


def test_the_limit_of_the_desk_wins_over_a_looser_limit_of_the_gate(swo):
    gate = gate_mod.tightened(gate_mod.load_default(), max_drawdown_pct=90.0)
    found = run(swo, rich_history(), gate=gate)
    assert found.status == "calibrated" and found.measures["drawdown_limit_pct"] == 15.0
    tight = run(swo, rich_history(), gate=dataclasses.replace(gate_mod.load_default(), max_drawdown_pct=0.5))
    assert tight.status == "not_passed" and tight.measures["drawdown_limit_pct"] == 0.5 and tight.reasons[0].startswith("drawdown_above_limit")


def test_the_gate_of_the_owner_can_tighten_and_a_looser_one_is_refused_before_anything_runs(swo):
    tight = gate_mod.tightened(gate_mod.load_default(), min_oos_trades=500)
    found = run(swo, rich_history(), gate=tight)
    assert found.status == "not_passed" and found.reasons[0].startswith("too_few_oos_trades:") and found.reasons[0].endswith("<500")
    loose = dataclasses.replace(gate_mod.load_default(), min_dsr=0.6, max_pbo=0.99)
    with pytest.raises(gate_mod.GateLoosened) as caught:
        run(swo, fair_history(), gate=loose)
    assert caught.value.code == "gate_loosened" and "min_dsr" in str(caught.value) and "max_pbo" in str(caught.value)
    assert rows(swo) == []                                           # nothing ran, nothing was written
    assert run(swo, fair_history(), gate=loose, owner_loosened=True).status == "not_passed"      # the owner may loosen; the numbers still decide


def test_no_argument_of_calibrate_can_open_the_switch():
    names = set(inspect.signature(premium.calibrate).parameters)
    assert names == {"desk_id", "underlying", "as_of", "gate", "source", "rules", "db", "owner_loosened", "seed"}
    assert not names & {"threshold", "metric", "evidence", "status", "backtest_run_ref", "open", "force"}


def test_a_bad_call_is_refused_with_a_stable_code(swo):
    for bad in ({"desk_id": 0, "underlying": "SPY"}, {"desk_id": True, "underlying": "SPY"}, {"desk_id": swo.desk_id, "underlying": "../x"}, {"desk_id": 99999, "underlying": "SPY"}):
        with pytest.raises(premium.CalibrationError) as caught:
            premium.calibrate(as_of=AS_OF, source=lambda n, a: None, db=swo.db, **bad)
        assert caught.value.code == "bad_calibration"
    with pytest.raises(premium.CalibrationError):
        premium.calibrate(swo.desk_id, "SPY", as_of=datetime(2026, 1, 1), source=lambda n, a: None, db=swo.db)      # a date with no zone


def test_without_the_rules_of_the_options_module_the_status_is_unavailable_and_nothing_is_written(swo, monkeypatch):
    def missing():
        raise premium.RulesUnavailable("options_rules_unavailable: no module")
    monkeypatch.setattr(premium, "default_rules", missing)
    found = run(swo, rich_history())
    assert found.status == "unavailable" and found.reasons[0].startswith("options_rules_unavailable")
    assert rows(swo) == [] and runs(swo) == []


# ---- the recalibration -------------------------------------------------------------------------------------------------------

def test_a_recalibration_that_fails_retires_the_row_and_the_switch_closes(swo):
    opened = run(swo, rich_history())
    assert switch_state(swo.desk_id, "SPY", db=swo.db).is_open
    (result,) = premium.recalibrate(swo.desk_id, as_of=AS_OF, source=lambda n, a: fair_history(), db=swo.db)
    assert result.status == "not_passed" and result.retired_id == opened.threshold_id
    state = switch_state(swo.desk_id, "SPY", db=swo.db)
    assert not state.is_open and state.reason == "no_calibrated_threshold"
    (row,) = rows(swo)
    assert row["status"] == "retired"
    retired = json.loads(row["evidence_json"])["retired"]
    assert retired["backtest_run_ref"] == result.backtest_run_ref != opened.backtest_run_ref
    assert retired["reason"].startswith("recalibration_failed:") and "dsr_below_min" in retired["reason"]
    assert calibrated_rows(swo.desk_id, db=swo.db) == []


def test_a_recalibration_that_passes_replaces_the_row_and_the_switch_stays_open(swo):
    first = run(swo, rich_history())
    (again,) = premium.recalibrate(swo.desk_id, as_of=AS_OF, source=lambda n, a: rich_history(), db=swo.db)
    assert again.status == "calibrated" and again.retired_id is None and again.threshold_id != first.threshold_id
    assert [r["status"] for r in rows(swo)] == ["retired", "calibrated"]                  # record_calibration retires the one it replaces
    state = switch_state(swo.desk_id, "SPY", db=swo.db)
    assert state.is_open and state.row_id == again.threshold_id and state.backtest_run_ref == again.backtest_run_ref


def test_a_recalibration_with_no_history_keeps_a_young_row_and_retires_a_stale_one(swo):
    opened = run(swo, rich_history())
    (young,) = premium.recalibrate(swo.desk_id, as_of=AS_OF, source=lambda n, a: None, db=swo.db)
    assert young.status == "data_missing" and young.retired_id is None and switch_state(swo.desk_id, "SPY", db=swo.db).is_open
    (stale,) = premium.recalibrate(swo.desk_id, as_of=datetime.now(timezone.utc) + timedelta(days=premium.STALE_DAYS + 1), source=lambda n, a: None, db=swo.db)
    assert stale.status == "data_missing" and stale.retired_id == opened.threshold_id
    assert not switch_state(swo.desk_id, "SPY", db=swo.db).is_open
    retired = json.loads(rows(swo)[0]["evidence_json"])["retired"]
    assert retired["reason"].startswith("calibration_stale_no_history:") and retired["backtest_run_ref"] == opened.backtest_run_ref


def test_a_recalibration_with_no_rules_changes_nothing(swo, monkeypatch):
    run(swo, rich_history())
    monkeypatch.setattr(premium, "default_rules", lambda: (_ for _ in ()).throw(premium.RulesUnavailable("options_rules_unavailable")))
    (result,) = premium.recalibrate(swo.desk_id, as_of=AS_OF, source=lambda n, a: fair_history(), db=swo.db)
    assert result.status == "unavailable" and result.retired_id is None and switch_state(swo.desk_id, "SPY", db=swo.db).is_open


def test_the_drift_alarm_measures_the_thresholds_of_the_desk_again_only_when_it_rings(swo):
    run(swo, rich_history())
    assert premium.on_drift_alarm(swo.desk_id, alarm=False, as_of=AS_OF, source=lambda n, a: fair_history(), db=swo.db) == []
    assert switch_state(swo.desk_id, "SPY", db=swo.db).is_open
    (result,) = premium.on_drift_alarm(swo.desk_id, as_of=AS_OF, source=lambda n, a: fair_history(), db=swo.db)
    assert result.retired_id is not None and not switch_state(swo.desk_id, "SPY", db=swo.db).is_open


def test_the_switch_of_another_desk_or_underlying_is_not_touched(swo):
    run(swo, rich_history())
    assert not switch_state(swo.desk_id, "QQQ", db=swo.db).is_open and not switch_state(swo.desk_id + 1, "SPY", db=swo.db).is_open
    assert [r.underlying for r in calibrated_rows(swo.desk_id, db=swo.db)] == ["SPY"]
