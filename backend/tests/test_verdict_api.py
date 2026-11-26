"""The routes of the screen Backtest (SPEC-032, OPT-20): the token, the confirmation of the owner on every route that writes, the shapes that the screen reads
(`dashboard/src/screens/backtest/types.ts`), the queue of "evaluate" with its progress, the report with its header, the map and what the runs saved. The strategies
are the ones of the bridge tests (saved specs of the factory, real runs of H4 over the moves of the NQ fixture); every expected value comes from the saved rows or from
the module that wrote them."""
import json
import re
import threading
import time
from pathlib import Path

import pytest
from backtest.support import requires_engine
from backtest.test_no_live_path import violations
from factory.test_evidence_bridge import World, world  # noqa: F401  (the world is a fixture of this module too)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from permissions_support import OWNER_NONCE, OWNER_ONCE

from hokma.backtest import optimize, results, surface, walkforward
from hokma.core import session
from hokma.factory import audit
from hokma.factory import evidence_bridge as eb
from hokma.factory import library_catalog, questions
from hokma.factory import thresholds as th
from hokma.kit import backtest_tools
from hokma.quant.metrics import montecarlo
from hokma.rpc.ws import SESSION_TOKEN
from hokma.verdict import api, service

pytestmark = requires_engine
ROOT = Path(__file__).resolve().parents[2]
TOKEN = {"X-Hokma-Token": SESSION_TOKEN}
OWNER = {**TOKEN, "X-Hokma-Owner": OWNER_NONCE}
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"


@pytest.fixture(scope="module")
def client(world):
    patch = pytest.MonkeyPatch()
    patch.setattr(backtest_tools, "services", lambda: world.services)
    session.forget_owner_for_tests()
    session.arm_owner_secret_for_tests(OWNER_ONCE)
    assert session.register_owner(OWNER_NONCE, OWNER_ONCE) == session.REGISTERED
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as c:
        yield c
    patch.undo()


def get(client, path, headers=TOKEN, **kw):
    return client.get(path, headers=headers, **kw)


def error(response, status, code):
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code and detail["message"], detail
    return detail


def wait_for_job(client, job_id, *, seconds=180):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        body = get(client, f"/api/verdict/jobs/{job_id}").json()
        if body["state"] in ("done", "failed"):
            return body
        time.sleep(0.2)
    raise AssertionError("the evaluation did not finish")


def interface(name):
    """The fields of an interface of types.ts: (required, optional)."""
    source = (ROOT / "dashboard" / "src" / "screens" / "backtest" / "types.ts").read_text(encoding="utf-8")
    body = re.search(r"export interface " + name + r"(?:<[^>]*>)? \{(.*?)\n\}", source, re.S).group(1)
    fields = re.findall(r"^  ([a-z_]+)(\??):", body, re.M)
    return [n for n, q in fields if not q], [n for n, q in fields if q]


def rows(world, sql, **params):
    return world.rows(sql, **params)


def count(world, table):
    return rows(world, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def evaluated(client, world, strategy="cross"):
    """The first evaluation of a strategy through the queue; the verdict that it made."""
    started = client.post(f"/api/verdict/{strategy}/1/evaluate", headers=TOKEN)
    assert started.status_code == 202, started.text
    done = wait_for_job(client, started.json()["job_id"])
    assert done["state"] == "done" and done["error"] is None, done
    return get(client, f"/api/verdict/{strategy}/1").json()


# ---- the door: token, owner, order of the routes ----------------------------------------------------------------------------------------

def test_the_router_is_part_of_the_application():
    from hokma.core.app import create_app

    paths = {route.path for route in create_app().routes}
    assert {"/api/verdict/strategies", "/api/verdict/{strategy_id}/{version}", "/api/verdict/runs/{run_id}/summary", "/api/verdict/thresholds/{line}"} <= paths


def test_every_route_needs_the_token(client):
    for path in ("/api/verdict/strategies", "/api/verdict/cross/1", "/api/verdict/library", "/api/verdict/thresholds/DT", "/api/verdict/runs/1/report"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"X-Hokma-Token": "wrong"}).status_code == 401, path
    assert client.post("/api/verdict/cross/1/evaluate").status_code == 401
    assert get(client, "/api/verdict/library", headers={}, params={"token": SESSION_TOKEN}).status_code == 200


