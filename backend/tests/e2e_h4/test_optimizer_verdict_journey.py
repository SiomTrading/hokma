"""OPT-33, the journey of the verdict of the optimizer from end to end (SPEC-032): a strategy of the line DT over the real moves of the NQ fixture of H4, laid on
several days; backtest, grid, walk forward and Monte Carlo through the tools of H4; then the verdict, taking a check out and putting it back, the answers, a change of a
limit with its recalculation, the restore, and the library with a request for an indicator. Everything runs in a data folder of Hokma that is not the user's.

The expected values come from the saved rows and from the modules that wrote them; nothing is calculated here. The facts of the run are printed (`pytest -s`) with the
lines that start with JOURNEY.

Second use, not a test: with `HOKMA_JOURNEY_KEEP=<folder>` set, `test_load_the_nq_run_for_the_screen` leaves the same runs and one evaluated verdict in that folder, so that
Hokma can be started on it (`HOKMA_DATA_DIR=<folder>/data`) and the screen Backtest has something to show. Without the variable that test is skipped."""
import json
import os
import time
from collections import Counter
from pathlib import Path

import pytest
from backtest.support import requires_engine
from e2e_h4.journey_support import STRATEGY, build_runs, point_at, use_nq_services
from e2e_h4.support import caller, ok
from fastapi import FastAPI
from fastapi.testclient import TestClient
from permissions_support import OWNER_NONCE, OWNER_ONCE

from hokma.core import session
from hokma.factory import thresholds as th
from hokma.rpc.ws import SESSION_TOKEN
from hokma.verdict import api

pytestmark = requires_engine
TOKEN = {"X-Hokma-Token": SESSION_TOKEN}
OWNER = {**TOKEN, "X-Hokma-Owner": OWNER_NONCE}
STATES = ("go", "review", "needs_evidence", "no_go")


def say(**facts) -> None:
    print("JOURNEY " + json.dumps(facts, sort_keys=True, default=str))


def rows(db, sql, **params):
    with db.read() as u:
        return u.session.query(sql, params)


