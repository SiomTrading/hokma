"""Task 3062: the remembered session. A cookie the engine issues, rotates on every use and revokes when an old copy
is used; its verifier lives in the credential store (a memory one here, never the real one).

The cookie alone never opens the session. The browser also keeps a private key (ECDSA P-256) that it isolates by
origin, and every `resume` carries a signature of a challenge of one use. `Browser` (p256_support) plays the page."""
import hashlib
import json
import logging
import stat
import sys
import threading

import keyring.errors
import pytest
from fastapi.testclient import TestClient
from keyring.backends import fail
from starlette.websockets import WebSocketDisconnect

from hokma.core import config, handoff, resume, session
from hokma.core.origin import hosts_for_port
from hokma.core.resume import COOKIE_NAME, ENTRY, SERVICE, ResumeStore, Verifier
from hokma.permissions import api as permissions_api
from hokma.rpc.ws import SESSION_TOKEN
from p256_support import Browser
from permissions_support import HEADERS, OWNER_NONCE, OWNER_ONCE, MemoryKeyring, owner  # noqa: F401  (fixture)

TOKEN_ONLY = {"X-Hokma-Token": SESSION_TOKEN}
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
TAB = "ab" * 16                                    # the owner confirmation a returning tab makes
THIEF = "cd" * 16
DAY = 86400.0
PORT = 8765


def ALLOW(x, y):                                   # a signature check that always passes (store level tests)
    return True


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class Loopback:
    """ASGI wrapper that gives the connection the addresses a real one from this computer has. The TestClient
    reports a peer called "testclient", which the engine rightly does not take for the loopback."""

    def __init__(self, app, client=("127.0.0.1", 50000), server=("127.0.0.1", PORT)) -> None:
        self.app, self.client, self.server = app, client, server

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "client": self.client, "server": self.server}
        await self.app(scope, receive, send)


class Lab:
    def __init__(self, owner, backend, clock, store, book) -> None:
        self.owner, self.app = owner, owner.client.app
        self.keyring, self.clock, self.store, self.book = backend, clock, store, book
        self.own = config.browser_host()                    # the name of this installation
        self.host = f"{self.own}:{PORT}"
        self.browser = Browser()                            # the key the page keeps
        self.client = TestClient(Loopback(self.app), base_url=f"http://127.0.0.1:{PORT}")

    def start(self):
        """The first registration of the tab, as the page does it with the one-time secret of the start link."""
        return self.client.post("/api/session/owner-nonce", headers=TOKEN_ONLY,
                                json={"nonce": OWNER_NONCE, "once": OWNER_ONCE})

    def handoff(self, headers=None, host=None):
        return self.client.post("/api/session/handoff-code", headers={
            **TOKEN_ONLY, "X-Hokma-Owner": OWNER_NONCE, **({"Host": host} if host else {}), **(headers or {})})

    def code(self) -> str:
        res = self.handoff()
        assert res.status_code == 200, res.text
        return res.json()["code"]

    def enter(self, code=None, nonce=TAB, browser=None, host=None, headers=None, key=None):
        code = code if code is not None else self.code()
        browser = browser or self.browser
        self.client.cookies.clear()
        return self.client.post("/api/session/enter", headers={"Host": host or self.host, **(headers or {})},
                                json={"code": code, "nonce": nonce, "public_key": key or browser.public_key_body()})

    def challenge(self, headers=SAME_ORIGIN, host=None, value=None):
        self.client.cookies.clear()
        sent = {"Host": host or self.host, **headers}
        if value is not None:
            sent["Cookie"] = f"{COOKIE_NAME}={value}"
        return self.client.post("/api/session/challenge", headers=sent)

    def challenge_value(self, value=None) -> str:
        res = self.challenge(value=value)
        assert res.status_code == 200, res.text
        return res.json()["challenge"]

    def challenge_for_any_cookie(self, value) -> str:
        """The challenge a resume test needs even when the cookie is one the engine would not give a challenge to
        (revoked, never issued, malformed): the engine refuses those at the challenge endpoint (tested on its own),
        so the book is asked directly with the same binding the endpoint would use."""
        res = self.challenge(value=value)
        if res.status_code == 200:
            return res.json()["challenge"]
        return self.book.new_challenge(resume.digest_of(value if isinstance(value, str) else "no cookie"))

    def resume(self, value, nonce=TAB, headers=SAME_ORIGIN, body=None, browser=None, challenge=None, signature=None):
        """What the page does: a challenge, then the cookie and the signature of the key it keeps."""
        sent = {"Host": self.host, **headers}
        if body is None:
            challenge = challenge if challenge is not None else self.challenge_for_any_cookie(value)
            signature = signature if signature is not None else (browser or self.browser).signature_for(challenge, nonce)
            body = {"nonce": nonce, "challenge": challenge, "signature": signature}
        self.client.cookies.clear()
        if value is not None:
            sent["Cookie"] = f"{COOKIE_NAME}={value}"
        return self.client.post("/api/session/resume", json=body, headers=sent)

    def guard_rows(self):
        with self.owner.db.read() as u:
            return [e for e in u.guard_events.list() if e.guard == "session"]

    def put_mode(self, confirmation):
        return self.client.put("/api/profile/permission-mode", json={"mode": "auto"},
                               headers={**TOKEN_ONLY, "X-Hokma-Owner": confirmation})


def set_cookie(res) -> dict | None:
    """The `hokma_resume` cookie of a response as {value, attrs{...}}, or None."""
    for line in res.headers.get_list("set-cookie"):
        parts = [p.strip() for p in line.split(";")]
        name, _, value = parts[0].partition("=")
        if name == COOKIE_NAME:
            attrs = {}
            for part in parts[1:]:
                key, _, val = part.partition("=")
                attrs[key.lower()] = val
            return {"value": value, "attrs": attrs}
    return None


