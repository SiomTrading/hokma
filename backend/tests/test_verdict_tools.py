"""The tools of the verdict of the optimizer (SPEC-032, OPT-19): their registration, the actor that the code sets, the classified errors of 9.2, the shared library
and the limits of the audit. The strategies are the ones of the bridge tests (saved specs of the factory and real runs of H4 over the moves of the NQ fixture);
every call goes through the entry point of the server, `hokma_tools.call`."""
import json
import re
from pathlib import Path

import pytest
from backtest.support import requires_engine
from backtest.test_no_live_path import violations
from e2e_h4.support import call, caller, failed, ok
from factory.test_evidence_bridge import World, world  # noqa: F401  (the world is a fixture of this module too)
from factory.test_spec import text

from hokma import i18n
from hokma.backtest import runner, walkforward
from hokma.factory import library_catalog
from hokma.factory import spec as factory_spec
from hokma.factory import thresholds as th
from hokma.kit import backtest_tools, hokma_tools, verdict_tools
from hokma.kit.tool_registry import ToolError

pytestmark = requires_engine
NAMES = ("optimizer_verdict", "optimizer_verdict_get", "audit_thresholds_get", "library_list", "library_request")
FILE = Path(__file__).resolve().parents[1] / "hokma" / "kit" / "verdict_tools.py"
CREATOR = "strategy-researcher"


@pytest.fixture(scope="module", autouse=True)
def tool_services(world):
    patch = pytest.MonkeyPatch()
    patch.setattr(backtest_tools, "services", lambda: world.services)
    yield
    patch.undo()


def rows(world, sql, **params):
    return world.rows(sql, **params)


