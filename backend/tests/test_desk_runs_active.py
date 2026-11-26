"""The cycles the server runs for the desks, as the right panel reads them (SPEC-013 13.5, T6)."""
from datetime import UTC, datetime, timedelta

from backoffice_support import db, desks_db, env, ready  # noqa: F401
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.backoffice import runs
from hokma.desks import service
from hokma.desks.api import router
from hokma.rpc.ws import SESSION_TOKEN


def test_a_running_cycle_is_listed_with_its_desk_and_moves_to_finished_when_it_ends(ready):
    run_id = runs.begin(ready.desk_id, "swo_cycle", signature="sig-a", db=ready.db)
    seen = service.active_runs(db=ready.db)
    assert seen["finished_24h"] == []
    [row] = seen["running"]
    assert row["run_id"] == run_id and row["desk_id"] == ready.desk_id and row["kind"] == "swo_cycle"
    assert row["desk_name"] and row["started_at"]
    runs.finish(run_id, "done", db=ready.db)
    seen = service.active_runs(db=ready.db)
    assert seen["running"] == []
    [done] = seen["finished_24h"]
    assert done["run_id"] == run_id and done["status"] == "done" and done["desk_name"] == row["desk_name"] and done["ended_at"]


def test_failed_cycles_count_and_the_ones_older_than_a_day_do_not(ready):
    old = runs.begin(ready.desk_id, "swo_cycle", signature="sig-old", db=ready.db)
    runs.finish(old, "done", ended_at=datetime.now(UTC) - timedelta(hours=25), db=ready.db)
    bad = runs.begin(ready.desk_id, "swo_cycle", signature="sig-bad", db=ready.db)
    runs.finish(bad, "failed", db=ready.db)
    stopped = runs.begin(ready.desk_id, "swo_cycle", signature="sig-int", db=ready.db)
    runs.interrupt(stopped, db=ready.db)
    seen = service.active_runs(db=ready.db)
    assert [(r["run_id"], r["status"]) for r in seen["finished_24h"]] == [(bad, "failed")]
    assert seen["running"] == []                     # an interrupted run is neither running nor finished


def test_the_route_needs_the_token_and_answers_the_same_shape(ready):
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/api/runs/active").status_code == 401
        res = client.get("/api/runs/active", headers={"X-Hokma-Token": SESSION_TOKEN})
    assert res.status_code == 200 and set(res.json()) == {"running", "finished_24h"}