@pytest.fixture
def lab(owner):  # noqa: F811
    """Nothing registered yet (as at the start of Hokma), a memory credential store and a clock the test moves."""
    session.forget_owner_for_tests()
    session.arm_owner_secret_for_tests(OWNER_ONCE)
    backend, clock = MemoryKeyring(), Clock()
    store, book = ResumeStore(backend, clock), handoff.Handoff(clock)
    previous, previous_book = resume.use_store_for_tests(store), handoff.use_book_for_tests(book)
    owner.client.app.state.allowed_hosts = hosts_for_port(PORT, config.browser_host())
    permissions_api.reset_bad_signature_throttle_for_tests()
    yield Lab(owner, backend, clock, store, book)
    resume.use_store_for_tests(previous)
    handoff.use_book_for_tests(previous_book)
    session.forget_owner_for_tests()
    session.arm_owner_secret_for_tests(OWNER_ONCE)
    assert session.register_owner(OWNER_NONCE, OWNER_ONCE) == session.REGISTERED


@pytest.fixture
def remembered(lab):
    """A started tab with its lineage: the cookie value it holds, after registering, handoff and enter."""
    assert lab.start().status_code == 200
    res = lab.enter()
    assert res.status_code == 200, res.text
    return set_cookie(res)["value"]


def rotate(lab, value, nonce=TAB):
    res = lab.resume(value, nonce)
    assert res.status_code == 200, res.text
    return set_cookie(res)["value"]


# ------------------------------------------------------------------------------ registration, handoff, enter

def test_the_registration_of_the_owner_never_issues_a_cookie(lab):
    res = lab.client.post("/api/session/owner-nonce", headers=TOKEN_ONLY,
                          json={"nonce": OWNER_NONCE, "once": OWNER_ONCE, "remember": True})
    assert res.status_code == 200 and res.json() == {"registered": True}
    assert set_cookie(res) is None and lab.keyring.items == {}
    again = lab.client.post("/api/session/owner-nonce", headers=TOKEN_ONLY, json={"nonce": OWNER_NONCE})
    assert again.status_code == 200 and again.json() == {"registered": True} and set_cookie(again) is None


def test_a_failed_registration_issues_nothing(lab):
    bad = lab.client.post("/api/session/owner-nonce", headers=TOKEN_ONLY,
                          json={"nonce": OWNER_NONCE, "once": "X" * 22, "remember": True})
    assert bad.status_code == 403 and set_cookie(bad) is None and lab.keyring.items == {}
    no_token = lab.client.post("/api/session/owner-nonce", json={"nonce": OWNER_NONCE, "once": OWNER_ONCE})
    assert no_token.status_code == 401 and set_cookie(no_token) is None and lab.keyring.items == {}


def test_the_handoff_code_needs_the_token_and_the_owner_and_only_works_at_the_numeric_address(lab):
    lab.start()
    assert lab.client.post("/api/session/handoff-code").status_code == 401
    assert lab.client.post("/api/session/handoff-code", headers=TOKEN_ONLY).status_code == 403
    assert lab.handoff(headers={"X-Hokma-Owner": THIEF}).status_code == 403
    for host in (f"{lab.own}:{PORT}",):
        res = lab.handoff(host=host)
        assert res.status_code == 403 and res.json()["detail"]["code"] == "session.handoff_origin"
        assert set(res.json()["detail"]) == {"code", "message"} and handoff.CODE_PATTERN.search(res.text) is None
    for host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
        assert lab.handoff(host=host).status_code == 200


def test_the_handoff_code_has_the_agreed_shape_and_the_name_to_carry_it_to(lab):
    lab.start()
    res = lab.handoff()
    body = res.json()
    assert res.status_code == 200 and set(body) == {"code", "host"}
    assert len(body["code"]) == 16 and body["code"].isupper() and handoff.CODE_PATTERN.fullmatch(body["code"])
    assert body["host"] == f"{lab.own}:{PORT}" and body["host"] == f"hokma.localhost:{PORT}"
    assert res.headers["cache-control"] == "no-store"
    assert lab.code() != body["code"]


def test_enter_spends_the_code_and_issues_the_cookie_with_every_attribute(lab):
    lab.start()
    code = lab.code()
    res = lab.enter(code)
    assert res.status_code == 200 and res.json() == {"token": SESSION_TOKEN, "persistent": True}
    cookie = set_cookie(res)
    assert len(cookie["value"]) == 43 and resume.VALUE_PATTERN.fullmatch(cookie["value"])      # 256 bits, base64url
    assert cookie["attrs"] == {"httponly": "", "samesite": "Strict", "path": "/api/session",
                               "max-age": "2592000", "secure": ""}
    assert "domain" not in cookie["attrs"]
    assert res.headers["cache-control"] == "no-store" and res.headers["pragma"] == "no-cache"
    again = lab.enter(code)                                            # one use
    assert again.status_code == 401 and again.json()["detail"]["code"] == "session.resume_denied"
    assert set_cookie(again) is None and SESSION_TOKEN not in again.text


def test_enter_registers_the_confirmation_of_the_tab(lab):
    lab.start()
    assert lab.put_mode(TAB).status_code == 403
    assert lab.enter().status_code == 200
    assert lab.put_mode(TAB).status_code == 200


def test_a_new_code_cancels_the_previous_one(lab):
    lab.start()
    first, second = lab.code(), lab.code()
    assert lab.enter(first).status_code == 401
    assert lab.enter(second).status_code == 200


def test_a_code_expires_after_sixty_seconds(lab):
    lab.start()
    code = lab.code()
    lab.clock.now += 61
    res = lab.enter(code)
    assert res.status_code == 401 and set_cookie(res) is None and lab.keyring.items == {}
    fresh = lab.code()
    lab.clock.now += 59
    assert lab.enter(fresh).status_code == 200


@pytest.mark.parametrize("code", ["", "short", "A" * 16, "a" * 16, "A" * 17, "0" * 16, "../" * 5 + "A", "\u00c9" * 16])
def test_a_wrong_code_is_denied_and_does_not_spend_the_real_one(lab, code):
    lab.start()
    real = lab.code()
    res = lab.enter(code)
    assert res.status_code == 401 and set_cookie(res) is None and lab.keyring.items == {}
    assert lab.enter(real).status_code == 200


def test_enter_at_the_numeric_address_is_denied_and_keeps_the_code(lab):
    lab.start()
    code = lab.code()
    for host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
        res = lab.enter(code, host=host)
        assert res.status_code == 401 and set_cookie(res) is None and SESSION_TOKEN not in res.text
    assert lab.keyring.items == {}
    assert lab.enter(code).status_code == 200                          # not consumed by the refused attempts


