"""Bug 1135: shutdown, update and restore refuse with 409 `*.in_use` while an agent works, unless `force=true`."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dbsupport import db, env  # noqa: F401  (fixtures)
from hokma.backup import restore
from hokma.backup.api import create_router
from hokma.core import app as app_module
from hokma.core import lifecycle as lifecycle_module
from hokma.core import session
from hokma.core.app import create_app
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN
from hokma.update import api as update_api

SHUTDOWN = "/api/system/shutdown"
SESSION = {"X-Hokma-Token": SESSION_TOKEN, "Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}
HEAD = {"X-Hokma-Token": SESSION_TOKEN}


@pytest.fixture
def working(monkeypatch):
    """The state of the agents, flipped by each test."""
    state = {"busy": False}
    monkeypatch.setattr(app_module, "agent_working", lambda: state["busy"])
    return state


@pytest.fixture
def stops(monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle_module.control, "request", lambda word, timeout=3.0: calls.append(word) or {"ok": True})
    monkeypatch.setattr(lifecycle_module, "FLUSH_SECONDS", 0.0)
    monkeypatch.setattr(session, "_supervised", True)
    return calls


def _detail(res):
    return res.json()["detail"]


# ---- shutdown ---------------------------------------------------------------------------------------------------

def test_shutdown_with_a_session_is_refused_while_an_agent_works(working, stops):
    working["busy"] = True
    with TestClient(create_app()) as c:
        res = c.post(SHUTDOWN, headers=SESSION)
        assert res.status_code == 409
        assert _detail(res) == {"code": "shutdown.in_use", "message": t("shutdown.err.busy")}
    assert stops == []                                   # nothing was stopped


def test_shutdown_with_force_goes_ahead_while_an_agent_works(working, stops):
    working["busy"] = True
    with TestClient(create_app()) as c:
        res = c.post(f"{SHUTDOWN}?force=true", headers=SESSION)
        assert res.status_code == 202 and res.json() == {"stopping": True}


def test_shutdown_without_work_needs_no_force(working, stops):
    with TestClient(create_app()) as c:
        assert c.post(SHUTDOWN, headers=SESSION).status_code == 202


# ---- update -----------------------------------------------------------------------------------------------------

def test_update_is_refused_while_an_agent_works_and_launches_nothing(working, monkeypatch, db):  # noqa: F811
    launched = []
    monkeypatch.setattr(update_api.detached, "check_newer", lambda source=None, force=False: launched.append("check") or None)
    working["busy"] = True
    c = TestClient(create_app())
    res = c.post("/api/update", headers=HEAD)
    assert res.status_code == 409
    assert _detail(res) == {"code": "update.in_use", "message": t("update.err.busy")}
    assert launched == []                                # it did not even look for a new version


def test_update_with_force_or_without_work_gets_past_the_gate(working, monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(update_api.detached, "check_newer", lambda source=None, force=False: None)
    c = TestClient(create_app())
    working["busy"] = True
    forced = c.post("/api/update?force=true", headers=HEAD)
    assert forced.status_code == 200 and forced.json()["status"] == "up-to-date"
    working["busy"] = False
    idle = c.post("/api/update", headers=HEAD)
    assert idle.status_code == 200 and idle.json()["status"] == "up-to-date"


# ---- restore ----------------------------------------------------------------------------------------------------

class _Lifecycle:
    def __init__(self):
        self.restarts = 0

    async def restart(self):
        self.restarts += 1


@pytest.fixture
def backup_client(monkeypatch):
    prepared = []
    monkeypatch.setattr(restore, "prepare", lambda db, name, key: prepared.append(name) or {"status": "restart_required", "reconnect": []})
    app = FastAPI()
    app.include_router(create_router())
    app.state.db = object()
    app.state.lifecycle = _Lifecycle()
    return TestClient(app), app.state.lifecycle, prepared


BODY = {"name": "x.hkb", "key": "k"}


def test_restore_is_refused_while_an_agent_works_and_prepares_nothing(working, backup_client):
    client, lifecycle, prepared = backup_client
    working["busy"] = True
    res = client.post("/api/backup/restore", json=BODY, headers=HEAD)
    assert res.status_code == 409
    assert _detail(res) == {"code": "restore.in_use", "message": t("restore.err.busy")}
    assert prepared == [] and lifecycle.restarts == 0    # the staged restore and the restart did not happen


def test_restore_with_force_or_without_work_goes_ahead(working, backup_client):
    client, lifecycle, prepared = backup_client
    working["busy"] = True
    assert client.post("/api/backup/restore?force=true", json=BODY, headers=HEAD).status_code == 200
    working["busy"] = False
    assert client.post("/api/backup/restore", json=BODY, headers=HEAD).status_code == 200
    assert prepared == ["x.hkb", "x.hkb"] and lifecycle.restarts == 2


def test_the_busy_messages_exist_in_both_languages():
    import json
    from pathlib import Path

    catalog = Path(app_module.__file__).resolve().parents[1] / "catalog"
    for lang in ("es", "en"):
        keys = json.loads((catalog / f"{lang}.json").read_text(encoding="utf-8"))
        for key in ("shutdown.err.busy", "update.err.busy", "restore.err.busy"):
            assert keys[key]
