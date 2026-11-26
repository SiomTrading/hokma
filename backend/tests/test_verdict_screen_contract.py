"""Contract between the routes of the screen Backtest and `dashboard/src/screens/backtest/types.ts` (SPEC-032, OPT-20, closing V9).

The shapes are read from types.ts itself, so there is no second copy that can drift: for each answer of a real route (over the real runs of H4 on the NQ fixture),
every field that types.ts requires must be there, and no field that types.ts does not declare may be there. The nested records (checks, exclusions, questions,
suggestions, windows, cells) and the closed sets of words (states, statuses, phases) are compared too.

Maintenance: a field added to a route needs its line in types.ts (optional with `?` when the screen can work without it), and the other way round. Two records
are inline in types.ts (`Verdict.thresholds` and `Verdict.evaluating`); their keys are written in this file, next to INLINE.
"""
import re
from pathlib import Path

import pytest
from backtest.support import requires_engine
from factory.test_evidence_bridge import World, world  # noqa: F401  (the world is a fixture of this module too)
from test_verdict_api import OWNER, TOKEN, client, error, evaluated, get, wait_for_job  # noqa: F401  (client is a fixture of this module too)

from hokma.factory import verdict as factory_verdict
from hokma.verdict import service

pytestmark = requires_engine
TYPES = (Path(__file__).resolve().parents[2] / "dashboard" / "src" / "screens" / "backtest" / "types.ts").read_text(encoding="utf-8")
INLINE = {"thresholds": {"line", "version"}, "evaluating": {"done", "total"}}


def fields(name):
    """(required, optional) of an interface of types.ts."""
    body = re.search(r"export interface " + name + r"(?:<[^>]*>)? \{(.*?)\n\}", TYPES, re.S).group(1)
    found = re.findall(r"^  ([a-z0-9_]+)(\??):", body, re.M)
    assert found, name
    return {n for n, q in found if not q}, {n for n, q in found if q}


def words(name):
    """The words of a closed set of types.ts: `export type Name = "a" | "b";`."""
    return set(re.findall(r'"([A-Za-z_]+)"', re.search(r"export type " + name + r" =([^;]*);", TYPES).group(1)))


def same_shape(name, body, *, where=""):
    """Every required field is there and nothing undeclared is."""
    required, optional = fields(name)
    keys = set(body)
    assert required <= keys, f"{where or name}: missing {sorted(required - keys)}"
    assert keys <= required | optional, f"{where or name}: not in types.ts {sorted(keys - required - optional)}"


def verdict_shape(body):
    same_shape("Verdict", body)
    assert body["verdict"] in words("VerdictState") and body["audit_verdict"] in words("VerdictState") and body["line"] in words("LineId")
    assert body["advisory"] is True
    for check in body["checks"]:
        same_shape("VerdictCheck", check)
        assert check["status"] in words("CheckStatus") and check["phase"] in words("PhaseId")
    for entry in body["excluded"]:
        same_shape("VerdictExclusion", entry)
    for item in body["open_questions"]:
        same_shape("OpenQuestion", item)
        assert item["scope"] in ("strategy", "data")
    for item in body["suggestions"]:
        same_shape("Suggestion", item)
        assert item["status"] in words("CheckStatus")
    same_shape("Ideal", body["ideal"])
    assert body["ideal"]["status"] in ("found", "not_found")
    same_shape("VerdictRuns", body["runs"])
    assert set(body["thresholds"]) == INLINE["thresholds"]
    assert body["evaluating"] is None or set(body["evaluating"]) == INLINE["evaluating"]


@pytest.fixture(scope="module")
def first(client, world):
    return evaluated(client, world)


def test_the_strategy_rows_are_the_ones_of_types(client, world, first):
    rows = get(client, "/api/verdict/strategies").json()
    assert rows
    for row in rows:
        same_shape("StrategyRow", row)
        assert row["line"] in words("LineId")


def test_the_verdict_and_every_record_inside_it_are_the_ones_of_types(client, first):
    verdict_shape(first)
    assert first["checks"] and first["open_questions"] is not None


def test_taking_out_putting_back_and_recomputing_answer_with_the_same_shape(client, first):
    red = next(c["key"] for c in first["checks"] if not c["excluded"])
    out = client.post(f"/api/verdict/{first['verdict_id']}/exclude", headers=OWNER, json={"keys": [red], "reason": "contract"})
    assert out.status_code == 200, out.text
    verdict_shape(out.json())
    assert out.json()["excluded"], "an exclusion has to show its record"
    back = client.post(f"/api/verdict/{out.json()['verdict_id']}/include", headers=OWNER, json={"keys": [red]})
    assert back.status_code == 200, back.text
    verdict_shape(back.json())
    for cause in ("thresholds", "answers"):
        again = client.post(f"/api/verdict/{back.json()['verdict_id']}/recompute", headers=OWNER, json={"cause": cause})
        assert again.status_code == 200, again.text
        verdict_shape(again.json())