@pytest.mark.parametrize("mutate", [
    lambda b: {"x": b["x"], "y": b["x"]},                              # not on the curve
    lambda b: {"x": b["x"][:-1], "y": b["y"]},                         # short
    lambda b: {"x": b["x"] + "A", "y": b["y"]},                        # long
    lambda b: {"x": "!" * 43, "y": b["y"]},                            # not base64url
    lambda b: {"x": "A" * 43, "y": "A" * 43},                          # the zero point is not on the curve
])
def test_enter_refuses_a_public_key_that_is_not_a_point_of_the_curve(lab, mutate):
    lab.start()
    code = lab.code()
    res = lab.enter(code, key=mutate(lab.browser.public_key_body()))
    assert res.status_code == 401 and set_cookie(res) is None and lab.keyring.items == {}
    assert lab.enter(code).status_code == 200                          # a refused body did not spend the code


def test_enter_refuses_a_confirmation_of_the_wrong_shape(lab):
    lab.start()
    code = lab.code()
    for nonce in ("", "short", "g" * 32, "A" * 32):
        assert lab.enter(code, nonce=nonce).status_code == 401
    assert lab.keyring.items == {} and lab.enter(code).status_code == 200


def test_enter_needs_the_origin_checks_of_the_whole_app(lab):
    lab.start()
    code = lab.code()
    assert lab.enter(code, headers={"Origin": "http://evil.com"}).status_code == 403
    assert lab.enter(code, host="evil.com").status_code == 403
    assert lab.enter(code, host=f"{lab.own}:9000").status_code == 403
    assert lab.keyring.items == {} and lab.enter(code).status_code == 200


def test_only_the_hash_and_the_public_key_reach_the_credential_store(lab, remembered):
    stored = lab.keyring.stored()
    assert set(stored) == {"lineage", "gen", "hash", "prev_hash", "prev_until", "created_at", "last_used_at",
                           "pub_x", "pub_y"}
    assert stored["hash"] == hashlib.sha256(remembered.encode()).hexdigest()
    assert (stored["pub_x"], stored["pub_y"]) == lab.browser.public
    assert stored["gen"] == 1 and stored["prev_hash"] is None
    assert remembered not in "".join(lab.keyring.items.values())
    assert str(lab.browser.d) not in "".join(lab.keyring.items.values())


def test_a_second_enter_replaces_the_lineage(lab, remembered):
    other = Browser()
    res = lab.enter(browser=other)
    assert res.status_code == 200
    assert (lab.keyring.stored()["pub_x"], lab.keyring.stored()["pub_y"]) == other.public
    assert lab.resume(remembered).status_code == 401                   # the old cookie belongs to a lineage that is gone


# ------------------------------------------------------------------------------ the key is what protects the cookie

def test_a_stolen_cookie_without_the_key_gets_nothing_and_changes_nothing(lab, remembered):
    before = dict(lab.keyring.items)
    res = lab.resume(remembered, THIEF, browser=Browser())                 # the thief signs with a key of its own
    assert res.status_code == 401 and res.json()["detail"]["code"] == "session.resume_denied"
    assert SESSION_TOKEN not in res.text and set_cookie(res) is None       # no token, and no order to drop the cookie
    assert lab.keyring.items == before
    assert lab.put_mode(THIEF).status_code == 403
    rows = lab.guard_rows()
    assert len(rows) == 1 and rows[0].decision == "blocked"
    assert lab.resume(remembered).status_code == 200                      # the owner is not locked out


def test_an_old_stolen_cookie_without_the_key_does_not_revoke_the_owner(lab, remembered):
    """Before: an old copy used after the window revoked the lineage by itself. Now it needs the key as well."""
    current = rotate(lab, remembered)
    lab.clock.now += 11
    before = dict(lab.keyring.items)
    res = lab.resume(remembered, THIEF, browser=Browser())
    assert res.status_code == 401 and res.json()["detail"]["code"] == "session.resume_denied"
    assert lab.keyring.items == before and set_cookie(res) is None
    assert lab.put_mode(TAB).status_code == 200                           # the confirmation of the lineage is intact
    assert rotate(lab, current) != current                                # and the owner keeps going


def test_an_old_cookie_with_the_right_key_still_revokes(lab, remembered):
    rotate(lab, remembered)
    lab.clock.now += 11
    res = lab.resume(remembered, THIEF)
    assert res.status_code == 401 and res.json()["detail"]["code"] == "session.revoked"
    assert lab.keyring.items == {}


def test_a_server_on_the_same_name_and_another_port_cannot_use_the_cookie_it_receives(lab, remembered):
    """The browser sends the cookie to the same name on any port. A server an agent starts there reads the value; it
    has no key (the browser keeps it per origin, port included) and the engine only answers for its own port."""
    for host in (f"{lab.own}:9000", "127.0.0.1:9000", "localhost:9000"):
        assert lab.challenge(host=host).status_code == 403
        fixed = lab.challenge_value(remembered)
        res = lab.resume(remembered, headers={**SAME_ORIGIN, "Host": host}, challenge=fixed)
        assert res.status_code == 403 and SESSION_TOKEN not in res.text
    for host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):                # right port, not the name of the cookie
        assert lab.challenge(host=host).status_code == 401
    before = dict(lab.keyring.items)
    attacker = Browser()                                                   # it replays the cookie from its own process
    for _ in range(3):
        res = lab.resume(remembered, THIEF, browser=attacker)
        assert res.status_code == 401 and SESSION_TOKEN not in res.text and set_cookie(res) is None
    assert lab.keyring.items == before and lab.put_mode(THIEF).status_code == 403
    assert lab.resume(remembered).status_code == 200


def test_a_signature_is_bound_to_its_challenge_and_its_confirmation(lab, remembered):
    first, second = lab.challenge_value(remembered), lab.challenge_value(remembered)
    mine = lab.browser.signature_for(first, TAB)
    assert lab.resume(remembered, TAB, challenge=second, signature=mine).status_code == 401      # other challenge
    third = lab.challenge_value(remembered)
    assert lab.resume(remembered, THIEF, challenge=third, signature=lab.browser.signature_for(third, TAB)
                      ).status_code == 401                                                        # other confirmation
    assert lab.keyring.stored()["gen"] == 1
    fourth = lab.challenge_value(remembered)
    assert lab.resume(remembered, TAB, challenge=fourth, signature=lab.browser.signature_for(fourth, TAB)
                      ).status_code == 200