def count(db, table: str) -> int:
    return rows(db, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def colors(verdict) -> dict[str, int]:
    return dict(sorted(Counter(c["status"] for c in verdict["checks"]).items()))


@pytest.fixture
def screen(monkeypatch):
    session.forget_owner_for_tests()
    session.arm_owner_secret_for_tests(OWNER_ONCE)
    assert session.register_owner(OWNER_NONCE, OWNER_ONCE) == session.REGISTERED
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        yield client


def evaluate(client, strategy=STRATEGY, version=1, seconds=300):
    started = client.post(f"/api/verdict/{strategy}/{version}/evaluate", headers=TOKEN)
    assert started.status_code == 202, started.text
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        job = client.get(f"/api/verdict/jobs/{started.json()['job_id']}", headers=TOKEN).json()
        if job["state"] in ("done", "failed"):
            assert job["state"] == "done" and job["error"] is None, job
            return job, client.get(f"/api/verdict/{strategy}/{version}", headers=TOKEN).json()
        time.sleep(0.2)
    raise AssertionError("the evaluation did not finish")


def test_the_journey_of_the_verdict_from_the_backtest_to_the_library(tmp_path, monkeypatch, screen):
    begun = time.monotonic()
    point_at(monkeypatch, tmp_path)
    services, bars = use_nq_services(monkeypatch, tmp_path)
    db = services.database()
    made = build_runs(services, bars)
    assert made["run"]["status"] == "done" and made["walk"]["scheme"] == "anchored" and made["carlo"]["method"] == "bootstrap" and made["grid"]["failed_trials"] == 0
    assert made["carlo"]["of_run_id"] == made["run"]["run_id"]
    last = {r["kind"]: r["id"] for r in rows(db, "SELECT kind, MAX(id) AS id FROM backtest_runs WHERE strategy_id = 'cross' GROUP BY kind")}
    ids = {"run": made["run"]["run_id"], "grid": made["grid"]["run_id"], "walk": made["walk"]["run_id"], "carlo": last["montecarlo"]}
    assert ids["grid"] == last["optimize"] and ids["walk"] == last["walkforward"] and ids["run"] == last["run"]
    say(step="runs", ids=ids, seconds=made["took"], bars=len(bars))

    # 1. evaluate: the queue, the job and the verdict that it saved
    runs_before = count(db, "backtest_runs")
    job, first = evaluate(screen)
    assert first["verdict_id"] == job["verdict_id"] and first["advisory"] is True and len(first["checks"]) == 24 and len(first["sentences"]) == 3
    assert first["verdict"] in STATES and first["audit_verdict"] in STATES and first["evaluating"] is None
    assert first["runs"] == {"run": ids["run"], "walkforward": ids["walk"], "optimize": ids["grid"], "montecarlo": ids["carlo"]}
    assert "monte_carlo_missing" not in first["notices"]
    stored = rows(db, "SELECT actor FROM strategy_verdicts WHERE id = :i", i=first["verdict_id"])[0]
    assert stored["actor"] == "owner"
    say(step="evaluate", verdict_id=first["verdict_id"], verdict=first["verdict"], audit_verdict=first["audit_verdict"], colors=colors(first), sentences=first["sentences"],
        open_questions=[q["key"] for q in first["open_questions"]], neighbors_run=count(db, "backtest_runs") - runs_before, notices=first["notices"])

    # what the ideal result said
    ideal = first["ideal"]
    try:
        from hokma.quant.portfolio.cluster import kmeans  # noqa: F401
        clusters = True
    except ImportError:
        clusters = False
    assert ideal["status"] in ("found", "not_found")
    if not clusters:
        assert ideal["status"] == "not_found" and ideal["reason"] == "plateaus_unavailable"
    say(step="ideal", ideal=ideal, kmeans_available=clusters)

    # the same verdict through the tool of the agent (read only: nothing new is run)
    again = ok("optimizer_verdict_get", {"strategy_id": STRATEGY, "version": 1}, caller("strategy-researcher"))
    assert again["verdict_id"] == first["verdict_id"] and again["checks"] == first["checks"]

    # 2. take a check out: no run, the audit verdict stays, the check shows as out
    runs_now, trials_now = count(db, "backtest_runs"), count(db, "backtest_trials")
    target = next((c for c in first["checks"] if c["status"] == "fail" and not c["excluded"]), next(c for c in first["checks"] if not c["excluded"]))
    out = screen.post(f"/api/verdict/{first['verdict_id']}/exclude", headers=OWNER, json={"keys": [target["key"]], "reason": "journey: not for this strategy"})
    assert out.status_code == 200, out.text
    excluded = out.json()
    assert excluded["audit_verdict"] == first["audit_verdict"] and excluded["verdict_id"] != first["verdict_id"]
    assert [e["key"] for e in excluded["excluded"] if e["by"] == "owner"] == [target["key"]] and [e for e in excluded["excluded"] if e["by"] != "owner"] == first["excluded"] and next(c for c in excluded["checks"] if c["key"] == target["key"])["excluded"] is True
    say(step="exclude", key=target["key"], status_was=target["status"], verdict_id=excluded["verdict_id"], verdict=excluded["verdict"], colors=colors(excluded))

    # 3. put it back: the verdict is the one of before taking it out
    back = screen.post(f"/api/verdict/{excluded['verdict_id']}/include", headers=OWNER, json={"keys": [target["key"]]})
    assert back.status_code == 200, back.text
    included = back.json()
    assert included["excluded"] == first["excluded"] and included["verdict"] == first["verdict"] and included["checks"] == first["checks"]
    assert (count(db, "backtest_runs"), count(db, "backtest_trials")) == (runs_now, trials_now)
    say(step="include", verdict_id=included["verdict_id"], verdict=included["verdict"])

    # 4. answer the open questions and recompute with the answers
    asked = screen.get(f"/api/verdict/{STRATEGY}/1/questions", headers=TOKEN).json()
    open_ones = [q for q in asked if q["status"] == "ask"]
    given = [{"key": q["key"], "value": q["suggestion"] or "no"} for q in open_ones]
    answered_before = count(db, "audit_answers")
    if given:
        saved = screen.post(f"/api/verdict/{STRATEGY}/1/answers", headers=OWNER, json={"answers": given})
        assert saved.status_code == 200 and saved.json()["saved"] == len(given), saved.text
        assert count(db, "audit_answers") == answered_before + len(given)
        assert not [q for q in saved.json()["questions"] if q["status"] == "ask"]
    recomputed = screen.post(f"/api/verdict/{included['verdict_id']}/recompute", headers=OWNER, json={"cause": "answers"})
    assert recomputed.status_code == 200, recomputed.text
    answered = recomputed.json()
    assert answered["open_questions"] == [] or all(q["key"] not in {g["key"] for g in given} for q in answered["open_questions"])
    say(step="answers", answers=given, verdict_id=answered["verdict_id"], verdict=answered["verdict"], colors=colors(answered))

    # 5. change a limit: a new version of the limits that the verdict does not use until it is recomputed
    now = screen.get("/api/verdict/thresholds/DT", headers=TOKEN).json()
    changed = {**now["values"], "min_years": 0.5, "min_trades": 10}                       # the loosest history the editor allows: it decides check 9 only
    assert changed != now["values"]
    saved_set = screen.put("/api/verdict/thresholds/DT", headers=OWNER, json={"values": changed, "reason": "journey: the loosest history"})
    assert saved_set.status_code == 200, saved_set.text
    new_set = saved_set.json()
    assert new_set["version"] == now["version"] + 1 and (new_set["values"]["min_years"], new_set["values"]["min_trades"]) == (0.5, 10) and new_set["unchanged"] is False
    still = screen.get(f"/api/verdict/{STRATEGY}/1", headers=TOKEN).json()
    assert still["thresholds"]["version"] == now["version"] and still["verdict_id"] == answered["verdict_id"]
    with_new = screen.post(f"/api/verdict/{answered['verdict_id']}/recompute", headers=OWNER, json={"cause": "thresholds"})
    assert with_new.status_code == 200, with_new.text
    limited = with_new.json()
    assert limited["thresholds"] == {"line": "DT", "version": new_set["version"]}
    moved = [(c["key"], c["status"], d["status"]) for c, d in zip(answered["checks"], limited["checks"]) if c["status"] != d["status"]]
    decided = {n for m in th.MEASURES if m.key in ("min_years", "min_trades") for n in m.checks}
    assert {c["number"] for c, d in zip(answered["checks"], limited["checks"]) if c["status"] != d["status"]} <= decided          # only what the changed limits decide moves
    say(step="threshold_recompute", version=new_set["version"], verdict_id=limited["verdict_id"], verdict=limited["verdict"], colors=colors(limited), moved=moved)

    # 6. restore the old limits: a third version with the values of the first, and the verdict goes back to what it was
    history = screen.get("/api/verdict/thresholds/DT/history", headers=TOKEN).json()
    assert [s["version"] for s in history][-2:] == [now["version"], new_set["version"]] or {s["version"] for s in history} >= {now["version"], new_set["version"]}
    old = next(s for s in history if s["version"] == now["version"])
    restored = screen.post(f"/api/verdict/thresholds/DT/restore/{old['id']}", headers=OWNER)
    assert restored.status_code == 200, restored.text
    assert restored.json()["values"] == old["values"] and restored.json()["version"] == new_set["version"] + 1 and restored.json()["restored_from"] == old["id"]
    went_back = screen.post(f"/api/verdict/{limited['verdict_id']}/recompute", headers=OWNER, json={"cause": "thresholds"}).json()
    assert went_back["checks"] == answered["checks"] and went_back["verdict"] == answered["verdict"]
    say(step="restore", version=restored.json()["version"], verdict_id=went_back["verdict_id"], verdict=went_back["verdict"])

    # 7. the library and the request of an indicator, as the creator would make them
    listing = ok("library_list", {"limit": 200}, caller("strategy-researcher"))
    assert listing["total"] == len(screen.get("/api/verdict/library", headers=TOKEN).json()) and listing["total"] > 0
    made_request = ok("library_request", {"name": "calm_ratio", "description": "Share of bars whose range is under half the average range of the last days.",
                                          "fields": ["high", "low"], "params": {"length": 20}}, caller("strategy-researcher"))
    assert made_request["status"] == "open" and made_request["requested_by"] == "strategy-researcher"
    requested = ok("library_list", {"status": "requested", "limit": 200}, caller("strategy-researcher"))
    assert "calm_ratio" in {i["name"] for i in requested["items"]}
    say(step="library", total=listing["total"], request_id=made_request["request_id"])

    # 8. the verdict stopped nothing: the rows of the verdict table are the ones of these steps and no order exists anywhere
    chain = rows(db, "SELECT id, parent_id, cause, actor FROM strategy_verdicts ORDER BY id")
    assert [r["id"] for r in chain][:1] == [first["verdict_id"]] and len(chain) == 6
    say(step="end", verdicts=[(r["id"], r["parent_id"], r["cause"], r["actor"]) for r in chain], seconds=round(time.monotonic() - begun, 1))
    assert th.current("DT", db=db).version == restored.json()["version"]


@pytest.mark.skipif(not os.environ.get("HOKMA_JOURNEY_KEEP"), reason="set HOKMA_JOURNEY_KEEP=<folder> to leave the data of the journey for the screen")
def test_load_the_nq_run_for_the_screen(monkeypatch, screen):
    base = Path(os.environ["HOKMA_JOURNEY_KEEP"]).resolve()
    assert not base.exists() or not any(base.iterdir()), f"{base} is not empty: use a folder that does not exist yet or is empty"
    point_at(monkeypatch, base)
    services, bars = use_nq_services(monkeypatch, base)
    made = build_runs(services, bars)
    job, found = evaluate(screen)
    say(step="loaded", folder=str(base), data=str(base / "data"), run=made["run"]["run_id"], verdict_id=found["verdict_id"], verdict=found["verdict"], seconds=made["took"])
