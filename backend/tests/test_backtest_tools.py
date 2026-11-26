"""The tools of the backtest service (SPEC-020, section 4.11): registration, sizes, classified errors, ids only, and one call of each through the server entry point."""
import json
import re
from pathlib import Path

import pytest
from backtest.support import requires_engine
from backtest.test_no_live_path import violations
from dbsupport import db, env  # noqa: F401  (fixtures)
from backtest.session_support import session_bars
from e2e_h4.support import SPACE, bars, call, caller, failed, ok, run_args, use_services

from hokma.backtest import _lib, trials
from hokma.kit import backtest_tools, hokma_tools
from hokma.kit.tool_registry import CODES, ToolError

PREMIUM_NAMES = ("premium_calibrate", "premium_switch_state")
NAMES = ("backtest_run", "backtest_status", "backtest_result", "backtest_optimize", "backtest_walkforward", "backtest_montecarlo", "backtest_evidence")
ROOT = Path(__file__).resolve().parents[2]
FILE = ROOT / "backend" / "hokma" / "kit" / "backtest_tools.py"


def test_the_seven_tools_are_published_with_a_strict_schema_and_need_no_database_unit():
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    assert set(NAMES) | set(PREMIUM_NAMES) == set(backtest_tools.TOOLS)
    for name in (*NAMES, *PREMIUM_NAMES):
        assert name in listed and listed[name]["inputSchema"]["additionalProperties"] is False and name in hokma_tools.NO_UNIT
    assert [hokma_tools.TOOLS[n][4] for n in NAMES] == [True, False, False, True, True, True, False]       # what saves rows writes; what reads does not
    assert not [n for n in NAMES if re.search(r"order|exec|desk|broker|submit|approve|flatten", n)]


def test_the_tool_file_has_no_road_to_a_broker_an_order_or_a_desk():
    assert violations(FILE.read_text(encoding="utf-8"), "hokma.kit") == []
    text = FILE.read_text(encoding="utf-8")
    for word in ("hokma.exec", "hokma.broker.service", "hokma.broker.sim", "hokma.broker.schwab", "hokma.market", "orders_enabled", "record_calibration", "retire_calibration", "premium_thresholds"):
        assert word not in text, word


def test_a_schema_that_is_not_respected_is_refused_before_anything_runs(db):
    text, is_error = hokma_tools.call("backtest_run", {"strategy_id": "cross"}, caller())
    assert is_error and "missing argument" in text
    text, is_error = hokma_tools.call("backtest_status", {"run_id": 1, "path": "/etc/passwd"}, caller())
    assert is_error and "unknown argument: path" in text


def test_the_extra_codes_are_stable_and_the_ones_of_h1_are_kept():
    for code in backtest_tools.EXTRA_CODES:
        assert json.loads(backtest_tools.ClassifiedError(code, "x").payload())["error_code"] == code and code not in CODES
    assert json.loads(backtest_tools.ClassifiedError("no_data", "x").payload())["error_code"] == "no_data"
    with pytest.raises(ValueError):
        backtest_tools.ClassifiedError("invented", "x")
    assert isinstance(backtest_tools.ClassifiedError("forbidden_fill", "x"), ToolError)


def test_a_forbidden_fill_and_a_bar_that_does_not_belong_to_the_line_are_classified(db):
    series = bars(60)
    for fill in ({"trade_on_close": True}, {"model": "best_price"}, {"stop_first": False}, {"gap_fills_at_open": False}, {"next_open_ticks": 0}):
        assert failed("backtest_run", run_args(series, fill=fill, slippage_ticks=1))["error_code"] == "forbidden_fill", fill
    daily = failed("backtest_run", run_args(series, bar="1d"))
    assert daily["error_code"] == "bar_not_allowed_for_line" and "1m" in daily["message"]
    assert failed("backtest_run", run_args(series, fill={"unknown": 1}))["error_code"] == "bad_input"
    assert failed("backtest_run", run_args(series, fill={"next_open_ticks": "1"}))["error_code"] == "bad_input"


def test_ids_and_values_only_no_free_path_and_no_free_pattern(db):
    series = bars(60)
    for bad in ({"instruments": ["../../etc/passwd"]}, {"instruments": ["a b"]}, {"instruments": []}, {"fee_table": "/etc/passwd"}, {"fee_table": "../schwab"},
                {"fee_table": "nope"}, {"params": {"fast": {"x": 1}}}, {"params": {"Bad-Name": 1}}, {"strategy_id": "x\x00y"}, {"capital_usd": -5}, {"capital_usd": True},
                {"start": "yesterday-ish"}, {"seed": -1}, {"slippage_ticks": 1000}, {"bar": "2m"}, {"impact": "x"}, {"session": "ALL"}):
        answer, is_error = call("backtest_run", run_args(series, **bad))
        assert is_error, bad
        if isinstance(answer, dict):
            assert answer["error_code"] == "bad_input", bad
    assert failed("backtest_run", run_args(series, start="2000-01-01", end="2025-01-01"))["error_code"] == "bad_input"
    assert failed("backtest_run", run_args(series, line="XX"))["error_code"] == "bad_input"
    for tool_name in NAMES:                                   # no argument of any tool is a path, a folder or a pattern
        for key in hokma_tools.TOOLS[tool_name][1]:
            assert key not in ("path", "file", "folder", "directory", "regex", "pattern", "glob", "url", "curve_dir", "db")