OWNER_CALLS = [
    ("post", "/api/verdict/1/exclude", {"keys": ["pbo"]}),
    ("post", "/api/verdict/1/include", {"keys": ["pbo"]}),
    ("post", "/api/verdict/1/recompute", {"cause": "answers"}),
    ("post", "/api/verdict/cross/1/answers", {"answers": [{"key": "survivorship_free", "value": "yes"}]}),
    ("put", "/api/verdict/thresholds/DT", {"values": {}}),
    ("post", "/api/verdict/thresholds/DT/restore/1", None),
    ("post", "/api/verdict/thresholds/LP/copy-from/SWC", None),
    ("post", "/api/verdict/runs/1/montecarlo", None),
]


@pytest.mark.parametrize("method, path, body", OWNER_CALLS)
def test_a_route_that_writes_needs_the_confirmation_of_the_owner(client, world, method, path, body):
    before = (count(world, "strategy_verdicts"), count(world, "audit_answers"), count(world, "audit_threshold_sets"), count(world, "backtest_runs"))
    for headers in (TOKEN, {**TOKEN, "X-Hokma-Owner": ""}, {**TOKEN, "X-Hokma-Owner": "f" * 32}, {**TOKEN, "X-Hokma-Owner": "not a nonce"}):
        answer = getattr(client, method)(path, headers=headers, **({} if body is None else {"json": body}))
        error(answer, 403, "owner_confirmation_required")
    assert (count(world, "strategy_verdicts"), count(world, "audit_answers"), count(world, "audit_threshold_sets"), count(world, "backtest_runs")) == before


# ---- the lists -------------------------------------------------------------------------------------------------------------------------------

def test_the_strategies_with_a_finished_run_come_with_their_line_and_no_verdict_yet(client):
    body = get(client, "/api/verdict/strategies").json()
    required, _ = interface("StrategyRow")
    assert [r["strategy_id"] for r in body] == ["cpcvdemo", "cross", "nowalk"] and all(set(r) == set(required) for r in body)
    assert all(r["version"] == 1 and r["line"] == "DT" and r["verdict"] is None and r["verdict_id"] is None and r["evaluated_at"] is None for r in body)


def test_a_version_with_no_verdict_or_no_run_says_which(client):
    error(get(client, "/api/verdict/cross/1"), 404, "no_verdict")
    error(get(client, "/api/verdict/nothing/1"), 404, "no_run")
    error(get(client, "/api/verdict/cross/7"), 404, "no_run")
    error(get(client, "/api/verdict/jobs/9999"), 404, "no_job")


# ---- evaluate: the queue, the progress and the shape of the verdict ---------------------------------------------------------------------------------

def test_evaluate_goes_to_the_queue_and_the_verdict_has_the_shape_the_screen_reads(client, world):
    started = client.post("/api/verdict/cross/1/evaluate", headers=TOKEN)
    assert started.status_code == 202 and set(started.json()) == {"job_id", "state"}
    finished = wait_for_job(client, started.json()["job_id"])
    assert finished["state"] == "done" and finished["error"] is None and finished["total"] >= 1 and finished["done"] == finished["total"] and finished["verdict_id"]
    verdict = get(client, "/api/verdict/cross/1").json()
    required, optional = interface("Verdict")
    assert set(required) <= set(verdict) and {"notices", "evaluating"} <= set(verdict) and set(optional) <= set(verdict)
    assert verdict["verdict_id"] == finished["verdict_id"] and verdict["evaluating"] is None and verdict["advisory"] is True
    assert len(verdict["sentences"]) == 3 and len(verdict["checks"]) == 24 and set(interface("Ideal")[0]) <= set(verdict["ideal"]) and "plateau_mean" in verdict["ideal"]
    assert verdict["runs"] == {"run": world.inputs().run_id, "walkforward": world.inputs().walkforward_id, "optimize": world.inputs().optimize_id, "montecarlo": None}
    assert "monte_carlo_missing" in verdict["notices"]
    stored = rows(world, "SELECT actor FROM strategy_verdicts WHERE id = :i", i=verdict["verdict_id"])[0]
    assert stored["actor"] == "owner"
    listed = {r["strategy_id"]: r for r in get(client, "/api/verdict/strategies").json()}
    assert (listed["cross"]["verdict"], listed["cross"]["verdict_id"]) == (verdict["verdict"], verdict["verdict_id"]) and listed["cross"]["evaluated_at"] == verdict["created_at"]
    assert listed["nowalk"]["verdict"] is None


