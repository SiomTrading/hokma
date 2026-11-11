"""POST /api/system/shutdown (task 3082, A-01 to A-10): the Shut down Hokma button and its entry-screen form."""
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dbsupport import db, env  # noqa: F401  (fixtures)
from hardblock_support import box  # noqa: F401  (fixture)
from hokma.core import lifecycle as lifecycle_module
from hokma.core import session, shutdown_api
from hokma.core.app import create_app
from hokma.db import get_database
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN

ROUTE = "/api/system/shutdown"
SAME = {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}
WITH_SESSION = {"X-Hokma-Token": SESSION_TOKEN, **SAME}


@pytest.fixture
def stops(monkeypatch):
    """The control channel is replaced by a recorder: it answers like a live supervisor."""
    calls = []
    monkeypatch.setattr(lifecycle_module.control, "request", lambda word, timeout=3.0: calls.append(word) or {"ok": True})
    monkeypatch.setattr(lifecycle_module, "FLUSH_SECONDS", 0.0)
    monkeypatch.setattr(shutdown_api, "turns_running", lambda: 0)
    return calls


def nonce_headers(client, **extra):
    """The entry screen asks for a nonce first (a fetch of the page itself) and sends it back with the call."""
    got = client.get("/api/system/shutdown-nonce", headers={"Sec-Fetch-Site": "same-origin"})
    assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
    return {**SAME, "X-Hokma-Shutdown-Nonce": got.json()["nonce"], **extra}


def _error(res):
    return res.json()["detail"]


def test_a01_with_a_session_it_closes_every_socket_and_asks_the_supervisor_once(stops, monkeypatch):
    monkeypatch.setattr(session, "_supervised", True)
    app = create_app()
    with TestClient(app) as c:
        with c.websocket_connect(f"/rpc?token={SESSION_TOKEN}") as one, \
                c.websocket_connect(f"/rpc?token={SESSION_TOKEN}") as two:
            one.send_text(json.dumps({"jsonrpc": "2.0", "id": 1}))
            one.receive_text()
            two.send_text(json.dumps({"jsonrpc": "2.0", "id": 2}))
            two.receive_text()
            assert app.state.lifecycle.open_sockets == 2
            res = c.post(ROUTE, headers=WITH_SESSION)
            assert res.status_code == 202 and res.json() == {"stopping": True}
            assert res.headers["cache-control"] == "no-store"
            for ws in (one, two):
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_text()
                assert closed.value.code == lifecycle_module.WS_SHUTDOWN_CODE == 1001
                assert closed.value.reason == lifecycle_module.WS_SHUTDOWN_REASON == "shutting_down"
    assert stops == ["stop"]
    assert app.state.lifecycle.shutting_down is True and app.state.lifecycle.exit_code == 0


def test_a02_without_a_supervisor_the_server_itself_is_stopped(stops):
    app = create_app()
    asked = []
    app.state.lifecycle.stop = lambda: asked.append(True)
    res = TestClient(app).post(ROUTE, headers=WITH_SESSION)
    assert res.status_code == 202
    assert asked == [True] and stops == [] and app.state.lifecycle.exit_code == 0


def test_a02b_a_supervisor_that_does_not_answer_falls_back_to_stopping_the_server(monkeypatch):
    monkeypatch.setattr(session, "_supervised", True)
    monkeypatch.setattr(lifecycle_module.control, "request", lambda word, timeout=3.0: None)
    monkeypatch.setattr(lifecycle_module, "FLUSH_SECONDS", 0.0)
    app = create_app()
    asked = []
    app.state.lifecycle.stop = lambda: asked.append(True)
    assert TestClient(app).post(ROUTE, headers=WITH_SESSION).status_code == 202
    assert asked == [True] and app.state.lifecycle.exit_code == 0


def test_a03_the_owner_confirmation_is_not_required(stops):
    session.forget_owner_for_tests()                                  # a tab that lost the owner confirmation
    try:
        res = TestClient(create_app()).post(ROUTE, headers=WITH_SESSION)
    finally:
        session.arm_owner_secret_for_tests("O" * 22)
        session.register_owner("0123456789abcdef0123456789abcdef", "O" * 22)
    assert res.status_code == 202