def test_sizes_are_checked_with_the_limits_of_quant(db):
    from hokma.quant import limits

    series = bars(60)
    too_many = limits.PARAM_CAPS["opt_trials"] + 1
    answer = failed("backtest_optimize", run_args(series, space=SPACE, method="tpe", n_trials=too_many, objective="net_pnl"))
    assert answer["error_code"] == "bad_input" and "opt_trials" in answer["message"]
    grid = failed("backtest_optimize", run_args(series, space=SPACE, method="grid", n_trials=limits.PARAM_CAPS["n_combinations"] + 1, objective="net_pnl"))
    assert grid["error_code"] == "bad_input" and "n_combinations" in grid["message"]
    huge = {"axes": [{"name": "fast", "kind": "int", "low": 1, "high": limits.PARAM_CAPS["n_combinations"] + 5, "step": 1}]}
    assert failed("backtest_optimize", run_args(series, space=huge, method="grid", n_trials=10, objective="net_pnl"))["error_code"] == "bad_input"
    assert failed("backtest_montecarlo", {"run_id": 1, "n": limits.PARAM_CAPS["runs"] + 1, "method": "bootstrap", "seed": 1})["error_code"] == "bad_input"
    assert failed("backtest_walkforward", run_args(series, space=SPACE, scheme="rolling", train=limits.PARAM_CAPS["train_size"] + 1, test=10, purge=1, embargo=1))["error_code"] == "bad_input"
    assert failed("backtest_walkforward", run_args(series, space=SPACE, scheme="rolling", train=10, test=10, purge=limits.PARAM_CAPS["window"] + 1, embargo=1))["error_code"] == "bad_input"
    assert failed("backtest_run", run_args(series, instruments=["CONT:NQ"] * (limits.PARAM_CAPS["n_instruments"] + 1)))["error_code"] == "bad_input"
    for space in ({"axes": []}, {"axes": "x"}, {"axes": [{"name": "fast", "code": "1"}]}, {"axes": [{"name": "fast", "kind": "int", "low": 1, "high": 2}], "extra": 1},
                  {"axes": [{"name": "fast", "kind": "int", "low": 1, "high": 2}], "constraints": [{"left": "fast", "op": "lt", "right": "nope"}]},
                  {"axes": [{"name": "fast", "kind": "int", "low": 1, "high": 2}], "constraints": [{"left": "fast", "op": "eval", "right": 1}]}):
        assert failed("backtest_optimize", run_args(series, space=space, method="grid", n_trials=10, objective="net_pnl"))["error_code"] == "bad_input", space
    text, is_error = call("backtest_optimize", run_args(series, space=SPACE, method="bayes", n_trials=10, objective="net_pnl"))
    assert is_error and "method must be one of" in text                                   # an enum of the schema is refused before the tool runs
    assert failed("backtest_result", {"run_id": 1, "max_trades": 10_000})["error_code"] == "bad_input"


def test_a_missing_run_or_evidence_is_no_data_and_a_machine_without_the_engine_is_unavailable(db, monkeypatch, tmp_path):
    use_services(monkeypatch, bars(60), db, tmp_path)
    assert failed("backtest_status", {"run_id": 99})["error_code"] == "no_data"
    assert failed("backtest_result", {"run_id": 99})["error_code"] == "no_data"
    assert failed("backtest_status", {})["error_code"] == "bad_input" and failed("backtest_status", {"run_id": 1, "job_id": 1})["error_code"] == "bad_input"
    assert failed("backtest_evidence", {"strategy_id": "cross", "version": 1})["error_code"] == "no_data"
    monkeypatch.setattr(_lib, "_IMPORT_ERROR", ImportError("no wheel"))                # a platform without the wheel of the engine
    gone = failed("backtest_run", run_args(bars(60)))
    assert gone["error_code"] == "unavailable" and "engine_unavailable" in gone["message"]
    assert failed("backtest_optimize", run_args(bars(60), space=SPACE, method="grid", n_trials=50, objective="net_pnl"))["error_code"] == "unavailable"


def test_only_a_known_failure_is_classified_and_the_rest_is_a_bug_that_propagates():
    assert backtest_tools.classify(RuntimeError("bug")) is None
    assert backtest_tools.classify(ValueError("no_run: x")).code == "no_data" and backtest_tools.classify(ValueError("data_changed: x")).code == "stale"
    assert backtest_tools.classify(ValueError("plain words")).code == "bad_input"

    @backtest_tools.safe
    def broken(u, c, a):
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        broken(None, None, {})