def test_the_language_of_the_panel_decides_the_language_of_the_sentences(client):
    spanish = get(client, "/api/verdict/cross/1", headers={**TOKEN, "Accept-Language": "es-CO,es;q=0.9"}).json()
    english = get(client, "/api/verdict/cross/1", headers={**TOKEN, "Accept-Language": "en-US"}).json()
    assert spanish["sentences"][0].startswith("Sugerencia") and english["sentences"][0].startswith("Suggestion") and spanish["checks"] == english["checks"]


def test_the_progress_of_the_neighbors_is_in_the_job_and_in_the_verdict_while_it_runs(client, world, monkeypatch):
    gate = threading.Semaphore(0)
    real = eb._saved

    def held(*args, **kwargs):
        assert gate.acquire(timeout=120), "the test did not release the neighbor"
        return real(*args, **kwargs)

    monkeypatch.setattr(eb, "_saved", held)
    first = client.post("/api/verdict/cross/1/evaluate", headers=TOKEN).json()
    again = client.post("/api/verdict/cross/1/evaluate", headers=TOKEN).json()
    assert again["job_id"] == first["job_id"]                                                      # an evaluation that is open is not queued twice
    end = time.monotonic() + 60
    while get(client, f"/api/verdict/jobs/{first['job_id']}").json()["total"] == 0 and time.monotonic() < end:
        time.sleep(0.05)
    running = get(client, f"/api/verdict/jobs/{first['job_id']}").json()
    assert running["state"] == "running" and running["done"] == 0 and running["total"] >= 2
    assert get(client, "/api/verdict/cross/1").json()["evaluating"] == {"done": 0, "total": running["total"]}
    gate.release()
    end = time.monotonic() + 60
    while get(client, f"/api/verdict/jobs/{first['job_id']}").json()["done"] < 1 and time.monotonic() < end:
        time.sleep(0.05)
    assert get(client, "/api/verdict/cross/1").json()["evaluating"] == {"done": 1, "total": running["total"]}
    gate.release(1000)
    assert wait_for_job(client, first["job_id"])["state"] == "done"
    assert get(client, "/api/verdict/cross/1").json()["evaluating"] is None


def test_an_evaluation_that_cannot_start_answers_at_once_with_the_reason(client, world):
    error(client.post("/api/verdict/nothing/1/evaluate", headers=TOKEN), 404, "no_spec")
    from hokma.factory import spec as factory_spec
    from factory.test_spec import text

    factory_spec.save_draft(text(strategy_id="lineless").replace("line: DT", "line: LP"), db=world.db)
    detail = error(client.post("/api/verdict/lineless/1/evaluate", headers=TOKEN), 400, "bad_input")
    assert detail["reason"] == "no_thresholds"
    factory_spec.save_draft(text(strategy_id="norun"), db=world.db)
    error(client.post("/api/verdict/norun/1/evaluate", headers=TOKEN), 404, "no_run")


# ---- exclude, include and recompute -----------------------------------------------------------------------------------------------------------------------

def test_taking_a_check_out_and_putting_it_back_runs_nothing_and_leaves_the_audit_verdict_alone(client, world):
    base = get(client, "/api/verdict/cross/1").json()
    red = next(c["key"] for c in base["checks"] if c["status"] == "fail" and not c["excluded"])
    before = (count(world, "backtest_runs"), count(world, "backtest_trials"))
    out = client.post(f"/api/verdict/{base['verdict_id']}/exclude", headers=OWNER, json={"keys": [red], "reason": "not for this strategy"})
    assert out.status_code == 200, out.text
    body = out.json()
    entry = next(e for e in body["excluded"] if e["key"] == red)
    assert (entry["by"], entry["reason"]) == ("owner", "not for this strategy") and next(c for c in body["checks"] if c["key"] == red)["excluded"] is True
    assert body["audit_verdict"] == base["audit_verdict"] and body["verdict_id"] != base["verdict_id"] and set(interface("Verdict")[0]) <= set(body)
    back = client.post(f"/api/verdict/{body['verdict_id']}/include", headers=OWNER, json={"keys": [red]}).json()
    assert red not in {e["key"] for e in back["excluded"]} and back["verdict"] == base["verdict"]
    assert (count(world, "backtest_runs"), count(world, "backtest_trials")) == before
    assert get(client, "/api/verdict/cross/1").json()["verdict_id"] == back["verdict_id"]


