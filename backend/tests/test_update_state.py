"""Novedades backend: the last-checked time, real update stages, the restart after an update, the 24 hour search."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dbsupport import db, env  # noqa: F401  (fixtures)
from hokma import __version__
from hokma.core import session
from hokma.core.app import create_app
from hokma.core.lifecycle import EXIT_RESTART
from hokma.db import get_database
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN
from hokma.status import news
from hokma.update import api as update_api
from hokma.update import state as state_module
from hokma.update.state import STAGES, ProgressWriter, UpdateState

HEADERS = {"X-Hokma-Token": SESSION_TOKEN}


@pytest.fixture(autouse=True)
def _fresh_cache():
    news.clear_cache()
    yield
    news.clear_cache()


@pytest.fixture
def profile(db):  # noqa: F811
    with get_database().write() as u:
        u.profile.save(owner_name="Ana")
    return db


def fake_update(monkeypatch, target: str | None = "9.9.9", error: Exception | None = None):
    """The update source answers `target` (None: up to date); `launch` records its call instead of starting anything."""
    launched = []

    def check(source=None, force=False):
        if error is not None:
            raise error
        return target

    def launch(**kwargs):
        launched.append(kwargs)
        return 4242

    monkeypatch.setattr(update_api.detached, "check_newer", check)
    monkeypatch.setattr(update_api.detached, "launch", launch)
    return launched


def test_state_needs_the_token(profile):
    c = TestClient(create_app())
    assert c.get("/api/update/state").status_code == 401
    body = c.get("/api/update/state", headers=HEADERS).json()
    assert body["version"] == __version__ and body["phase"] == "idle" and body["checked_at"] is None
    assert body["supervised"] is False and body["engine_log"].endswith("engine.log")


def test_a_successful_check_is_stored_in_the_profile_and_shown(profile, monkeypatch):
    monkeypatch.setattr(news, "fetch_latest", lambda: news.Latest("99.0.0"))
    c = TestClient(create_app(), headers=HEADERS)
    assert c.get("/api/update/state").json()["newer"] is False
    body = c.get("/api/news", params={"refresh": "true"}).json()
    assert body["newer"] is True and body["checked_at"].endswith("Z")
    with get_database().read() as u:
        assert u.profile.get().update_checked_at                           # profile.update_checked_at
    state = c.get("/api/update/state").json()
    assert state["checked_at"] == body["checked_at"] and state["newer"] is True and state["latest_version"] == "99.0.0"


def test_a_failed_check_does_not_move_the_checked_time(profile, monkeypatch):
    monkeypatch.setattr(news, "fetch_latest", lambda: news.Latest(__version__))
    c = TestClient(create_app(), headers=HEADERS)
    first = c.get("/api/news", params={"refresh": "true"}).json()["checked_at"]

    def offline():
        raise OSError("offline")

    monkeypatch.setattr(news, "fetch_latest", offline)
    body = c.get("/api/news", params={"refresh": "true"}).json()
    assert body["latest"] is None and body["latest_error"] and body["checked_at"] == first


def test_the_checked_time_survives_before_the_profile_exists(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(news, "fetch_latest", lambda: news.Latest(__version__))
    c = TestClient(create_app(), headers=HEADERS)
    assert c.get("/api/news", params={"refresh": "true"}).json()["checked_at"].endswith("Z")


def test_update_reports_the_real_stage_and_a_running_clock(profile, monkeypatch):
    state = UpdateState()
    assert state.begin_update() is True and state.begin_update() is False        # one update at a time
    writer = ProgressWriter(state_module.progress_path())
    writer("download")
    writer("dependencies")
    snap = state.snapshot(get_database())
    assert snap["phase"] == "updating" and snap["stage"] == "dependencies"
    assert snap["elapsed_s"] >= 0 and snap["stage_elapsed_s"] >= 0 and "dependencies" in STAGES
    state.fail_update("boom")
    assert state.snapshot(get_database())["stage"] is None


def test_a_second_update_while_one_runs_is_refused(profile):
    app = create_app()
    app.state.updates.begin_update()
    r = TestClient(app).post("/api/update", headers=HEADERS)
    assert r.status_code == 409 and r.json()["detail"] == t("update.in_progress")


def test_update_launches_the_detached_updater_and_shuts_down_with_the_reason_updating(profile, monkeypatch):
    launched = fake_update(monkeypatch)
    shut = []

    async def fake_shutdown(reason="shutting_down"):
        shut.append(reason)

    app = create_app()
    monkeypatch.setattr(app.state.lifecycle, "shutdown", fake_shutdown)
    r = TestClient(app).post("/api/update", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "launched" and body["to_version"] == "9.9.9" and body["restarting"] is True
    assert len(launched) == 1 and launched[0]["restart"] is True and launched[0]["to_version"] == "9.9.9"
    token, _owner, _once = launched[0]["handoff"]
    assert token == session.token()                                 # the same session comes back after the update
    assert shut == ["updating"]                                     # shut down AFTER the answer, the clean way
    assert app.state.updates.snapshot(get_database())["phase"] == "restarting"
    assert TestClient(app).post("/api/update", headers=HEADERS).status_code == 409   # one launch only


def test_up_to_date_never_launches_nor_shuts_down(profile, monkeypatch):
    launched = fake_update(monkeypatch, target=None)
    app = create_app()
    r = TestClient(app).post("/api/update", headers=HEADERS)
    assert r.json()["status"] == "up-to-date" and r.json()["restarting"] is False
    assert launched == [] and app.state.lifecycle.shutting_down is False


def test_a_check_that_fails_keeps_its_message_for_the_screen(profile, monkeypatch):
    from hokma.update.source import UpdateError

    fake_update(monkeypatch, error=UpdateError("GitHub is unreachable"))
    app = create_app()
    c = TestClient(app, headers=HEADERS)
    r = c.post("/api/update")
    assert r.status_code == 502 and r.json()["detail"] == "GitHub is unreachable"
    snap = c.get("/api/update/state").json()
    assert snap["phase"] == "error" and snap["error"] == "GitHub is unreachable"
    assert "Traceback" not in snap["error"]
    fake_update(monkeypatch)                                                       # "Retry" works

    async def no_shutdown(reason=""):
        return None

    monkeypatch.setattr(app.state.lifecycle, "shutdown", no_shutdown)
    assert c.post("/api/update").status_code == 200


def test_a_failed_detached_update_is_reported_after_the_restart(profile):
    from hokma.update import detached

    status = detached.StatusFile()
    status.write("failed", message="the old files are in use")
    app = create_app()                                        # the engine that the updater started again
    snap = TestClient(app, headers=HEADERS).get("/api/update/state").json()
    assert snap["phase"] == "error" and snap["error"] == "the old files are in use"
    status.body["updated_at"] = 0                             # a day later it is no longer news
    detached.status_path().write_text(json.dumps(dict(status.body, updated_at=time.time() - 2 * 86400)))
    assert TestClient(app, headers=HEADERS).get("/api/update/state").json()["phase"] == "idle"


def test_restart_closes_open_sockets_with_the_reason_restarting(profile):
    app = create_app()
    with TestClient(app) as c:
        with c.websocket_connect(f"/rpc?token={SESSION_TOKEN}") as ws:
            ws.send_text(json.dumps({"jsonrpc": "2.0", "id": 1}))
            ws.receive_text()
            c.portal.call(app.state.lifecycle.restart)
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
    assert closed.value.code == 1012 and closed.value.reason == "restarting"
    assert app.state.lifecycle.exit_code == EXIT_RESTART


def test_the_automatic_search_runs_at_start_and_every_24_hours(profile, monkeypatch):
    waits, checks = [], []

    async def fake_sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(state_module.asyncio, "sleep", fake_sleep)
    state = UpdateState()
    monkeypatch.setattr(state, "check", lambda db, refresh: checks.append(refresh))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(state.auto_check_loop(lambda: get_database()))
    assert waits == [state_module.AUTO_CHECK_FIRST_DELAY_SECONDS, 86400, 86400] and checks == [True, True]


def test_the_automatic_search_is_off_unless_the_real_engine_asks_for_it(profile, monkeypatch):
    started = []
    monkeypatch.setattr(UpdateState, "auto_check_loop", lambda self, get_db: started.append(1) or asyncio.sleep(0))
    with TestClient(create_app()):
        pass
    assert started == []
    with TestClient(create_app(auto_update_check=True)):
        time.sleep(0.05)
    assert started == [1]