@pytest.mark.parametrize("signature", ["", "A" * 86, "A" * 85, "A" * 87, "!" * 86, "A" * 86 + "="])
def test_a_signature_of_the_wrong_shape_is_denied(lab, remembered, signature):
    res = lab.resume(remembered, signature=signature)
    assert res.status_code in (401, 422) and SESSION_TOKEN not in res.text
    assert lab.keyring.stored()["gen"] == 1


def test_a_signature_with_r_or_s_out_of_range_is_denied(lab, remembered):
    from hokma.core import p256

    for r, s in ((0, 1), (1, 0), (p256.N, 1), (1, p256.N)):
        raw = p256.b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        assert lab.resume(remembered, signature=raw).status_code == 401
    assert lab.keyring.stored()["gen"] == 1


def test_the_failed_signatures_leave_at_most_one_record_every_thirty_seconds(lab, remembered):
    for _ in range(4):
        assert lab.resume(remembered, THIEF, browser=Browser()).status_code == 401
    assert len(lab.guard_rows()) == 1


# ------------------------------------------------------------------------------------------------ the challenge

def test_the_challenge_has_the_agreed_shape_and_never_caches(lab, remembered):
    res = lab.challenge(value=remembered)
    body = res.json()
    assert res.status_code == 200 and set(body) == {"challenge"} and len(body["challenge"]) == 43
    assert handoff.CHALLENGE_PATTERN.fullmatch(body["challenge"]) and res.headers["cache-control"] == "no-store"
    assert lab.challenge(value=remembered).json()["challenge"] != body["challenge"]


def test_the_challenge_needs_the_own_name_and_the_same_origin_fetch_metadata(lab):
    for headers in ({}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "none"}):
        res = lab.challenge(headers=headers)
        assert res.status_code == 401 and res.json()["detail"]["code"] == "session.resume_denied"
    for host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
        assert lab.challenge(host=host).status_code == 401
    assert lab.challenge(host="evil.com").status_code == 403
    assert lab.book.pending_challenges() == 0


def test_the_signature_carries_the_exact_origin_with_its_port(lab, remembered):
    """A page on `hokma.localhost:<other port>` holds the same cookie and could ask this engine for a challenge, but
    what its browser key signs there names that other origin, so it never verifies here."""
    challenge = lab.challenge_value(remembered)
    elsewhere = lab.browser.signature_for(challenge, TAB, origin=f"http://hokma.localhost:{PORT + 1}")
    res = lab.resume(remembered, challenge=challenge, signature=elsewhere)
    assert res.status_code == 401 and lab.keyring.stored()["gen"] == 1
    no_origin = lab.browser.sign(f"hokma-resume:{challenge}:{TAB}".encode("utf-8"))
    assert lab.resume(remembered, signature=no_origin).status_code == 401
    assert lab.resume(remembered).status_code == 200                           # the right origin works


def test_the_key_of_a_lineage_cannot_be_swapped_by_signing_with_another_one(lab, remembered):
    before = (lab.keyring.stored()["pub_x"], lab.keyring.stored()["pub_y"])
    attacker = Browser()
    challenge = lab.challenge_value(remembered)
    res = lab.resume(remembered, challenge=challenge, signature=attacker.signature_for(challenge, TAB))
    assert res.status_code == 401
    assert (lab.keyring.stored()["pub_x"], lab.keyring.stored()["pub_y"]) == before == lab.browser.public


def test_a_challenge_works_once_even_when_the_attempt_fails(lab, remembered):
    challenge = lab.challenge_value(remembered)
    good = lab.browser.signature_for(challenge, TAB)
    bad = lab.resume(remembered, challenge=challenge, signature=Browser().signature_for(challenge, TAB))
    assert bad.status_code == 401
    replay = lab.resume(remembered, challenge=challenge, signature=good)    # the right signature, a spent challenge
    assert replay.status_code == 401 and lab.keyring.stored()["gen"] == 1
    assert lab.resume(remembered).status_code == 200


def test_a_challenge_expires_after_thirty_seconds(lab, remembered):
    challenge = lab.challenge_value(remembered)
    lab.clock.now += 31
    assert lab.resume(remembered, challenge=challenge).status_code == 401
    challenge = lab.challenge_value(remembered)
    lab.clock.now += 29
    assert lab.resume(remembered, challenge=challenge).status_code == 200


def test_a_challenge_the_engine_never_issued_is_denied(lab, remembered):
    assert lab.resume(remembered, challenge="A" * 43).status_code == 401
    assert lab.resume(remembered, challenge="short").status_code == 401
    assert lab.keyring.stored()["gen"] == 1


def test_the_last_tab_to_ask_always_gets_a_challenge_and_only_its_own_cookie_loses_the_oldest(lab, remembered):
    held = [lab.challenge_value(remembered) for _ in range(handoff.MAX_PENDING_PER_COOKIE)]
    newest = lab.challenge_value(remembered)                               # the fifth: the oldest of this cookie goes
    assert lab.book.pending_challenges() == handoff.MAX_PENDING_PER_COOKIE
    assert lab.resume(remembered, challenge=held[0]).status_code == 401
    assert lab.resume(remembered, challenge=newest).status_code == 200


def test_a_stranger_cannot_fill_the_book_or_ask_for_a_challenge(lab, remembered):
    assert lab.challenge().status_code == 401                              # no cookie
    assert lab.challenge(value="A" * 43).status_code == 401                # a well formed cookie nobody issued
    assert lab.book.pending_challenges() == 0
    legit = lab.challenge_value(remembered)
    for _ in range(50):
        lab.challenge(value="B" * 43)
    assert lab.resume(remembered, challenge=legit).status_code == 200


def test_a_challenge_is_bound_to_the_cookie_that_asked_for_it(lab, remembered):
    challenge = lab.challenge_value(remembered)
    other = rotate(lab, remembered)                                         # the cookie moved on to a new value
    assert lab.resume(other, challenge=challenge).status_code == 401       # a challenge of the old cookie is not for it