def test_a_check_that_does_not_exist_or_is_not_out_is_bad_input(client):
    latest = get(client, "/api/verdict/cross/1").json()
    error(client.post(f"/api/verdict/{latest['verdict_id']}/exclude", headers=OWNER, json={"keys": ["nothing"]}), 400, "bad_input")
    error(client.post(f"/api/verdict/{latest['verdict_id']}/include", headers=OWNER, json={"keys": ["pbo"]}), 400, "bad_input")
    error(client.post(f"/api/verdict/{latest['verdict_id']}/recompute", headers=OWNER, json={"cause": "whim"}), 400, "bad_input")
    error(client.post("/api/verdict/999999/exclude", headers=OWNER, json={"keys": ["pbo"]}), 404, "no_verdict")
    assert client.post(f"/api/verdict/{latest['verdict_id']}/exclude", headers=OWNER, json={"keys": []}).status_code == 422


# ---- the questions ----------------------------------------------------------------------------------------------------------------------------------------

def test_the_questions_are_listed_even_when_the_bars_cannot_be_read(client, world, monkeypatch):
    from hokma.backtest.runner import StageError

    def no_bars(spec):
        raise StageError("data", "no_data", "no contract of NQ has bars in this period")

    monkeypatch.setattr(backtest_tools, "services", lambda: type("NoBars", (), {"database": world.services.database, "curves": world.services.curves, "load": staticmethod(no_bars)})())
    answer = get(client, "/api/verdict/cross/1/questions")
    assert answer.status_code == 200, answer.text
    listed = {q["key"]: q for q in answer.json()}
    assert listed["gaps_checked"]["status"] == "ask" and listed["gaps_checked"]["suggestion"] is None


def test_the_questions_are_read_and_the_answers_are_new_rows_that_the_next_verdict_uses(client, world):
    asked = get(client, "/api/verdict/cross/1/questions").json()
    required, _ = interface("QuestionState")
    assert [q["key"] for q in asked] == [s.key for s in questions.states("cross", 1, services=world.services)] and all(set(required) <= set(q) for q in asked)
    open_one = next(q for q in asked if q["status"] == "ask")
    before = count(world, "audit_answers")
    wrong = client.post("/api/verdict/cross/1/answers", headers=OWNER, json={"answers": [{"key": open_one["key"], "value": "yes"}, {"key": "nothing", "value": "yes"}]})
    error(wrong, 400, "bad_input")
    assert count(world, "audit_answers") == before                                                   # nothing was written: every answer is checked first
    saved = client.post("/api/verdict/cross/1/answers", headers=OWNER, json={"answers": [{"key": open_one["key"], "value": "yes", "note": "checked by hand"}]})
    assert saved.status_code == 200 and saved.json()["saved"] == 1 and count(world, "audit_answers") > before
    now = {q["key"]: q for q in saved.json()["questions"]}
    assert now[open_one["key"]]["status"] == "answered" and now[open_one["key"]]["answer"] == "yes" and now[open_one["key"]]["answer_id"]
    assert get(client, "/api/verdict/cross/1/questions").json() == saved.json()["questions"]
    latest = get(client, "/api/verdict/cross/1").json()
    recomputed = client.post(f"/api/verdict/{latest['verdict_id']}/recompute", headers=OWNER, json={"cause": "answers"})
    assert recomputed.status_code == 200 and open_one["key"] not in {q["key"] for q in recomputed.json()["open_questions"] if q["subject"] == open_one["subject"]}
    error(get(client, "/api/verdict/nothing/1/questions"), 404, "no_spec")


# ---- the limits of the audit -----------------------------------------------------------------------------------------------------------------------------

