"""1.3.0 (task 3433), E: the strategy of the new flow in the bank, the registry, the backtest and the optimizer; Ben before paper and real money, the owner after him; the old format keeps running.

Real money is protected twice: the code of the owner decision and a trigger of the base. A strategy that only passed the checks of Hokma is available to the backtest and the optimizer and nowhere else."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from backtest.runner_support import NQ, source_of
from backtest.support import requires_engine, spec as base_spec
from code_support import BRIEF, make_bank, put_files, rows
from code_support import db, env, pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)
from e2e_h4.support import caller, failed, ok
from test_pandas_checks_1300 import REFERENCE

from hokma import backtest
from hokma.backtest import runner
from hokma.backtest.ticks import Bar
from hokma.core.config import hokma_home
from hokma.factory import pandas_flow, user_rules
from hokma.factory.pandas_strategy import checks, loader
from hokma.factory.spec import default_registry, live_registry

CREATOR, BEN = "strategy-creator", "strategy-reviewer"


def make_pandas_version(db, source=REFERENCE):
    """What strategy_create does for the storage, step by step: the card and its spec, the requirement, the file of the fund and the saved version."""
    bank_id = make_bank(db, title="Ema vwap")
    made = ok("strategy_brief_write", {"bank_id": bank_id, "title": "Ema vwap", "brief": {"summary": "Enters long when the EMA crosses above the VWAP of the regular session."}}, caller(CREATOR))
    problems, defs = loader.read(source)
    assert problems == []
    text = pandas_flow.text_of(source)
    slug = rows(db, "SELECT slug FROM strategy_bank WHERE id = :i", i=bank_id)[0]["slug"]
    folder = user_rules.version_folder(bank_id, slug, 1)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "strategy.py").write_bytes(text.encode("utf-8"))
    brief = rows(db, "SELECT id, request_version FROM user_rule_briefs WHERE rule_id = :r", r=made["rule_id"])[0]
    stored = pandas_flow.store_version(rule_id=made["rule_id"], brief_id=int(brief["id"]), request_version=int(brief["request_version"]), registry_id=f"u_str{bank_id:03d}_v1", number=1,
                                       text=text, defs=defs, report={"stats": {}}, caller=CREATOR, origin=(None, None), db=db)
    return bank_id, made["rule_id"], stored


def status_of(db, version_id):
    return rows(db, "SELECT status FROM user_rule_versions WHERE id = :i", i=version_id)[0]["status"]


def test_the_checks_generate_the_version_and_the_optimizer_can_have_it_but_paper_and_real_money_cannot(db):
    _bank, _rule, stored = make_pandas_version(db)
    assert stored["status"] == "generated" and status_of(db, stored["rule_version_id"]) == "generated"
    rid = stored["registry_id"]
    assert rid in default_registry().entries and rid not in live_registry().entries
    reviews = rows(db, "SELECT reviewer, verdict FROM user_rule_reviews WHERE rule_version_id = :i", i=stored["rule_version_id"])
    assert [(r["reviewer"], r["verdict"]) for r in reviews] == [("hokma-checks", "approved")]
    with pytest.raises(user_rules.UserRuleError) as err:
        user_rules.propose_gate(rid)
    assert err.value.code == "blocked"


def test_the_owner_cannot_approve_a_version_that_only_passed_the_checks_neither_through_the_code_nor_through_the_base(db):
    _bank, _rule, stored = make_pandas_version(db)
    vid = stored["rule_version_id"]
    with pytest.raises(user_rules.UserRuleError) as err:
        user_rules.owner_decision(rule_version_id=vid, approve=True)
    assert err.value.code == "blocked" and "Ben" in err.value.message and status_of(db, vid) == "generated"
    with pytest.raises(Exception) as raw:                                                       # whoever writes the base directly: the trigger of 0041
        with db.write() as u:
            u.session.execute("UPDATE user_rule_versions SET status = 'owner_reviewed', reviewed_by = 'x', reviewed_at = CURRENT_TIMESTAMP, reviewed_sha256 = code_sha256 WHERE id = :i", {"i": vid})
    assert "reviewer has not approved" in str(raw.value)
    assert status_of(db, vid) == "generated" and stored["registry_id"] not in live_registry().entries


def test_ben_judges_the_logic_the_creator_cannot_judge_his_own_and_the_checks_are_not_a_reviewer(db):
    _bank, _rule, stored = make_pandas_version(db)
    vid = stored["rule_version_id"]
    args = {"rule_version_id": vid, "verdict": "approved"}
    assert failed("strategy_code_verdict", args, caller(CREATOR))["error_code"] == "blocked"
    assert failed("strategy_code_verdict", args, caller("hokma-checks"))["error_code"] == "blocked"
    assert failed("strategy_code_verdict", {"rule_version_id": vid, "verdict": "rejected"}, caller(BEN))["error_code"] == "bad_input"      # a rejection needs its findings
    done = ok("strategy_code_verdict", args, caller(BEN))
    assert done["status"] == "generated" and done["ben_approved"] is True
    owner = user_rules.owner_decision(rule_version_id=vid, approve=True)
    assert owner["status"] == "owner_reviewed" and stored["registry_id"] in live_registry().entries
    user_rules.propose_gate(stored["registry_id"])                                               # now it may be proposed


def test_a_rejection_of_ben_closes_the_version_and_the_owner_cannot_pick_it_up(db):
    _bank, _rule, stored = make_pandas_version(db)
    vid = stored["rule_version_id"]
    done = ok("strategy_code_verdict", {"rule_version_id": vid, "verdict": "rejected", "findings": [{"severity": "major", "message": "the exit does not match the spec"}]}, caller(BEN))
    assert done["status"] == "rejected" and status_of(db, vid) == "rejected"
    with pytest.raises(user_rules.UserRuleError):
        user_rules.owner_decision(rule_version_id=vid, approve=True)
    assert stored["registry_id"] not in default_registry().entries


def test_a_change_of_the_code_after_the_review_does_not_load(db):
    bank_id, _rule, stored = make_pandas_version(db)
    slug = rows(db, "SELECT slug FROM strategy_bank WHERE id = :i", i=bank_id)[0]["slug"]
    path = user_rules.version_folder(bank_id, slug, 1) / "strategy.py"
    path.write_bytes(path.read_bytes() + b"\n# changed\n")
    assert stored["registry_id"] not in default_registry().entries and "does not load" in user_rules.LOAD_PROBLEMS[stored["registry_id"]]


def test_params_are_the_search_space_of_the_optimizer(db):
    _bank, _rule, stored = make_pandas_version(db)
    space = pandas_flow.space_for(stored["registry_id"])
    assert space.names == ("ema_n", "atr_n", "atr_mult") and space.axes[0].values[:3] == (3, 4, 5) and space.axes[2].values == (1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)
    assert pandas_flow.space_for(stored["registry_id"], ("atr_mult",)).names == ("atr_mult",)
    from hokma.kit import backtest_tools

    assert backtest_tools._space_of({"strategy_id": stored["registry_id"]}).names == space.names
    given = {"axes": [{"name": "ema_n", "kind": "int", "low": 5, "high": 7, "step": 1}]}
    assert backtest_tools._space_of({"strategy_id": stored["registry_id"], "space": given}).names == ("ema_n",)
    assert pandas_flow.space_for("u_other_v1") is None
    with pytest.raises(Exception) as err:
        backtest_tools._space_of({"strategy_id": "ema_cross"})
    assert "space is needed" in str(err.value)


def sample_series():
    times, cols = checks.sample_bars()
    return [Bar(datetime.fromtimestamp(int(t) / 1e9, tz=timezone.utc), D(repr(float(o))), D(repr(float(h))), D(repr(float(l))), D(repr(float(c))), D(repr(float(v))))
            for t, o, h, l, c, v in zip(times, cols["open"], cols["high"], cols["low"], cols["close"], cols["volume"])]


@requires_engine
def test_the_backtest_and_the_optimizer_run_it_through_the_process_apart(db, tmp_path):
    _bank, _rule, stored = make_pandas_version(db)
    bars = sample_series()
    spec = base_spec(strategy_id=stored["registry_id"], bar="5m", start=bars[0].ts, end=bars[-1].ts + timedelta(minutes=5), as_of=bars[-1].ts + timedelta(minutes=5), params={})
    services = runner.Services(db=db, registry=default_registry(), source=source_of({NQ: bars}), curve_dir=tmp_path)
    result = backtest.run(spec, services=services)
    assert result.status == "done" and len(result.trades) > 0, result.warnings
    assert all(t.exit_price is not None for t in result.trades)
    space = pandas_flow.space_for(stored["registry_id"], ("atr_mult",))
    found = backtest.optimize(spec, space, method="grid", n_trials=7, objective="net_pnl", presweep=False, services=services)
    assert found.best is not None and found.n_trials >= 7 and not any(t.status == "failed" for t in found.trials)


@requires_engine
def test_the_old_format_keeps_loading_and_running_in_the_backtest_and_the_optimizer(db, tmp_path):
    """Regression: a strategy written as code in the old format (class with on_bar, tests first) is saved, reviewed, generated and run like before."""
    bank_id = make_bank(db)
    made = ok("strategy_brief_write", {"bank_id": bank_id, "slug": "old_format", "title": "Inside bar", "brief": BRIEF}, caller(CREATOR))
    put_files("old_format")
    saved = ok("strategy_code_save", {"rule_id": made["rule_id"]}, caller(CREATOR))
    assert saved["saved"] is True
    approved = ok("strategy_code_verdict", {"rule_version_id": saved["rule_version_id"], "verdict": "approved"}, caller(BEN))
    assert approved["status"] == "generated" and approved["registry_id"] in default_registry().entries
    bars = sample_series()
    spec = base_spec(strategy_id=approved["registry_id"], bar="5m", start=bars[0].ts, end=bars[-1].ts + timedelta(minutes=5), as_of=bars[-1].ts + timedelta(minutes=5), params={})
    services = runner.Services(db=db, registry=default_registry(), source=source_of({NQ: bars}), curve_dir=tmp_path)
    assert backtest.run(spec, services=services).status == "done"
    space = {"axes": [{"name": "min_range", "kind": "float", "low": 0.0, "high": 2.0, "step": 1.0}]}
    from hokma.kit.backtest_tools import build_space

    found = backtest.optimize(spec, build_space(space), method="grid", n_trials=3, objective="net_pnl", presweep=False, services=services)
    assert found.best is not None
    assert pandas_flow.space_for(approved["registry_id"]) is None                                 # an old strategy has no PARAMS space: the agent gives its own