# ----------------------------------------------------------------------------- the name and the loopback

def test_the_panel_name_is_fixed_and_leaves_no_label_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "one"))
    assert config.browser_host() == "hokma.localhost"
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "two"))
    assert config.browser_host() == "hokma.localhost"                     # the same on every installation
    assert not (tmp_path / "one" / "host.label").exists() and not (tmp_path / "two" / "host.label").exists()


def test_the_engine_answers_to_its_own_name_on_the_loopback_only(lab):
    health = {"Host": lab.host}
    assert lab.client.get("/health", headers=health).status_code == 200
    far = TestClient(Loopback(lab.app, client=("192.168.1.9", 40000)), base_url=f"http://127.0.0.1:{PORT}")
    assert far.get("/health", headers=health).status_code == 403                  # peer is not the loopback
    unknown = TestClient(lab.app, base_url=f"http://127.0.0.1:{PORT}")           # the TestClient peer is "testclient"
    assert unknown.get("/health", headers=health).status_code == 403
    assert unknown.get("/health", headers={"Host": f"127.0.0.1:{PORT}"}).status_code == 200      # as before
    assert unknown.get("/health", headers={"Host": f"localhost:{PORT}"}).status_code == 200


@pytest.mark.parametrize("client,server,expected", [
    (("127.0.0.1", 1), ("127.0.0.1", PORT), 200), (("::1", 1), ("::1", PORT), 200),
    (("::ffff:127.0.0.1", 1), ("::ffff:127.0.0.1", PORT), 200), (("::1", 1), ("127.0.0.1", PORT), 200),
    (("10.0.0.5", 1), ("127.0.0.1", PORT), 403), (("127.0.0.1", 1), ("192.168.1.5", PORT), 403),
    (("127.0.0.1", 1), ("0.0.0.0", PORT), 403), (None, ("127.0.0.1", PORT), 403),
    (("127.0.0.1", 1), None, 403), (("127.0.0.1", 1), ("/run/hokma.sock", None), 403),
])
def test_both_the_peer_and_the_local_address_must_be_the_loopback_for_the_own_name(lab, client, server, expected):
    wrapped = TestClient(Loopback(lab.app, client, server), base_url=f"http://127.0.0.1:{PORT}")
    assert wrapped.get("/health", headers={"Host": lab.host}).status_code == expected
    assert wrapped.get("/health", headers={"Host": f"127.0.0.1:{PORT}"}).status_code == 200


def test_the_own_name_is_refused_on_a_websocket_from_a_peer_that_is_not_the_loopback(lab):
    far = TestClient(Loopback(lab.app, client=("192.168.1.9", 40000)), base_url=f"http://127.0.0.1:{PORT}")
    with pytest.raises(WebSocketDisconnect) as exc:
        with far.websocket_connect(f"/rpc?token={SESSION_TOKEN}", headers={"host": lab.host}):
            pass
    assert exc.value.code == 1008
    with lab.client.websocket_connect(f"/rpc?token={SESSION_TOKEN}", headers={"host": lab.host}) as ws:
        ws.send_text('{"id": 1}')
        assert ws.receive_json()["id"] == 1


def test_resume_over_a_connection_that_is_not_the_loopback_is_refused(lab, remembered):
    far = TestClient(Loopback(lab.app, client=("192.168.1.9", 40000)), base_url=f"http://127.0.0.1:{PORT}")
    challenge = lab.challenge_value(remembered)
    res = far.post("/api/session/resume", headers={"Host": lab.host, **SAME_ORIGIN, "Cookie": f"{COOKIE_NAME}={remembered}"},
                   json={"nonce": TAB, "challenge": challenge, "signature": lab.browser.signature_for(challenge, TAB)})
    assert res.status_code == 403 and lab.keyring.stored()["gen"] == 1


# ------------------------------------------------------------------------------------------------ rotation

def test_resume_gives_the_token_rotates_the_cookie_and_never_caches(lab, remembered):
    first = stored_hash = lab.keyring.stored()["hash"]
    lab.clock.now += 60
    res = lab.resume(remembered)
    assert res.status_code == 200 and res.json() == {"token": SESSION_TOKEN}
    assert res.headers["cache-control"] == "no-store" and res.headers["pragma"] == "no-cache"
    cookie = set_cookie(res)
    assert cookie["value"] != remembered and cookie["attrs"]["path"] == "/api/session"
    assert cookie["attrs"]["max-age"] == "2592000" and "httponly" in cookie["attrs"] and "secure" in cookie["attrs"]
    stored = lab.keyring.stored()
    assert stored["gen"] == 2 and stored["prev_hash"] == first == stored_hash
    assert stored["hash"] == hashlib.sha256(cookie["value"].encode()).hexdigest()
    assert stored["prev_until"] == lab.clock.now + 10 and stored["last_used_at"] == lab.clock.now


def test_resume_registers_the_confirmation_of_the_returning_tab_and_replaces_the_previous_one(lab, remembered):
    assert lab.put_mode(TAB).status_code == 200                    # `enter` registered it
    other = "12" * 16
    value = rotate(lab, remembered, other)
    assert lab.put_mode(other).status_code == 200 and lab.put_mode(TAB).status_code == 403
    other = "34" * 16
    rotate(lab, value, other)
    assert lab.put_mode(other).status_code == 200
    assert lab.put_mode(TAB).status_code == 403                    # the older confirmation of the lineage is gone
    assert lab.put_mode(OWNER_NONCE).status_code == 200            # the first registration still counts, as before


def test_the_value_used_inside_ten_seconds_is_accepted_one_time_without_a_new_cookie(lab, remembered):
    current = rotate(lab, remembered)
    lab.clock.now += 9.9
    res = lab.resume(remembered, THIEF)                            # the second tab that loaded at once
    assert res.status_code == 200 and res.json() == {"token": SESSION_TOKEN} and set_cookie(res) is None
    assert lab.keyring.stored() is not None and lab.guard_rows() == []
    assert lab.put_mode(THIEF).status_code == 200                  # the last tab to resume holds the confirmation
    assert lab.resume(current).status_code == 200                  # the cookie the first tab holds still works