def test_evaluate_and_its_job_are_the_ones_of_types(client, world, first):
    started = client.post("/api/verdict/cross/1/evaluate", headers=TOKEN)
    assert started.status_code == 202, started.text
    same_shape("EvaluateResponse", started.json())
    assert started.json()["state"] in words("JobState")
    finished = wait_for_job(client, started.json()["job_id"])
    same_shape("JobStatus", finished)
    assert finished["state"] in words("JobState") and finished["kind"] == "verdict" and finished["error"] is None


def test_a_job_that_fails_carries_the_classified_error_the_screen_translates(client, world, monkeypatch):
    def broken(*args, **kwargs):
        raise factory_verdict.VerdictError("bad_input", "the contract test breaks it on purpose")

    monkeypatch.setattr(service.factory_verdict, "evaluate", broken)
    started = client.post("/api/verdict/nowalk/1/evaluate", headers=TOKEN)
    assert started.status_code == 202, started.text
    failed = wait_for_job(client, started.json()["job_id"])
    same_shape("JobStatus", failed)
    assert failed["state"] == "failed" and failed["verdict_id"] is None and set(failed["error"]) == {"code", "message"}
    assert failed["error"]["code"] == "bad_input" and failed["error"]["message"]
    # every code a job can carry has its sentence in the screen: the codes of the backend are the keys of the catalog
    catalog = (Path(__file__).resolve().parents[2] / "dashboard" / "src" / "i18n" / "messages" / "verdict.ts").read_text(encoding="utf-8")
    for code in set(service.TOOL_CLASS) | set(service.HTTP_STATUS) | {"internal", "no_data", "stale"}:
        if code in ("no_verdict", "no_optimization", "no_walk_forward", "data_changed", "no_run", "no_spec", "no_thresholds", "bad_input", "internal", "no_data", "stale",
                    "unavailable", "blocked"):
            assert f'"verdict.error.code.{code}"' in catalog, code


def test_the_questions_the_limits_and_their_history_are_the_ones_of_types(client, first):
    asked = get(client, "/api/verdict/cross/1/questions").json()
    assert asked
    for q in asked:
        same_shape("QuestionState", q)
        assert q["status"] in ("ask", "answered", "na", "auto") and (q["suggestion"] in (None, "yes", "no"))
    now = get(client, "/api/verdict/thresholds/DT").json()
    same_shape("ThresholdSet", now)
    history = get(client, "/api/verdict/thresholds/DT/history").json()
    assert history
    for item in history:
        same_shape("ThresholdSet", item)


def test_what_the_runs_saved_is_the_shape_of_types(client, world, first):
    inputs = world.inputs()
    same_shape("RunSummary", get(client, f"/api/verdict/runs/{inputs.run_id}/summary").json())
    walk = get(client, f"/api/verdict/runs/{inputs.walkforward_id}/walkforward").json()
    same_shape("WalkForwardData", walk)
    assert walk["scheme"] in ("rolling", "anchored", "cpcv") and walk["windows"]
    for window in walk["windows"]:
        same_shape("WalkForwardWindow", window)
    ran = client.post(f"/api/verdict/runs/{inputs.run_id}/montecarlo", headers=OWNER)
    assert ran.status_code == 200, ran.text
    same_shape("MonteCarloData", ran.json())
    assert ran.json()["method"] in ("bootstrap", "permutation", "placebo")
    for figure in ran.json()["figures"].values():
        if isinstance(figure, dict):
            same_shape("Spread", figure)
    saved = get(client, f"/api/verdict/runs/{world.inputs().montecarlo_id}/montecarlo").json()
    same_shape("MonteCarloData", saved)


def test_the_map_and_its_records_are_the_ones_of_types(client, world, first, monkeypatch):
    from hokma.backtest import surface

    def nobody():
        raise surface.ClusterUnavailable("cluster_unavailable: the grouping of hokma.quant.portfolio is not available")

    monkeypatch.setattr(surface, "_default_cluster", nobody)
    body = get(client, f"/api/verdict/optimizations/{world.inputs().optimize_id}/heatmap").json()
    same_shape("HeatmapResponse", body)
    same_shape("HeatmapData", body["heatmap"])
    assert body["heatmap"]["cells"]
    for cell in body["heatmap"]["cells"]:
        same_shape("HeatmapCell", cell)
    for axis in body["axes"]:
        same_shape("OptimizationAxis", axis)
        assert axis["kind"] in ("int", "float", "choice")
    assert body["best"] is None or set(body["best"]) == {"params", "objective"}


def test_a_failure_has_the_body_the_client_reads(client):
    detail = error(get(client, "/api/verdict/cross/9"), 404, "no_run")
    assert set(detail) == {"code", "message"}
    detail = error(get(client, "/api/verdict/thresholds/XX"), 400, "bad_input")
    assert {"code", "message"} <= set(detail) <= {"code", "message", "reason"}
