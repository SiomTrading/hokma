"""Bug 1035: the owner confirmation. The session token can be read from the browser profile by a process of the same
user, so the calls only the owner may make also need `X-Hokma-Owner`, a value the open tab registered once and the
engine keeps only as a hash in memory."""
import logging

import pytest

from hokma.core import session
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN
from permissions_support import HEADERS, OWNER_NONCE, OWNER_ONCE, owner  # noqa: F401  (fixture)

TOKEN_ONLY = {"X-Hokma-Token": SESSION_TOKEN}
OWNER_CALLS = [
    ("put", "/api/profile/permission-mode", {"mode": "auto"}),
    ("post", "/api/trash/empty", {"confirm": True}),
    ("post", "/api/trash/1/purge", {"confirm": True}),
    ("post", "/api/trash/1/restore", None),
    ("post", "/api/checkpoints/abc/restore-file", {"path": "a.txt"}),
    ("post", "/api/checkpoints/abc/restore-deleted", {}),
    ("post", "/api/session/logout", None),
    ("post", "/api/session/handoff-code", None),
]


@pytest.fixture
def fresh_owner(owner):  # noqa: F811
    """Nothing registered yet, as at the start of Hokma; the shared confirmation of the other tests comes back after."""
    session.forget_owner_for_tests()
    session.arm_owner_secret_for_tests(OWNER_ONCE)
    yield owner
    session.forget_owner_for_tests()
    session.arm_owner_secret_for_tests(OWNER_ONCE)
    assert session.register_owner(OWNER_NONCE, OWNER_ONCE) == session.REGISTERED


def call(owner, method, route, body, headers):
    return getattr(owner.client, method)(route, json=body, headers=headers) if body is not None else \
        getattr(owner.client, method)(route, headers=headers)


@pytest.mark.parametrize("method,route,body", OWNER_CALLS)
def test_1035_the_token_alone_does_not_pass_the_owner_calls(owner, method, route, body):  # noqa: F811
    for headers in (TOKEN_ONLY, {**TOKEN_ONLY, "X-Hokma-Owner": "0" * 32}, {**TOKEN_ONLY, "X-Hokma-Owner": "nonsense"},
                    {**TOKEN_ONLY, "X-Hokma-Owner": ""}, {**TOKEN_ONLY, "X-Hokma-Owner": OWNER_NONCE.upper()}):
        res = call(owner, method, route, body, headers)
        assert res.status_code == 403, (route, headers)
        assert res.json()["detail"]["code"] == "owner_confirmation_required"
        assert res.json()["detail"]["message"] == t("permissions.err.owner_confirmation_required")
    assert owner.profile().permission_mode == "ask"


@pytest.mark.parametrize("method,route,body", OWNER_CALLS)
def test_1035_the_owner_confirmation_without_the_token_is_a_401(owner, method, route, body):  # noqa: F811
    res = call(owner, method, route, body, {"X-Hokma-Owner": OWNER_NONCE})
    assert res.status_code == 401


@pytest.mark.parametrize("method,route,body", OWNER_CALLS)
def test_1035_with_the_registered_confirmation_the_call_reaches_its_own_logic(owner, method, route, body):  # noqa: F811
    res = call(owner, method, route, body, HEADERS)
    assert res.status_code != 403, route                  # 200, or the call's own 404 / 409 for an item that is not there
    assert res.status_code != 401


def test_1035_the_mode_changes_with_the_confirmation_and_not_without(owner):  # noqa: F811
    assert call(owner, "put", "/api/profile/permission-mode", {"mode": "auto"}, TOKEN_ONLY).status_code == 403
    assert owner.profile().permission_mode == "ask"
    assert call(owner, "put", "/api/profile/permission-mode", {"mode": "auto"}, HEADERS).json()["mode"] == "auto"
    assert owner.profile().permission_mode == "auto"


def test_1035_reads_need_only_the_token(owner):  # noqa: F811
    for route in ("/api/profile/permission-mode", "/api/protections", "/api/trash"):
        assert owner.client.get(route, headers=TOKEN_ONLY).status_code == 200, route


def test_1035_nothing_can_register_without_the_token(fresh_owner):
    res = fresh_owner.client.post("/api/session/owner-nonce", json={"nonce": OWNER_NONCE, "once": OWNER_ONCE})
    assert res.status_code == 401 and not session.owner_matches(OWNER_NONCE)


def test_1035_the_first_registration_wins_and_only_the_same_value_is_accepted_again(fresh_owner):
    c = fresh_owner.client
    assert call(fresh_owner, "put", "/api/profile/permission-mode", {"mode": "auto"}, HEADERS).status_code == 403
    body = {"nonce": OWNER_NONCE, "once": OWNER_ONCE}
    assert c.post("/api/session/owner-nonce", json=body, headers=TOKEN_ONLY).json() == {"registered": True}
    assert c.post("/api/session/owner-nonce", json={"nonce": OWNER_NONCE}, headers=TOKEN_ONLY).status_code == 200   # a reload
    other = c.post("/api/session/owner-nonce", json={"nonce": "f" * 32, "once": OWNER_ONCE}, headers=TOKEN_ONLY)
    assert other.status_code == 409 and other.json()["detail"]["code"] == "owner_already_registered"
    assert call(fresh_owner, "put", "/api/profile/permission-mode", {"mode": "auto"}, HEADERS).status_code == 200
    stolen = {**TOKEN_ONLY, "X-Hokma-Owner": "f" * 32}
    assert call(fresh_owner, "put", "/api/profile/permission-mode", {"mode": "ask"}, stolen).status_code == 403


# ---- task 3045: the token alone cannot take the place of the owner --------------------------------------