def test_the_limits_are_read_changed_restored_and_copied_with_every_version_kept(client, world):
    now = get(client, "/api/verdict/thresholds/DT").json()
    required, _ = interface("ThresholdSet")
    assert set(required) <= set(now) and now["values"] == dict(th.current("DT", db=world.db).values) and now["line"] == "DT"
    history = get(client, "/api/verdict/thresholds/DT/history").json()
    assert [s["id"] for s in history] == [s.id for s in th.history("DT", db=world.db)] and now["id"] in {s["id"] for s in history}
    wider = {**now["values"], "pbo_reject": 0.6}
    changed = client.put("/api/verdict/thresholds/DT", headers=OWNER, json={"values": wider, "reason": "this line tolerates more"})
    assert changed.status_code == 200, changed.text
    new = changed.json()
    assert (new["version"], new["changed_by"], new["reason"], new["values"]["pbo_reject"]) == (now["version"] + 1, "owner", "this line tolerates more", 0.6)
    same = client.put("/api/verdict/thresholds/DT", headers=OWNER, json={"values": wider}).json()
    assert same["id"] == new["id"] and same["unchanged"] is True                                          # an identical version is not written again
    before = count(world, "audit_threshold_sets")
    for values in ({**wider, "pbo_reject": 7}, {**wider, "unknown_measure": 1}, {"pbo_reject": 0.5}):
        error(client.put("/api/verdict/thresholds/DT", headers=OWNER, json={"values": values}), 400, "bad_input")
    assert count(world, "audit_threshold_sets") == before
    restored = client.post(f"/api/verdict/thresholds/DT/restore/{now['id']}", headers=OWNER).json()
    assert restored["version"] == new["version"] + 1 and restored["restored_from"] == now["id"] and restored["values"] == now["values"]
    assert sorted(s["version"] for s in get(client, "/api/verdict/thresholds/DT/history").json()) == list(range(1, restored["version"] + 1))   # every version is kept
    error(client.post("/api/verdict/thresholds/DT/restore/999999", headers=OWNER), 400, "bad_input")
    error(get(client, "/api/verdict/thresholds/XX"), 400, "bad_input")


def test_a_line_with_no_limits_says_so_and_can_copy_another(client):
    detail = error(get(client, "/api/verdict/thresholds/LP"), 400, "bad_input")
    assert detail["reason"] == "no_thresholds"
    copied = client.post("/api/verdict/thresholds/LP/copy-from/SWC", headers=OWNER)
    assert copied.status_code == 200 and copied.json()["line"] == "LP" and copied.json()["values"] == get(client, "/api/verdict/thresholds/SWC").json()["values"]
    assert get(client, "/api/verdict/thresholds/LP").json()["version"] == 1
    error(client.post("/api/verdict/thresholds/LP/copy-from/LP", headers=OWNER), 400, "bad_input")


def test_new_limits_change_the_verdict_only_when_the_owner_asks_for_the_recalculation(client, world):
    before = get(client, "/api/verdict/cross/1").json()
    current = th.current("DT", db=world.db)
    assert before["thresholds"]["version"] < current.version                                         # saving the limits did not touch the verdict
    again = client.post(f"/api/verdict/{before['verdict_id']}/recompute", headers=OWNER, json={"cause": "thresholds"})
    assert again.status_code == 200, again.text
    assert again.json()["thresholds"] == {"line": "DT", "version": current.version} and again.json()["verdict_id"] != before["verdict_id"]


# ---- what the runs saved ------------------------------------------------------------------------------------------------------------------------------------

def test_the_report_is_html_with_the_header_that_keeps_it_closed(client, world):
    run_id = world.inputs().run_id
    answer = get(client, f"/api/verdict/runs/{run_id}/report")
    assert answer.status_code == 200 and answer.headers["content-type"].startswith("text/html")
    assert answer.headers["content-security-policy"] == CSP and answer.headers["x-content-type-options"] == "nosniff" and answer.headers["cache-control"] == "no-store"
    expected = results.report_html(world.result_of(run_id), title=f"cross version 1 run {run_id}")
    assert answer.text == expected and "<script" not in answer.text.lower() and "<svg" in answer.text
    error(get(client, f"/api/verdict/runs/{world.inputs().walkforward_id}/report"), 404, "no_run")
    error(get(client, "/api/verdict/runs/999999/report"), 404, "no_run")


