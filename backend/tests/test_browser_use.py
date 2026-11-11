"""Task 3162: use Hokma in the browser as an alternative to the app."""
import pytest
from fastapi.testclient import TestClient

from dbsupport import db, env  # noqa: F401  (fixtures)
from hardblock_support import box  # noqa: F401  (fixture)
from hokma.core import config, control, shutdown_api
from hokma.core.app import create_app
from hokma.core.supervisor import Supervisor
from hokma.rpc.ws import SESSION_TOKEN

SAME = {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}
WITH_SESSION = {"X-Hokma-Token": SESSION_TOKEN, **SAME}


@pytest.fixture
def asked(monkeypatch):
    calls = []
    monkeypatch.setattr(control, "request", lambda word, timeout=3.0: calls.append(word) or {"ok": True})
    monkeypatch.setattr(shutdown_api, "turns_running", lambda: 0)
    return calls


def entry_headers(c):
    nonce = c.get("/api/system/shutdown-nonce", headers={"Sec-Fetch-Site": "same-origin"}).json()["nonce"]
    return {**SAME, "X-Hokma-Shutdown-Nonce": nonce}


def test_entry_screen_asks_the_supervisor_for_the_browser(asked):
    with TestClient(create_app()) as c:
        assert c.post("/api/system/browser", headers=entry_headers(c)).status_code == 202
    assert asked == ["browser"]


def test_entry_screen_without_nonce_or_with_someone_inside_is_refused(asked):
    with TestClient(create_app()) as c:
        assert c.post("/api/system/browser", headers=SAME).status_code == 403
        with c.websocket_connect(f"/rpc?token={SESSION_TOKEN}"):
            assert c.post("/api/system/browser", headers=entry_headers(c)).status_code == 409
    assert asked == []


def test_with_a_session_it_works_and_a_wrong_token_does_not(asked):
    with TestClient(create_app()) as c:
        assert c.post("/api/system/browser", headers={**SAME, "X-Hokma-Token": "nope"}).status_code == 401
        assert c.post("/api/system/browser", headers=WITH_SESSION).status_code == 202
    assert asked == ["browser"]


def test_preference_is_a_marker_file_that_needs_the_session(env):
    with TestClient(create_app()) as c:
        url = "/api/system/browser-pref"
        assert c.get(url, headers=SAME).status_code == 401
        assert c.put(url, json={"always": True}, headers=SAME).status_code == 401
        assert c.put(url, json={"always": True}, headers=WITH_SESSION).json() == {"always": True}
        assert config.browser_preferred() and c.get(url, headers=WITH_SESSION).json() == {"always": True}
        assert c.put(url, json={"always": "x"}, headers=WITH_SESSION).status_code == 422
        c.put(url, json={"always": False}, headers=WITH_SESSION)
        assert not config.browser_preferred()


def test_control_word_browser_replies_only_ok(monkeypatch):
    monkeypatch.setattr(control, "_browser", [lambda: True])
    assert control.answer(b"browser\n", lambda: {}, lambda: None) == b'{"ok": true}\n'
    monkeypatch.setattr(control, "_browser", [None])
    assert b"unknown_command" in control.answer(b"browser\n", lambda: {}, lambda: None)


def test_supervisor_new_session_makes_a_new_code_and_limits_the_rate(monkeypatch):
    opened = []
    monkeypatch.setattr("hokma.core.supervisor.open_when_ready", lambda url, health: opened.append(url))
    sup = Supervisor(port=8765, token="T" * 20, job=None, owner_hash="x", before_force=lambda: None)

    class Child:
        terminated = False
        poll = staticmethod(lambda: None)

        def terminate(self):
            self.terminated = True
    sup._child = Child()
    assert sup.open_browser() is True and sup._child.terminated
    assert sup.open_browser() is False                      # less than 5 seconds apart
    assert sup._reopening and sup.owner_hash == "" and sup.owner_secret_hash
    import time
    time.sleep(0.2)
    assert opened and "#t=" in opened[0] and "&o=" in opened[0]


def test_with_a_session_it_still_waits_for_running_turns(asked, monkeypatch):
    monkeypatch.setattr(shutdown_api, "turns_running", lambda: 1)
    with TestClient(create_app()) as c:
        assert c.post("/api/system/browser", headers=WITH_SESSION).status_code == 409
    assert asked == []


def test_reopening_flag_stays_false_when_there_is_no_engine(monkeypatch):
    monkeypatch.setattr("hokma.core.supervisor.open_when_ready", lambda url, health: None)
    sup = Supervisor(port=8765, token="T" * 20, job=None, owner_hash="x", before_force=lambda: None)
    assert sup._child is None and sup.open_browser() is True
    assert sup._reopening is False