def test_the_previous_value_is_not_accepted_a_second_time_even_inside_the_window(lab, remembered):
    rotate(lab, remembered)
    assert lab.resume(remembered).status_code == 200
    again = lab.resume(remembered)
    assert again.status_code == 401 and again.json()["detail"]["code"] == "session.revoked"


def test_two_loads_at_once_do_not_revoke(lab, remembered):
    results, barrier = [], threading.Barrier(2)

    def load():
        barrier.wait()
        results.append(lab.store.resume(remembered, ALLOW).status)

    threads = [threading.Thread(target=load) for _ in range(2)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert results == [resume.OK, resume.OK]
    assert lab.guard_rows() == [] and lab.keyring.stored() is not None


def test_the_current_cookie_keeps_working_across_many_rotations(lab, remembered):
    value = remembered
    for _ in range(12):
        lab.clock.now += 30
        value = rotate(lab, value)
    assert lab.keyring.stored()["gen"] == 13 and lab.guard_rows() == []


# ------------------------------------------------------------------------------------------------ revocation

def test_an_old_value_after_the_window_revokes_the_lineage_and_leaves_the_record(lab, remembered):
    stolen = remembered
    rotate(lab, remembered, TAB)                                   # the owner's tab rotates it
    assert lab.put_mode(TAB).status_code == 200
    lab.clock.now += 11
    res = lab.resume(stolen, THIEF)                                # the copy is used 11 s later
    assert res.status_code == 401 and res.json()["detail"]["code"] == "session.revoked"
    assert "token" not in res.text.lower().replace("session", "") and SESSION_TOKEN not in res.text
    assert res.headers["cache-control"] == "no-store"
    assert set_cookie(res)["attrs"]["max-age"] == "0"
    assert lab.keyring.items == {}                                 # the verifier is gone from the credential store
    rows = lab.guard_rows()
    assert len(rows) == 1 and rows[0].decision == "blocked"
    assert lab.put_mode(TAB).status_code == 403                    # the confirmation of the lineage is forgotten
    assert lab.put_mode(THIEF).status_code == 403


def test_after_a_revocation_every_hash_of_the_lineage_answers_revoked_and_one_row_is_written(lab, remembered):
    second = rotate(lab, remembered)
    lab.clock.now += 11
    assert lab.resume(remembered).json()["detail"]["code"] == "session.revoked"
    for value in (second, remembered):                             # the owner's own cookie sees the notice too
        res = lab.resume(value)
        assert res.status_code == 401 and res.json()["detail"]["code"] == "session.revoked"
    assert len(lab.guard_rows()) == 1


def test_a_perfect_thief_gets_one_answer_and_the_next_legitimate_refresh_ends_it(lab, remembered):
    """R1-S4, worst case: the thief holds the cookie AND the key and rotates first; the owner's tab still holds the previous value."""
    assert lab.resume(remembered, THIEF).status_code == 200
    assert lab.put_mode(THIEF).status_code == 200
    lab.clock.now += 600                                           # the owner's refresh, ten minutes later
    owner_side = lab.resume(remembered, TAB)
    assert owner_side.status_code == 401 and owner_side.json()["detail"]["code"] == "session.revoked"
    assert lab.put_mode(THIEF).status_code == 403                  # what the thief got no longer passes
    assert lab.keyring.items == {} and len(lab.guard_rows()) == 1


def test_a_value_the_engine_never_issued_is_denied_and_does_not_touch_the_lineage(lab, remembered):
    before = lab.keyring.stored()
    res = lab.resume("A" * 43)
    assert res.status_code == 401 and res.json()["detail"]["code"] == "session.resume_denied"
    assert lab.keyring.stored() == before and lab.guard_rows() == []
    assert lab.resume(remembered).status_code == 200


def test_a_new_lineage_after_a_revocation_works(lab, remembered):
    rotate(lab, remembered)
    lab.clock.now += 11
    lab.resume(remembered)
    assert lab.keyring.items == {}
    issued = lab.store.issue(lab.browser.public, TAB)
    assert lab.resume(issued.value).status_code == 200


# ---------------------------------------------------------------------------------------------- expiry

def test_thirty_one_days_without_use_is_denied_and_the_verifier_is_removed(lab, remembered):
    lab.clock.now += 31 * DAY
    res = lab.resume(remembered)
    assert res.status_code == 401 and res.json()["detail"]["code"] == "session.resume_denied"
    assert res.headers["cache-control"] == "no-store" and SESSION_TOKEN not in res.text
    assert lab.keyring.items == {} and lab.guard_rows() == []


def test_every_use_renews_the_thirty_days(lab, remembered):
    value = remembered
    for _ in range(3):
        lab.clock.now += 29 * DAY
        value = rotate(lab, value)
    assert lab.resume(value).status_code == 200
    lab.clock.now += 31 * DAY
    assert lab.resume(value).status_code == 401


# ---------------------------------------------------------------------------------------- hostile requests

def assert_refused(res):
    assert res.status_code in (401, 403)
    assert SESSION_TOKEN not in res.text
    assert "token" not in (res.json().get("detail") or {}) if res.headers.get("content-type", "").startswith("application/json") else True


def test_resume_without_a_cookie_is_denied(lab, remembered):
    res = lab.resume(None)
    assert res.status_code == 401 and res.json()["detail"]["code"] == "session.resume_denied"
    assert_refused(res)


@pytest.mark.parametrize("value", ["", "short", "A" * 42, "A" * 44, "A" * 42 + "=", "../" * 14 + "AA", "A" * 42 + "!"])
def test_resume_with_a_cookie_of_the_wrong_shape_is_denied(lab, remembered, value):
    res = lab.resume(None, headers={**SAME_ORIGIN, "Cookie": f"{COOKIE_NAME}={value}"})
    assert res.status_code == 401 and res.json()["detail"]["code"] == "session.resume_denied"
    assert_refused(res)
    assert lab.keyring.stored() is not None


def test_resume_with_a_foreign_origin_or_host_is_refused(lab, remembered):
    foreign_origin = lab.resume(remembered, headers={**SAME_ORIGIN, "Origin": "http://evil.com"})
    assert foreign_origin.status_code == 403 and SESSION_TOKEN not in foreign_origin.text
    evil_host = lab.resume(remembered, headers={**SAME_ORIGIN, "Host": "evil.com"})
    assert evil_host.status_code == 403 and SESSION_TOKEN not in evil_host.text
    both = lab.resume(remembered, headers={**SAME_ORIGIN, "Host": "evil.com", "Origin": "http://evil.com"})
    assert both.status_code == 403
    numeric = lab.resume(remembered, headers={**SAME_ORIGIN, "Host": f"127.0.0.1:{PORT}"})    # right port, wrong name
    assert numeric.status_code == 401 and SESSION_TOKEN not in numeric.text
    assert lab.keyring.stored()["gen"] == 1                         # none of them rotated anything


@pytest.mark.parametrize("headers", [{}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                                     {"Sec-Fetch-Site": "none"}, {"Sec-Fetch-Site": ""}])
def test_resume_needs_the_same_origin_fetch_metadata(lab, remembered, headers):
    res = lab.resume(remembered, headers=headers)
    assert res.status_code == 401 and res.json()["detail"]["code"] == "session.resume_denied"
    assert SESSION_TOKEN not in res.text and lab.keyring.stored()["gen"] == 1


@pytest.mark.parametrize("body", [{}, {"nonce": ""}, {"nonce": "g" * 32}, {"nonce": TAB}, {"challenge": "A" * 43},
                                  {"nonce": TAB, "challenge": "A" * 43}, {"nonce": TAB, "signature": "A" * 86}])
def test_resume_needs_the_three_fields(lab, remembered, body):
    res = lab.resume(remembered, body=body)
    assert res.status_code in (401, 422) and SESSION_TOKEN not in res.text
    assert lab.keyring.stored()["gen"] == 1


@pytest.mark.parametrize("nonce", ["", "g" * 32, "A" * 32, "0" * 31, "0" * 33])
def test_resume_needs_a_well_formed_confirmation(lab, remembered, nonce):
    res = lab.resume(remembered, nonce)
    assert res.status_code == 401 and SESSION_TOKEN not in res.text
    assert lab.keyring.stored()["gen"] == 1


def test_a_valid_cookie_used_with_a_wrong_hash_never_matches_by_prefix(lab, remembered):
    sibling = remembered[:-1] + ("A" if remembered[-1] != "A" else "B")
    assert lab.resume(sibling).status_code == 401
    assert lab.resume(remembered).status_code == 200


# ----------------------------------------------------------------------------------------------- logout

def test_logout_needs_the_token_and_the_owner(lab, remembered):
    rotate(lab, remembered)
    assert lab.client.post("/api/session/logout").status_code == 401
    assert lab.client.post("/api/session/logout", headers=TOKEN_ONLY).status_code == 403
    assert lab.client.post("/api/session/logout", headers={**TOKEN_ONLY, "X-Hokma-Owner": THIEF}).status_code == 403
    assert lab.keyring.stored() is not None


def test_logout_deletes_the_verifier_expires_the_cookie_and_forgets_the_lineage(lab, remembered):
    value = rotate(lab, remembered)
    res = lab.client.post("/api/session/logout", headers={**TOKEN_ONLY, "X-Hokma-Owner": TAB})
    assert res.status_code == 200 and res.json() == {"logged_out": True}
    assert res.headers["cache-control"] == "no-store"
    cookie = set_cookie(res)
    assert cookie["value"] == "" and cookie["attrs"]["max-age"] == "0" and cookie["attrs"]["path"] == "/api/session"
    assert lab.keyring.items == {}
    assert lab.put_mode(TAB).status_code == 403
    again = lab.resume(value)
    assert again.status_code == 401 and again.json()["detail"]["code"] == "session.resume_denied"
    assert lab.guard_rows() == []                                  # closing it yourself is not a revocation


# -------------------------------------------------------------------------------- credential store cases

def test_without_a_usable_credential_store_the_verifier_lives_in_memory(owner):  # noqa: F811
    store = ResumeStore(fail.Keyring(), Clock())
    assert store.available() is False
    issued = store.issue(Browser().public, TAB)
    assert issued.persistent is False
    second = store.resume(issued.value, ALLOW)
    assert second.status == resume.OK and second.cookie and second.cookie != issued.value
    assert store.resume(second.cookie, ALLOW).status == resume.OK
    assert ResumeStore(fail.Keyring(), Clock()).resume(second.cookie, ALLOW).status == resume.DENIED   # a new process knows nothing


def test_a_restarted_engine_finds_the_verifier_in_the_credential_store(lab, remembered):
    restarted = ResumeStore(lab.keyring, lab.clock)
    outcome = restarted.resume(remembered, ALLOW)
    assert outcome.status == resume.OK and outcome.cookie
    # it does not remember the older hashes of the memory, but the previous one is in the verifier
    lab.clock.now += 11
    assert ResumeStore(lab.keyring, lab.clock).resume(remembered, ALLOW).status == resume.REVOKED


def test_a_damaged_entry_is_the_same_as_no_entry(lab, remembered):
    for damaged in ("", "not json", "[]", "{}", json.dumps({**lab.keyring.stored(), "hash": "zz"}),
                    json.dumps({**lab.keyring.stored(), "gen": "one"}), json.dumps({**lab.keyring.stored(), "prev_hash": 5}),
                    json.dumps({**lab.keyring.stored(), "pub_x": "A" * 43, "pub_y": "A" * 43}),
                    json.dumps({**lab.keyring.stored(), "pub_x": 5}), json.dumps({**lab.keyring.stored(), "pub_y": None})):
        lab.keyring.items[(SERVICE, ENTRY)] = damaged
        assert Verifier.loads(damaged) is None
        assert lab.store.resume(remembered, ALLOW).status == resume.DENIED


def test_an_entry_without_a_public_key_is_readable_but_never_verifies(lab, remembered):
    """A verifier saved before the key existed: the cookie alone is not enough, so nothing opens and nothing changes."""
    older = {k: v for k, v in lab.keyring.stored().items() if not k.startswith("pub_")}
    lab.keyring.items[(SERVICE, ENTRY)] = json.dumps(older)
    loaded = Verifier.loads(json.dumps(older))
    assert loaded is not None and loaded.pub_x is None and loaded.pub_y is None
    outcome = lab.store.resume(remembered, ALLOW)
    assert outcome.status == resume.DENIED and outcome.bad_signature
    assert json.loads(lab.keyring.items[(SERVICE, ENTRY)]) == older
    assert lab.resume(remembered).status_code == 401


def test_the_signature_check_runs_before_anything_changes_and_an_error_in_it_is_a_refusal(lab, remembered):
    before = dict(lab.keyring.items)

    def broken(x, y):
        raise RuntimeError("boom")

    outcome = lab.store.resume(remembered, broken)
    assert outcome.status == resume.DENIED and outcome.bad_signature and lab.keyring.items == before
    rejected = lab.store.resume(remembered, lambda x, y: False)
    assert rejected.status == resume.DENIED and lab.keyring.items == before
    lab.clock.now += 11
    assert lab.store.resume(remembered, lambda x, y: False).status == resume.DENIED     # an old value, no key: no revocation
    assert lab.keyring.items == before


def test_a_credential_store_that_refuses_to_write_keeps_the_verifier_in_memory(lab):
    class Refusing(MemoryKeyring):
        def set_password(self, service, username, password):
            raise keyring.errors.KeyringError("locked")

    backend = Refusing()
    store = ResumeStore(backend, lab.clock)
    issued = store.issue(Browser().public, TAB)
    assert issued.persistent is False and backend.items == {}
    outcome = store.resume(issued.value, ALLOW)
    assert outcome.status == resume.OK
    assert store.resume(outcome.cookie, ALLOW).status == resume.OK


# ----------------------------------------------------------------------------------- hygiene and contracts

def test_neither_the_token_nor_the_cookie_values_reach_the_logs_or_the_database(lab, caplog):
    caplog.set_level(logging.DEBUG)
    lab.start()
    code = lab.code()
    first = set_cookie(lab.enter(code))["value"]
    second = rotate(lab, first)
    challenge = lab.challenge_value(second)
    signature = lab.browser.signature_for(challenge, TAB)
    lab.resume(second, THIEF, browser=Browser())                    # a refused signature, which writes a record
    lab.clock.now += 11
    lab.resume(first)                                              # a revocation, which writes the record
    lab.resume(second)
    secrets_seen = (SESSION_TOKEN, first, second, OWNER_NONCE, TAB, code, challenge, signature)
    for secret in secrets_seen:
        assert secret not in caplog.text, secret[:6]
    for row in lab.guard_rows():
        assert not any(secret in f"{row.reason}{row.tool}" for secret in secrets_seen)
    for path in lab.owner.tmp.rglob("*"):
        if path.is_file() and not path.name.endswith((".sock", ".lock")):
            data = path.read_bytes()
            assert first.encode() not in data and second.encode() not in data and SESSION_TOKEN.encode() not in data, path
    sample = lab.store.issue(lab.browser.public, TAB)
    assert sample.value not in repr(sample) and "value=" not in repr(sample)


def test_guard_events_accepts_the_session_guard_and_still_refuses_unknown_ones(owner):  # noqa: F811
    from hokma.db import IntegrityViolation

    with owner.db.write() as u:
        row = u.guard_events.record("session", "blocked", "a copy was used", tool="resume")
    assert row.guard == "session" and row.decision == "blocked"
    with pytest.raises(IntegrityViolation):
        with owner.db.write() as u:
            u.guard_events.record("sessions", "blocked", "x")


def test_the_session_module_keeps_the_first_registration_and_adds_lineages():
    session.forget_owner_for_tests()
    try:
        session.arm_owner_secret_for_tests(OWNER_ONCE)
        assert session.register_owner_detail(OWNER_NONCE, OWNER_ONCE) == (session.REGISTERED, True)
        assert session.register_owner_detail(OWNER_NONCE) == (session.REGISTERED, False)
        assert session.register_owner("f" * 32) == session.TAKEN
        assert session.register_lineage_owner("L1", TAB) and session.owner_matches(TAB) and session.owner_matches(OWNER_NONCE)
        assert session.register_lineage_owner("L1", THIEF)
        assert session.owner_matches(THIEF) and not session.owner_matches(TAB)
        assert not session.register_lineage_owner("L1", "short") and not session.register_lineage_owner("", TAB)
        session.forget_lineage_owner("L1")
        assert not session.owner_matches(THIEF) and session.owner_matches(OWNER_NONCE)
    finally:
        session.forget_owner_for_tests()
        session.arm_owner_secret_for_tests(OWNER_ONCE)
        session.register_owner(OWNER_NONCE, OWNER_ONCE)


def test_both_catalogs_have_the_new_error_messages():
    from hokma.i18n import catalog

    for key in ("permissions.err.resume_denied", "permissions.err.session_revoked"):
        assert catalog("en")[key] and catalog("es")[key] and catalog("en")[key] != catalog("es")[key]


# ------------------------------------------------------------------ revoke and register cannot race

def test_resume_registers_the_confirmation_under_the_store_lock(lab, remembered):
    seen = []
    real = session.register_lineage_owner

    def probe(lineage, nonce):
        seen.append(lab.store._lock._is_owned())            # the RLock is held by this very thread
        return real(lineage, nonce)

    session.register_lineage_owner = probe
    try:
        assert lab.resume(remembered).status_code == 200
    finally:
        session.register_lineage_owner = real
    assert seen == [True]


def test_a_late_registration_for_a_revoked_lineage_is_refused(lab, remembered):
    lineage = lab.store._load().lineage
    assert lab.resume(remembered).status_code == 200
    lab.store.logout()                                       # the lineage ends
    assert session.register_lineage_owner(lineage, THIEF) is False
    assert session.owner_matches(THIEF) is False


def test_revoking_while_a_resume_registers_never_leaves_a_live_confirmation(lab, remembered):
    """Many resumes race with the revocation. Whatever order they run in, once the lineage is revoked no confirmation
    that a resume registered is accepted."""
    lineage = lab.store._load().lineage
    barrier = threading.Barrier(5)

    def go(i):
        barrier.wait()
        if i == 0:
            lab.store.logout()
        else:
            lab.store.resume(remembered, ALLOW, f"{i:032x}")

    threads = [threading.Thread(target=go, args=(i,)) for i in range(5)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert not any(session.owner_matches(f"{i:032x}") for i in range(1, 5))
    assert session.register_lineage_owner(lineage, "ef" * 16) is False