def test_the_summary_is_the_one_the_run_saved(client, world):
    run_id = world.inputs().run_id
    body = get(client, f"/api/verdict/runs/{run_id}/summary").json()
    saved = json.loads(rows(world, "SELECT summary_json FROM backtest_runs WHERE id = :i", i=run_id)[0]["summary_json"])
    required, _ = interface("RunSummary")
    assert body == saved and set(required) <= set(body) and "trades" not in body and "equity" not in body
    error(get(client, f"/api/verdict/runs/{world.inputs().optimize_id}/summary"), 404, "no_run")


def test_the_walk_forward_comes_with_its_own_id_and_the_dates_of_its_windows(client, world):
    inputs = world.inputs()
    body = get(client, f"/api/verdict/runs/{inputs.walkforward_id}/walkforward").json()
    saved = json.loads(rows(world, "SELECT summary_json FROM backtest_runs WHERE id = :i", i=inputs.walkforward_id)[0]["summary_json"])["result"]
    assert body["run_id"] == inputs.walkforward_id and saved["run_id"] is None
    assert {k: v for k, v in body.items() if k not in ("run_id", "windows")} == {k: v for k, v in saved.items() if k not in ("run_id", "windows")}
    for found, original in zip(body["windows"], saved["windows"]):
        assert {k: v for k, v in found.items() if k not in ("train_dates", "test_dates")} == original
        first, end = original["train"]
        assert found["train_dates"] == [world.bars[first].ts.date().isoformat(), world.bars[end - 1].ts.date().isoformat()]
        assert found["test_dates"] == [[world.bars[a].ts.date().isoformat(), world.bars[b - 1].ts.date().isoformat()] for a, b in original["test_blocks"]]
    assert set(interface("WalkForwardData")[0]) <= set(body)
    error(get(client, f"/api/verdict/runs/{inputs.run_id}/walkforward"), 404, "no_run")


def test_without_the_bars_the_walk_forward_is_still_answered_with_row_numbers(client, world, monkeypatch):
    inputs = world.inputs()
    monkeypatch.setattr(backtest_tools, "services", lambda: type("NoBars", (), {"database": world.services.database, "curves": world.services.curves,
                                                                              "load": staticmethod(lambda spec: (_ for _ in ()).throw(LookupError("no bars")))})())
    body = get(client, f"/api/verdict/runs/{inputs.walkforward_id}/walkforward").json()
    assert body["windows"] and not any("train_dates" in w or "test_dates" in w for w in body["windows"]) and body["run_id"] == inputs.walkforward_id


def test_the_map_is_a_svg_with_its_cells_its_best_point_and_the_warnings_of_the_analysis(client, world, monkeypatch):
    def nobody():
        raise surface.ClusterUnavailable("cluster_unavailable: the grouping of hokma.quant.portfolio is not available")

    monkeypatch.setattr(surface, "_default_cluster", nobody)
    opt_id = world.inputs().optimize_id
    answer = get(client, f"/api/verdict/optimizations/{opt_id}/heatmap")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    found = optimize.load(world.db, opt_id)
    drawn = surface.heatmap(found, "fast", "slow")
    assert set(interface("HeatmapResponse")[0]) <= set(body) and body["svg"] == drawn.svg() and body["heatmap"] == drawn.to_dict()
    assert [a["name"] for a in body["axes"]] == ["fast", "slow"] and body["best"] == {"params": dict(found.best.params), "objective": found.best.objective}
    assert (body["n_trials"], body["cache_hits"], body["edge_of_space"]) == (found.n_trials, found.cache_hits, list(found.edge_of_space))
    assert body["failed"] == sum(t.status == "failed" for t in found.trials) and "analysis_part_missing:plateaus" in body["warnings"]
    layout = body["layout"]
    width, height = (int(re.search(rf'{k}="(\d+)"', body["svg"]).group(1)) for k in ("width", "height"))                  # the geometry the screen draws on top of
    assert width == layout["left"] + len(drawn.x_values) * layout["cell"] + layout["gap"] and height == layout["top"] + len(drawn.y_values) * layout["cell"] + layout["bottom"]
    swapped = get(client, f"/api/verdict/optimizations/{opt_id}/heatmap", params={"x": "slow", "y": "fast", "metric": found.objective}).json()
    assert swapped["heatmap"] == surface.heatmap(found, "slow", "fast", metric=found.objective).to_dict() and swapped["heatmap"]["x"] == "slow"
    error(get(client, f"/api/verdict/optimizations/{opt_id}/heatmap", params={"x": "fast", "y": "fast"}), 400, "bad_input")
    error(get(client, f"/api/verdict/optimizations/{opt_id}/heatmap", params={"x": "nothing"}), 400, "bad_input")
    error(get(client, f"/api/verdict/optimizations/{opt_id}/heatmap", params={"metric": "something_else"}), 400, "bad_input")
    error(get(client, f"/api/verdict/optimizations/{world.inputs().run_id}/heatmap"), 404, "no_optimization")
    error(get(client, "/api/verdict/optimizations/999999/heatmap"), 404, "no_optimization")