def test_more_than_the_open_jobs_limit_is_told_to_wait(monkeypatch):
    import threading

    from hokma.quant import limits

    monkeypatch.setattr(backtest_tools, "_SLOTS", threading.BoundedSemaphore(1))
    with backtest_tools._slot():
        with pytest.raises(ToolError) as busy, backtest_tools._slot():
            pass
    assert busy.value.code == "unavailable" and str(limits.MAX_OPEN_JOBS) in busy.value.message
    with backtest_tools._slot():                                                         # the slot is given back
        pass


@requires_engine
def test_each_tool_runs_through_the_entry_point(db, monkeypatch, tmp_path):
    series = bars(1500)
    use_services(monkeypatch, series, db, tmp_path)
    who = caller("hokma")                                                       # a caller that is not Ian runs the tools directly, with no saved spec
    done = ok("backtest_run", run_args(series), who)
    assert done["status"] == "done" and done["trades"] >= 3 and done["run_id"] >= 1 and {"sharpe", "drawdown"} <= set(done["stats"])
    assert done["stats"]["sharpe"]["method"] and done["stats"]["sharpe"]["piece_ids"]                        # the statistics are QuantResult, not engine numbers
    status = ok("backtest_status", {"run_id": done["run_id"]}, who)
    assert (status["kind"], status["status"], status["strategy_id"]) == ("run", "done", "cross")
    read = ok("backtest_result", {"run_id": done["run_id"], "max_trades": 2}, who)
    assert len(read["trade_rows"]) == 2 and read["trades_total"] == done["trades"] and read["equity"]["points"] == 1500
    grid = ok("backtest_optimize", run_args(series, space=SPACE, method="grid", n_trials=50, objective="net_pnl"), who)
    assert grid["status"] == "done" and grid["best"] and grid["run_id"] != done["run_id"] and grid["n_trials"] == trials.count("cross")
    assert failed("backtest_result", {"run_id": grid["run_id"], "report": True}, who)["error_code"] == "bad_input"          # a search has no report
    assert ok("backtest_result", {"run_id": grid["run_id"]}, who)["kind"] == "optimize"
    forward = ok("backtest_walkforward", run_args(series, space=SPACE, scheme="rolling", train=250, test=250, purge=2, embargo=1), who)
    assert forward["status"] == "done" and forward["leakage_free"] is True and forward["windows"] and forward["windows_total"] == len(forward["windows"])
    carlo = ok("backtest_montecarlo", {"run_id": done["run_id"], "n": 200, "method": "placebo", "seed": 5}, who)
    assert carlo["of_run_id"] == done["run_id"] and "run_id" not in carlo and carlo["p_value"] is not None
    assert failed("backtest_montecarlo", {"run_id": grid["run_id"], "n": 10, "method": "placebo", "seed": 5}, who)["error_code"] == "bad_input"
    short = ok("backtest_montecarlo", {"run_id": done["run_id"], "n": 10, "method": "bootstrap", "seed": 5}, who)             # two days of returns: no figure, and it says why
    assert short["figures"]["sharpe"] is None and any("observation" in w for w in short["warnings"])
    bundle = ok("backtest_evidence", {"strategy_id": "cross", "version": 1}, who)
    assert bundle["pbo"] is not None and bundle["content_hash"] and "monte_carlo_missing" not in bundle["warnings"]
    assert bundle["n_trials"] == trials.count("cross") > 9 + 9


@requires_engine
def test_the_report_is_html_with_every_text_escaped_even_a_hostile_strategy_id(db, monkeypatch, tmp_path):
    from backtest.api_support import Cross
    from backtest.runner_support import source_of
    from hokma.backtest.runner import Services
    from hokma.factory.registry import Entry, Registry

    hostile = "<script>alert(1)</script>"
    daily = session_bars("daily")

    def hostile_services():
        registry = Registry()
        registry.add(Entry(hostile, "strategy", Cross, "SWC", "", {"fast": 5, "slow": 20}, "test"))
        return Services(db=db, registry=registry, source=source_of({"CONT:NQ": daily}), curve_dir=tmp_path)

    monkeypatch.setattr(backtest_tools, "services", hostile_services)
    who = caller("hokma")
    done = ok("backtest_run", run_args(daily, strategy_id=hostile, line="SWC", bar="1d", params={"fast": 3, "slow": 8}), who)
    page = ok("backtest_result", {"run_id": done["run_id"], "report": True}, who)
    assert page["format"] == "html" and page["html"].lstrip().lower().startswith("<!doctype html")
    assert "<script>alert(1)</script>" not in page["html"] and "&lt;script&gt;alert(1)&lt;/script&gt;" in page["html"]