def test_3045_a_process_that_only_has_the_token_cannot_register_first(fresh_owner):
    c = fresh_owner.client
    for body in ({"nonce": "f" * 32}, {"nonce": "f" * 32, "once": ""}, {"nonce": "f" * 32, "once": "X" * 22},
                 {"nonce": "f" * 32, "once": OWNER_ONCE.lower()}, {"nonce": "f" * 32, "once": OWNER_ONCE + "A"}):
        res = c.post("/api/session/owner-nonce", json=body, headers=TOKEN_ONLY)
        assert res.status_code == 403 and res.json()["detail"]["code"] == "owner_secret_required", body
        assert res.json()["detail"]["message"] == t("permissions.err.owner_secret_required")
    assert not session.owner_matches("f" * 32)
    assert session._owner_hash is None
    # the real tab, with the secret of the start link, still gets the place afterwards
    assert c.post("/api/session/owner-nonce", json={"nonce": OWNER_NONCE, "once": OWNER_ONCE},
                  headers=TOKEN_ONLY).status_code == 200
    assert call(fresh_owner, "put", "/api/profile/permission-mode", {"mode": "auto"}, HEADERS).status_code == 200


def test_3045_the_secret_is_used_once_and_then_the_place_is_taken(fresh_owner):
    c = fresh_owner.client
    assert c.post("/api/session/owner-nonce", json={"nonce": OWNER_NONCE, "once": OWNER_ONCE}, headers=TOKEN_ONLY).status_code == 200
    assert session._owner_secret_hash is None                              # consumed with the registration
    again = c.post("/api/session/owner-nonce", json={"nonce": "e" * 32, "once": OWNER_ONCE}, headers=TOKEN_ONLY)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "owner_already_registered"
    assert not session.owner_matches("e" * 32) and session.owner_matches(OWNER_NONCE)


def test_3045_an_engine_started_without_the_secret_hash_refuses_every_registration(fresh_owner):
    session.forget_owner_for_tests()                                       # no secret armed: the engine got none
    res = fresh_owner.client.post("/api/session/owner-nonce", json={"nonce": OWNER_NONCE, "once": OWNER_ONCE},
                                  headers=TOKEN_ONLY)
    assert res.status_code == 403 and session._owner_hash is None


def test_3045_the_secret_is_not_kept_by_the_engine_only_its_hash():
    session.forget_owner_for_tests()
    try:
        session.arm_owner_secret_for_tests(OWNER_ONCE)
        assert session._owner_secret_hash != OWNER_ONCE and len(session._owner_secret_hash) == 64
    finally:
        session.forget_owner_for_tests()
        session.arm_owner_secret_for_tests(OWNER_ONCE)
        session.register_owner(OWNER_NONCE, OWNER_ONCE)


@pytest.mark.parametrize("nonce", ["", "short", "g" * 32, "A" * 32, "0" * 31, "0" * 33, "0" * 64, "../" * 11])
def test_1035_a_registration_has_to_be_128_bits_of_hex(fresh_owner, nonce):
    res = fresh_owner.client.post("/api/session/owner-nonce", json={"nonce": nonce, "once": OWNER_ONCE}, headers=TOKEN_ONLY)
    assert res.status_code in (400, 422)
    assert not session.owner_matches(nonce)
    assert call(fresh_owner, "put", "/api/profile/permission-mode", {"mode": "auto"}, HEADERS).status_code == 403


def test_1035_the_engine_keeps_a_hash_in_memory_and_writes_nothing(fresh_owner, caplog):
    caplog.set_level(logging.DEBUG)
    body = {"nonce": OWNER_NONCE, "once": OWNER_ONCE}
    assert fresh_owner.client.post("/api/session/owner-nonce", json=body, headers=TOKEN_ONLY).status_code == 200
    call(fresh_owner, "put", "/api/profile/permission-mode", {"mode": "auto"}, HEADERS)
    call(fresh_owner, "put", "/api/profile/permission-mode", {"mode": "auto"}, TOKEN_ONLY)
    assert session._owner_hash and session._owner_hash != OWNER_NONCE and len(session._owner_hash) == 64
    needle = OWNER_NONCE.encode()
    for folder in (fresh_owner.tmp,):
        for path in folder.rglob("*"):
            if path.is_file() and not path.name.endswith((".sock", ".lock")):
                assert needle not in path.read_bytes(), path                  # not in the database, the journal, a log
    assert OWNER_NONCE not in caplog.text


def test_1035_a_restarted_engine_that_was_given_the_hash_accepts_the_same_tab():
    import hashlib

    code = hashlib.sha256(OWNER_NONCE.encode()).hexdigest()
    session.forget_owner_for_tests()
    try:
        session._owner_hash = code                          # what `session.adopt(token, hash)` does at start
        assert session.owner_matches(OWNER_NONCE) and not session.owner_matches("f" * 32)
        assert session.register_owner(OWNER_NONCE) == session.REGISTERED
        assert session.register_owner("f" * 32) == session.TAKEN
    finally:
        session.forget_owner_for_tests()
        session.arm_owner_secret_for_tests(OWNER_ONCE)
        session.register_owner(OWNER_NONCE, OWNER_ONCE)


def test_1035_every_owner_call_of_the_router_declares_the_header():
    """A new route that changes something for the owner has to be added to OWNER_CALLS above: this lists the routes."""
    from hokma.permissions.api import router

    needs = {(next(iter(r.methods)), r.path) for r in router.routes
             if any(p.alias == "x-hokma-owner" for p in r.dependant.header_params)}
    expected = {(m.upper(), route.replace("/1/", "/{item_id}/").replace("/abc/", "/{point_id}/")) for m, route, _ in OWNER_CALLS}
    assert needs == expected