@pytest.mark.parametrize("headers,status,code", [
    ({**WITH_SESSION, "X-Hokma-Token": "wrong"}, 401, "shutdown.unauthorized"),
    ({**WITH_SESSION, "Host": "evil.com"}, 403, None),
    ({**WITH_SESSION, "Origin": "http://evil.com"}, 403, None),
    ({"X-Hokma-Token": SESSION_TOKEN, "Origin": "http://testserver"}, 403, "shutdown.origin"),     # no Sec-Fetch-Site
    ({**WITH_SESSION, "Sec-Fetch-Site": "cross-site"}, 403, "shutdown.origin"),
])
def test_a04_the_session_form_refuses_bad_token_host_origin_and_fetch_site(stops, headers, status, code):
    app = create_app()
    asked = []
    app.state.lifecycle.stop = lambda: asked.append(True)
    with TestClient(app) as c:
        with c.websocket_connect(f"/rpc?token={SESSION_TOKEN}") as ws:
            res = c.post(ROUTE, headers=headers)
            assert res.status_code == status
            if code:
                assert _error(res)["code"] == code
            ws.send_text(json.dumps({"jsonrpc": "2.0", "id": 1}))
            assert json.loads(ws.receive_text())["id"] == 1               # the socket was not closed
    assert asked == [] and stops == [] and app.state.lifecycle.shutting_down is False


def test_a05_without_a_session_and_nobody_in_it_shuts_down(stops):
    app = create_app()
    c = TestClient(app)
    res = c.post(ROUTE, headers=nonce_headers(c))
    assert res.status_code == 202 and res.json() == {"stopping": True}
    assert app.state.lifecycle.shutting_down is True


def test_a05b_without_a_session_the_origin_must_be_present_and_the_engines(stops):
    c = TestClient(create_app())
    good = nonce_headers(c)
    for headers in ({"Sec-Fetch-Site": "same-origin"}, {"Origin": "http://testserver"}):
        res = c.post(ROUTE, headers=headers)
        assert res.status_code == 403 and _error(res)["code"] == "shutdown.origin", headers
    assert c.post(ROUTE, headers={**good, "Origin": "http://evil.com"}).status_code == 403     # the app-wide guard
    assert stops == []


def test_a06_without_a_session_an_open_socket_blocks_it(stops):
    app = create_app()
    with TestClient(app) as c:
        with c.websocket_connect(f"/rpc?token={SESSION_TOKEN}"):
            res = c.post(ROUTE, headers=nonce_headers(c))
            assert res.status_code == 409 and _error(res)["code"] == "shutdown.in_use"
            assert _error(res)["message"] == t("shutdown.err.in_use")
    assert stops == [] and app.state.lifecycle.shutting_down is False


def test_a07_without_a_session_a_running_turn_blocks_it(stops, monkeypatch):
    monkeypatch.setattr(shutdown_api, "turns_running", lambda: 1)
    c = TestClient(create_app())
    res = c.post(ROUTE, headers=nonce_headers(c))
    assert res.status_code == 409 and _error(res)["code"] == "shutdown.in_use" and stops == []


def test_a08_without_a_session_a_second_call_within_30_seconds_is_too_soon(stops):
    app = create_app()
    app.state.lifecycle.shutdown = _never_ends                          # the simulated shutdown does not finish
    c = TestClient(app)
    assert c.post(ROUTE, headers=nonce_headers(c)).status_code == 202
    res = c.post(ROUTE, headers=nonce_headers(c))
    assert res.status_code == 429 and _error(res)["code"] == "shutdown.too_soon"


async def _never_ends():
    return None


def test_a09_shutdown_twice_asks_the_supervisor_once(stops, monkeypatch):
    monkeypatch.setattr(session, "_supervised", True)
    app = create_app()
    with TestClient(app) as c:
        c.portal.call(app.state.lifecycle.shutdown)
        c.portal.call(app.state.lifecycle.shutdown)
    assert stops == ["stop"]


def test_a09b_a_restart_in_progress_wins_over_a_shutdown(stops, monkeypatch):
    monkeypatch.setattr(session, "_supervised", True)
    app = create_app()
    app.state.lifecycle.restarting = True
    with TestClient(app) as c:
        c.portal.call(app.state.lifecycle.shutdown)
    assert stops == [] and app.state.lifecycle.shutting_down is False