def test_monte_carlo_is_missing_then_runs_with_the_defaults_of_h4_and_is_found_after(client, world):
    inputs = world.inputs()
    assert inputs.montecarlo_id is None
    error(get(client, "/api/verdict/runs/999999/montecarlo"), 404, "no_run")
    ran = client.post(f"/api/verdict/runs/{inputs.run_id}/montecarlo", headers=OWNER)
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert (body["run_id"], body["method"], body["n"], body["seed"]) == (inputs.run_id, "bootstrap", montecarlo.DEFAULT_RUNS, montecarlo.DEFAULT_SEED)
    assert set(interface("MonteCarloData")[0]) <= set(body) and {"sharpe", "cagr"} <= set(body["figures"]) and {"percentile_5", "median", "percentile_95"} <= set(body["figures"]["sharpe"])
    saved_id = world.inputs().montecarlo_id
    assert saved_id is not None and get(client, f"/api/verdict/runs/{saved_id}/montecarlo").json() == body
    error(client.post(f"/api/verdict/runs/{inputs.walkforward_id}/montecarlo", headers=OWNER), 404, "no_run")
    fresh = evaluated(client, world)
    assert fresh["runs"]["montecarlo"] == saved_id and "monte_carlo_missing" not in fresh["notices"]


def test_the_library_is_the_catalog_with_its_filters(client, world):
    body = get(client, "/api/verdict/library").json()
    assert body == [i.as_dict() for i in library_catalog.catalog(db=world.db)] and {"ready", "h3_only"} <= {i["status"] for i in body}
    ready = get(client, "/api/verdict/library", params={"kind": "indicator", "status": "ready"}).json()
    assert ready and all(i["kind"] == "indicator" and i["status"] == "ready" for i in ready)
    error(get(client, "/api/verdict/library", params={"kind": "nothing"}), 400, "bad_input")


# ---- the files ---------------------------------------------------------------------------------------------------------------------------------------------

def test_the_route_files_have_no_road_to_a_broker_and_only_the_routes_read_the_verdict_of_the_table():
    for name in ("api.py", "service.py", "__init__.py"):
        source = (ROOT / "backend" / "hokma" / "verdict" / name).read_text(encoding="utf-8")
        assert violations(source, "hokma.verdict") == [] and "\u2014" not in source, name
        for word in ("hokma.exec", "hokma.broker", "hokma.desks", "hokma.market", "orders_enabled"):
            assert word not in source, (name, word)
    assert "strategy_verdicts" not in (ROOT / "backend" / "hokma" / "verdict" / "service.py").read_text(encoding="utf-8")
    assert "strategy_verdicts" not in (ROOT / "backend" / "hokma" / "kit" / "verdict_tools.py").read_text(encoding="utf-8")


def test_the_routes_that_write_are_the_ones_that_ask_for_the_owner():
    owned = {(m, r.path) for r in api.router.routes for m in r.methods if any(d.call is api.owner_required for d in r.dependant.dependencies)}
    free = {(m, r.path) for r in api.router.routes for m in r.methods} - owned
    assert {m for m, _ in owned} <= {"POST", "PUT"} and len(owned) == len(OWNER_CALLS)
    assert {p for m, p in free if m != "GET"} == {"/api/verdict/{strategy_id}/{version}/evaluate"}
    assert audit is not None and walkforward is not None and service is not None