def count(world, table):
    return rows(world, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


# ---- registration -----------------------------------------------------------------------------------------------------------------------

def test_the_five_tools_are_published_with_a_strict_schema_and_need_no_database_unit():
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    assert set(NAMES) == set(verdict_tools.TOOLS)
    for name in NAMES:
        assert name in listed and listed[name]["inputSchema"]["additionalProperties"] is False and name in hokma_tools.NO_UNIT
    assert [hokma_tools.TOOLS[n][4] for n in NAMES] == [True, False, False, False, True]       # what saves rows writes; what reads does not


def test_there_is_no_tool_to_take_a_check_out_edit_a_limit_or_answer_a_question():
    assert not [n for n in verdict_tools.TOOLS if re.search(r"exclude|include|answer|restore|copy|set_|save|edit|put|delete|order|exec|desk|broker", n)]
    for name in NAMES:
        for key in hokma_tools.TOOLS[name][1]:
            assert key not in ("path", "file", "folder", "directory", "regex", "pattern", "glob", "url", "db", "actor", "requested_by", "keys", "values", "answers"), (name, key)


def test_the_tool_file_has_no_road_to_a_broker_an_order_or_a_desk():
    source = FILE.read_text(encoding="utf-8")
    assert violations(source, "hokma.kit") == []
    for word in ("hokma.exec", "hokma.broker", "hokma.desks", "hokma.market", "orders_enabled"):
        assert word not in source, word
    assert "\u2014" not in source


def test_a_schema_that_is_not_respected_is_refused_before_anything_runs():
    text_, is_error = hokma_tools.call("optimizer_verdict", {"strategy_id": "cross"}, caller())
    assert is_error and "missing argument" in text_
    text_, is_error = hokma_tools.call("optimizer_verdict_get", {"strategy_id": "cross", "version": 1, "path": "/etc/passwd"}, caller())
    assert is_error and "unknown argument: path" in text_
    text_, is_error = hokma_tools.call("audit_thresholds_get", {"line": "XX"}, caller())
    assert is_error and "line" in text_


# ---- the classes of failure of 9.2 --------------------------------------------------------------------------------------------------------

def test_every_known_failure_has_its_class_and_keeps_its_stable_code_first():
    cases = {"no_run": ("no_data", "no_run"), "no_walk_forward": ("no_data", "no_walk_forward"), "data_changed": ("stale", "data_changed"),
             "no_thresholds": ("bad_input", "no_thresholds"), "no_spec": ("bad_input", "no_spec")}
    for code, (kind, stated) in cases.items():
        error = verdict_tools.classified(walkforward.WalkForwardError(f"{code}: x"))
        assert (error.code, json.loads(error.payload())["message"].split(":")[0]) == (kind, stated), code
    assert verdict_tools.classified(RuntimeError("a bug")) is None


def test_a_missing_spec_a_missing_run_and_a_line_with_no_limits_are_classified(world):
    factory_spec.save_draft(text(strategy_id="norun"), db=world.db)
    factory_spec.save_draft(text(strategy_id="lineless").replace("line: DT", "line: LP"), db=world.db)
    unknown = failed("optimizer_verdict", {"strategy_id": "nothing_saved", "version": 1}, caller(CREATOR))
    assert unknown["error_code"] == "bad_input" and unknown["message"].startswith("no_spec")
    no_run = failed("optimizer_verdict", {"strategy_id": "norun", "version": 1}, caller(CREATOR))
    assert no_run["error_code"] == "no_data" and no_run["message"].startswith("no_run") and no_run["suggestions"] == ["backtest_run"]
    lineless = failed("optimizer_verdict", {"strategy_id": "lineless", "version": 1}, caller(CREATOR))
    assert lineless["error_code"] == "bad_input" and lineless["message"].startswith("no_thresholds")
    assert failed("optimizer_verdict", {"strategy_id": "cross", "version": 0}, caller(CREATOR))["error_code"] == "bad_input"
    assert failed("optimizer_verdict", {"strategy_id": "x\x00y", "version": 1}, caller(CREATOR))["error_code"] == "bad_input"


def test_a_verdict_that_does_not_exist_is_no_data_and_one_of_another_version_is_bad_input(world):
    nothing = failed("optimizer_verdict_get", {"strategy_id": "cpcvdemo", "version": 1}, caller(CREATOR))
    assert nothing["error_code"] == "no_data" and nothing["message"].startswith("no_verdict") and nothing["suggestions"] == ["optimizer_verdict"]
    assert failed("optimizer_verdict_get", {"strategy_id": "cpcvdemo", "version": 1, "verdict_id": 99_999}, caller(CREATOR))["error_code"] == "no_data"


# ---- the verdict ---------------------------------------------------------------------------------------------------------------------------

def test_optimizer_verdict_answers_the_contract_of_9_2_and_writes_the_agent_as_the_actor(world):
    answer = ok("optimizer_verdict", {"strategy_id": "cross", "version": 1}, caller(CREATOR))
    assert answer["advisory"] is True and answer["verdict"] in ("go", "review", "needs_evidence", "no_go") and len(answer["sentences"]) == 3 and len(answer["checks"]) == 24
    assert set(answer) >= {"verdict_id", "strategy_id", "strategy_version", "line", "verdict", "audit_verdict", "advisory", "sentences", "checks", "excluded", "open_questions",
                           "suggestions", "ideal", "thresholds", "runs", "created_at", "notices", "evaluating"}
    assert answer["runs"]["run"] and answer["runs"]["walkforward"] and answer["runs"]["optimize"] and answer["runs"]["montecarlo"] is None
    assert answer["thresholds"] == {"line": "DT", "version": th.current("DT", db=world.db).version} and "plateau_mean" in answer["ideal"]
    assert "monte_carlo_missing" in answer["notices"] and answer["evaluating"] is None
    stored = rows(world, "SELECT actor, strategy_id, strategy_version FROM strategy_verdicts WHERE id = :i", i=answer["verdict_id"])[0]
    assert (stored["actor"], stored["strategy_id"], stored["strategy_version"]) == (CREATOR, "cross", 1)
    anonymous = ok("optimizer_verdict", {"strategy_id": "cross", "version": 1}, caller(None))
    assert rows(world, "SELECT actor FROM strategy_verdicts WHERE id = :i", i=anonymous["verdict_id"])[0]["actor"] == "agent"
    assert not [s for s in answer["suggestions"] if not s["text"] or not s["action"]]


def test_the_verdict_is_read_back_without_running_anything(world):
    made = ok("optimizer_verdict", {"strategy_id": "nowalk", "version": 1}, caller(CREATOR))
    assert {c["key"]: c["status"] for c in made["checks"]}["validation_scheme"] == "unknown" and "no_walk_forward" not in made["notices"]
    before = (count(world, "backtest_runs"), count(world, "backtest_trials"), count(world, "strategy_verdicts"))
    latest = ok("optimizer_verdict_get", {"strategy_id": "nowalk", "version": 1}, caller(CREATOR))
    by_id = ok("optimizer_verdict_get", {"strategy_id": "nowalk", "version": 1, "verdict_id": made["verdict_id"]}, caller(CREATOR))
    assert (count(world, "backtest_runs"), count(world, "backtest_trials"), count(world, "strategy_verdicts")) == before
    assert latest["verdict_id"] == by_id["verdict_id"] == made["verdict_id"] and by_id["sentences"] == made["sentences"] and by_id["checks"] == made["checks"]
    other = failed("optimizer_verdict_get", {"strategy_id": "cross", "version": 1, "verdict_id": made["verdict_id"]}, caller(CREATOR))
    assert other["error_code"] == "bad_input" and "nowalk" in other["message"]


def test_a_verdict_without_a_walk_forward_still_comes_with_the_changes_the_creator_can_use(world):
    answer = ok("optimizer_verdict_get", {"strategy_id": "nowalk", "version": 1}, caller(CREATOR))
    assert any(s["action"] == "run_walk_forward" and s["text"] for s in answer["suggestions"])


# ---- the limits of the audit -----------------------------------------------------------------------------------------------------------------

def test_the_limits_are_the_ones_in_force_with_the_13_measures_and_their_short_description(world):
    answer = ok("audit_thresholds_get", {"line": "DT"}, caller(CREATOR))
    current = th.current("DT", db=world.db)
    assert (answer["line"], answer["version"], answer["values"]) == ("DT", current.version, dict(current.values))
    assert [m["key"] for m in answer["measures"]] == [m.key for m in th.MEASURES] and len(answer["measures"]) == 13
    english = i18n.catalog("en")
    for measure in answer["measures"]:
        assert measure["description"] == english[f"verdict.measure.{measure['key']}"] and measure["description"] and measure["value"] == current.values[measure["key"]]
    assert failed("audit_thresholds_get", {"line": "LP"}, caller(CREATOR))["message"].startswith("no_thresholds")


# ---- the library -------------------------------------------------------------------------------------------------------------------------------

def test_the_library_lists_by_kind_and_status_with_a_limit(world):
    everything = ok("library_list", {"limit": 200}, caller(CREATOR))
    expected = library_catalog.catalog(db=world.db)
    assert everything["total"] == len(expected) and [i["name"] for i in everything["items"]] == [i.name for i in expected][:200]
    ready = ok("library_list", {"kind": "indicator", "status": "ready", "limit": 200}, caller(CREATOR))
    assert {"ema", "sma", "rsi", "atr"} <= {i["name"] for i in ready["items"]} and all(i["kind"] == "indicator" and i["status"] == "ready" for i in ready["items"])
    assert len(ok("library_list", {"limit": 3}, caller(CREATOR))["items"]) == 3 and ok("library_list", {"limit": 3}, caller(CREATOR))["total"] == everything["total"]
    for bad in (0, 201):
        assert failed("library_list", {"limit": bad}, caller(CREATOR))["error_code"] == "bad_input"
    assert call("library_list", {"kind": "nothing"}, caller(CREATOR))[1]


def test_a_request_for_an_indicator_is_saved_with_the_name_of_the_caller_and_listed(world):
    existing = next(i.name for i in library_catalog.catalog(status="h3_only", db=world.db))
    made = ok("library_request", {"name": existing, "description": "The strategy needs this one for its filter of calm markets.", "fields": ["close"], "params": {"length": 14}},
              caller(CREATOR))
    assert made["status"] == "open" and made["requested_by"] == CREATOR and made["notice"] == library_catalog.request_notice(existing) and "H3" in made["notice"]
    saved = rows(world, "SELECT name, requested_by, status, fields_json, params_json FROM library_requests WHERE id = :i", i=made["request_id"])[0]
    assert (saved["name"], saved["requested_by"], saved["status"]) == (existing, CREATOR, "open") and json.loads(saved["fields_json"]) == ["close"]
    new = ok("library_request", {"name": "calm_ratio", "description": "Share of bars whose range is under half the average range of the last days.", "fields": ["high", "low"],
                                 "params": {"length": 20}}, caller(CREATOR))
    assert "notice" not in new and new["request_id"] != made["request_id"]
    listed = ok("library_list", {"status": "requested", "limit": 200}, caller(CREATOR))
    assert {"calm_ratio"} <= {i["name"] for i in listed["items"]} and all(i["status"] == "requested" for i in listed["items"])


def test_a_request_that_is_not_valid_is_bad_input_and_writes_nothing(world):
    before = count(world, "library_requests")
    ready = failed("library_request", {"name": "ema", "description": "I want the exponential average that already exists here.", "fields": ["close"], "params": {}}, caller(CREATOR))
    assert ready["error_code"] == "bad_input" and "already exists" in ready["message"]
    for bad in ({"description": "too short"}, {"fields": ["close", "close"]}, {"fields": ["price"]}, {"params": {"length": [1]}}, {"name": ""}, {"name": "x" * 49},
                {"bank_version_id": 0}):
        args = {"name": "other_one", "description": "A long enough description of what the indicator measures.", "fields": ["close"], "params": {}, **bad}
        assert failed("library_request", args, caller(CREATOR))["error_code"] == "bad_input", bad
    assert count(world, "library_requests") == before


def test_ids_of_the_service_are_taken_from_the_state_and_not_from_the_arguments(world):
    for name in ("optimizer_verdict", "optimizer_verdict_get"):
        assert set(hokma_tools.TOOLS[name][1]) <= {"strategy_id", "version", "verdict_id"}
    assert set(hokma_tools.TOOLS["library_request"][1]) == {"name", "description", "fields", "params", "bank_version_id"}
    assert runner is not None and ToolError is not None