def test_a09c_the_shutdown_is_written_in_the_journal_as_the_owner(stops, db):
    app = create_app()
    c = TestClient(app)
    assert c.post(ROUTE, headers=WITH_SESSION).status_code == 202
    app.state.lifecycle.shutting_down = False
    app.state.shutdown_at = None
    assert c.post(ROUTE, headers=nonce_headers(c)).status_code == 202
    with db.read() as u:
        entries = sorted((e.actor_kind, e.title) for e in u.journal.list() if "shut down" in e.title)
    assert entries == [("human", t("shutdown.journal.entry")), ("human", t("shutdown.journal.panel"))]


# ---- A-10: an agent cannot name the route -------------------------------------------------------------------------






def test_a10c_a_normal_api_call_is_still_free(box):
    assert box.bash("curl http://127.0.0.1:8765/health") is None
    assert box.bash("curl http://127.0.0.1:8765/api/system/other") is None


# ---- the nonce of the entry-screen form -----------------------------------------------------------------------------
def test_n01_without_a_nonce_the_entry_form_is_refused(stops):
    app = create_app()
    res = TestClient(app).post(ROUTE, headers=SAME)
    assert res.status_code == 403 and _error(res)["code"] == "shutdown.nonce"
    assert app.state.lifecycle.shutting_down is False and stops == []


def test_n02_a_wrong_nonce_is_refused_and_does_not_spend_a_real_one(stops):
    c = TestClient(create_app())
    good = nonce_headers(c)
    res = c.post(ROUTE, headers={**good, "X-Hokma-Shutdown-Nonce": "x" * 32})
    assert res.status_code == 403 and _error(res)["code"] == "shutdown.nonce"
    assert c.post(ROUTE, headers=good).status_code == 202


def test_n03_a_nonce_works_once(stops, monkeypatch):
    monkeypatch.setattr(shutdown_api, "turns_running", lambda: 1)       # the first use is refused for another reason
    c = TestClient(create_app())
    headers = nonce_headers(c)
    assert _error(c.post(ROUTE, headers=headers))["code"] == "shutdown.in_use"      # the nonce was spent
    again = c.post(ROUTE, headers=headers)
    assert again.status_code == 403 and _error(again)["code"] == "shutdown.nonce"


def test_n04_an_expired_nonce_is_refused(stops, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(shutdown_api, "now", lambda: clock[0])
    c = TestClient(create_app())
    headers = nonce_headers(c)
    clock[0] += shutdown_api.NONCE_TTL_SECONDS + 1
    res = c.post(ROUTE, headers=headers)
    assert res.status_code == 403 and _error(res)["code"] == "shutdown.nonce" and stops == []


def test_n05_a_nonce_is_bound_to_the_client_that_asked_for_it(stops):
    c = TestClient(create_app())
    headers = nonce_headers(c)
    res = c.post(ROUTE, headers={**headers, "User-Agent": "another-program"})
    assert res.status_code == 403 and _error(res)["code"] == "shutdown.nonce"


def test_n06_only_a_fetch_of_the_page_itself_gets_a_nonce(stops):
    c = TestClient(create_app())
    for headers in ({}, {"Sec-Fetch-Site": "cross-site"}):
        res = c.get("/api/system/shutdown-nonce", headers=headers)
        assert res.status_code == 403 and _error(res)["code"] == "shutdown.origin"


def test_n07_the_session_form_needs_no_nonce(stops):
    assert TestClient(create_app()).post(ROUTE, headers=WITH_SESSION).status_code == 202


def test_n08_nonces_are_capped_and_expired_ones_dropped(stops, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(shutdown_api, "now", lambda: clock[0])
    app = create_app()
    c = TestClient(app)
    for _ in range(shutdown_api.MAX_NONCES + 5):
        nonce_headers(c)
    assert len(app.state.shutdown_nonces) == shutdown_api.MAX_NONCES
    clock[0] += shutdown_api.NONCE_TTL_SECONDS + 1
    nonce_headers(c)
    assert len(app.state.shutdown_nonces) == 1




