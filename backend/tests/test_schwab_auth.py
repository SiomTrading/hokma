"""Sign-in with Schwab and its session (SPEC-015 5.2 and 5.3): the authorization address, the pasted callback, the exchange,
the renewal, the 7-day end and the one notice. Schwab is a local fake server; the credential store is in memory."""
import base64
import json
import logging
import sys
import threading
import time
import types
import urllib.parse

import keyring
import pytest
from live_support import MemoryKeyring
from schwab_support import FAKE_ACCOUNT_NUMBER, FAKE_HASH, FakeSchwab

import hokma.schwab as schwab_package
from hokma.connections import runtime
from hokma.connections.service import ConnectionService
from hokma.connections.store import ConnectionSettings
from hokma.market.providers import get_catalog
from hokma.schwab import auth, http
from hokma.schwab.http import SchwabError

CLIENT_ID = "test-client-id-1234"
CLIENT_SECRET = "test-client-secret-5678"
CODE = "C0.test-code-abcdef@"
T0 = 1_800_000_000
DAY = 86400
REDIRECT = "https://127.0.0.1:8182"
TOKEN_PATH = "/v1/oauth/token"
QUOTES = "/marketdata/v1/quotes"
NUMBERS = "/trader/v1/accounts/accountNumbers"


class Clock:
    def __init__(self, at: float = T0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


class Issuer:
    """The token endpoint of the fake: numbered tokens, optional refresh_token, optional delay, refusals."""

    def __init__(self) -> None:
        self.n = 0
        self.refresh_in_answer = True
        self.delay = 0.0
        self.status = 200
        self.body: dict | None = None
        self.valid = None
        self.lock = threading.Lock()

    def __call__(self, call):
        form = dict(urllib.parse.parse_qsl(call.body.decode()))
        if self.delay:
            time.sleep(self.delay)
        if self.status != 200:
            return self.status, self.body or {"error": "invalid_grant"}
        with self.lock:
            self.n += 1
            n = self.n
        answer = {"access_token": f"AT-SECRET-{n}", "expires_in": 1800, "token_type": "Bearer", "scope": "api",
                  "id_token": "ID-TOKEN-SECRET"}
        if self.refresh_in_answer:
            answer["refresh_token"] = f"RT-SECRET-{n}"
        self.valid = answer["access_token"]
        self.last_form = form
        return 200, self.body or answer


class Env:
    pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    previous = keyring.get_keyring()
    ring = MemoryKeyring()
    keyring.set_keyring(ring)
    settings_path = tmp_path / "connections.json"
    service = ConnectionService(get_catalog(), settings=ConnectionSettings(settings_path))
    runtime.install(service, None)
    added: list[str] = []
    monkeypatch.setattr(service, "mark_added", lambda provider_id: added.append(provider_id), raising=False)
    clock = Clock()
    auth.reset()
    auth.set_clock(clock)
    notices: list[str] = []
    auth.set_notifier(notices.append)

    fake_accounts = types.ModuleType("hokma.schwab.accounts")
    state = {"refreshed": 0}

    def refresh():
        state["refreshed"] += 1
        rows = http.get(NUMBERS)
        return [{"account_ref": f"A{i + 1}", "mask": "••••" + r["accountNumber"][-4:]} for i, r in enumerate(rows)]

    fake_accounts.refresh = refresh
    fake_accounts.listing = lambda: [{"account_ref": "A1", "mask": "••••" + FAKE_ACCOUNT_NUMBER[-4:]}]
    monkeypatch.setitem(sys.modules, "hokma.schwab.accounts", fake_accounts)
    monkeypatch.setattr(schwab_package, "accounts", fake_accounts, raising=False)

    with FakeSchwab() as fake:
        http.set_token_provider(None)                     # the real wiring: the token comes from auth
        issuer = Issuer()
        fake.on("POST", TOKEN_PATH, issuer)
        fake.on("GET", QUOTES, lambda call: (200, {"SPY": {"realtime": True, "quote": {"lastPrice": 512.34}}}))
        fake.on("GET", NUMBERS, [{"accountNumber": FAKE_ACCOUNT_NUMBER, "hashValue": FAKE_HASH}])
        e = Env()
        e.ring, e.service, e.clock, e.fake, e.issuer, e.added, e.notices = ring, service, clock, fake, issuer, added, notices
        e.settings_path, e.accounts_state = settings_path, state
        ring.set_password("hokma-market", "schwab", json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}))
        yield e
    auth.set_notifier(None)
    auth.set_clock(None)
    auth.reset()
    runtime.reset()
    keyring.set_keyring(previous)


def callback(state: str, code: str = "C0.test-code-abcdef%40", extra: str = "&session=abc") -> str:
    return f"{REDIRECT}/?code={code}{extra}&state={state}"


def state_of(url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]


def sign_in(env: Env) -> dict:
    url = auth.start_login()["auth_url"]
    return auth.finish_login(callback(state_of(url)))


def token_posts(env: Env) -> list:
    return [c for c in env.fake.calls if c.method == "POST" and c.path == TOKEN_PATH]


def stored(env: Env) -> dict:
    return json.loads(env.ring.items[("hokma-market", "schwab.session")])


# -------------------------------------------------------------------------------------------- the address

def test_the_authorization_address_is_exact(env):
    url = auth.start_login()["auth_url"]
    state = state_of(url)
    assert url == ("https://api.schwabapi.com/v1/oauth/authorize?client_id=test-client-id-1234"
                   "&redirect_uri=https%3A%2F%2F127.0.0.1%3A8182&response_type=code&state=" + state)
    assert len(base64.urlsafe_b64decode(state + "=")) == 32 and len(state) == 43


def test_start_needs_the_saved_client_id_and_secret(env):
    del env.ring.items[("hokma-market", "schwab")]
    with pytest.raises(SchwabError) as error:
        auth.start_login()
    assert error.value.code == "connections.no_key"


def test_only_one_start_lives_the_new_one_replaces_the_old(env):
    first = auth.start_login()["auth_url"]
    second = auth.start_login()["auth_url"]
    assert state_of(first) != state_of(second)
    with pytest.raises(SchwabError) as error:
        auth.finish_login(callback(state_of(first)))
    assert error.value.code == "connections.schwab_state"
    assert not token_posts(env)
    auth.finish_login(callback(state_of(second)))
    assert len(token_posts(env)) == 1


# -------------------------------------------------------------------------------------------- the pasted address

def test_finish_accepts_the_full_address_and_decodes_once(env):
    url = auth.start_login()["auth_url"]
    auth.finish_login(callback(state_of(url), code="C0.a%2540b%40"))     # %2540 is a literal "%40"
    form = dict(urllib.parse.parse_qsl(token_posts(env)[0].body.decode()))
    assert form["code"] == "C0.a%40b@"


def test_finish_accepts_the_code_alone(env):
    auth.start_login()
    auth.finish_login("  " + CODE + "  ")
    assert dict(urllib.parse.parse_qsl(token_posts(env)[0].body.decode()))["code"] == CODE
    auth.start_login()
    auth.finish_login("C0.test-code-abcdef%40")                           # copied still encoded: decoded once
    assert dict(urllib.parse.parse_qsl(token_posts(env)[1].body.decode()))["code"] == CODE


@pytest.mark.parametrize("make, code", [
    (lambda s: f"http://127.0.0.1:8182/?code=x&state={s}", "connections.schwab_url"),
    (lambda s: f"https://localhost:8182/?code=x&state={s}", "connections.schwab_url"),
    (lambda s: f"https://127.0.0.1:8183/?code=x&state={s}", "connections.schwab_url"),
    (lambda s: f"https://127.0.0.1/?code=x&state={s}", "connections.schwab_url"),
    (lambda s: f"https://127.0.0.1:99999/?code=x&state={s}", "connections.schwab_url"),
    (lambda s: f"https://evil.example:8182/?code=x&state={s}", "connections.schwab_url"),
    (lambda s: f"https://127.0.0.1:8182/?state={s}", "connections.schwab_url"),
    (lambda s: f"https://127.0.0.1:8182/?code=&state={s}", "connections.schwab_url"),
    (lambda s: f"https://127.0.0.1:8182/?code=a&code=b&state={s}", "connections.schwab_url"),
    (lambda s: f"?code=x&state={s}", "connections.schwab_url"),
    (lambda s: "", "connections.schwab_url"),
    (lambda s: "https://127.0.0.1:8182/?code=x", "connections.schwab_state"),
    (lambda s: "https://127.0.0.1:8182/?code=x&state=other", "connections.schwab_state"),
    (lambda s: f"https://127.0.0.1:8182/?code=x&state={s}&state=other", "connections.schwab_state"),
])
def test_finish_refuses_a_bad_address_before_any_call(env, make, code):
    state = state_of(auth.start_login()["auth_url"])
    with pytest.raises(SchwabError) as error:
        auth.finish_login(make(state))
    assert error.value.code == code
    assert not token_posts(env)


def test_finish_without_a_live_start_or_after_ten_minutes(env):
    with pytest.raises(SchwabError) as error:
        auth.finish_login(callback("anything"))
    assert error.value.code == "connections.no_login"
    state = state_of(auth.start_login()["auth_url"])
    env.clock.at += 599
    auth.finish_login(callback(state))                                   # still alive at 9 min 59 s
    state = state_of(auth.start_login()["auth_url"])
    env.clock.at += 600
    with pytest.raises(SchwabError) as error:
        auth.finish_login(callback(state))
    assert error.value.code == "connections.no_login"
    assert len(token_posts(env)) == 1


def test_a_code_works_once_and_a_start_works_once(env):
    state = state_of(auth.start_login()["auth_url"])
    auth.finish_login(callback(state))
    with pytest.raises(SchwabError) as error:
        auth.finish_login(callback(state))
    assert error.value.code == "connections.no_login"


# -------------------------------------------------------------------------------------------- the exchange

def test_the_exchange_is_a_form_post_with_the_basic_header(env):
    sign_in(env)
    call = token_posts(env)[0]
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert call.headers["authorization"] == "Basic " + basic
    assert call.headers["content-type"] == "application/x-www-form-urlencoded"
    assert dict(urllib.parse.parse_qsl(call.body.decode())) == {
        "grant_type": "authorization_code", "code": CODE, "redirect_uri": REDIRECT}
    assert call.query == {}


def test_sign_in_saves_the_session_runs_the_test_and_marks_the_provider_added(env):
    result = sign_in(env)
    test = result["test"]
    assert test["ok"] is True and test["state"] == "connected" and test["symbol"] == "SPY" and test["first_data"] is True
    assert test["data_state"] == "live" and test["error_code"] is None and isinstance(test["latency_ms"], int)
    assert [c.path for c in env.fake.calls if c.method == "GET"] == [QUOTES, NUMBERS]
    assert env.fake.calls[1].query == {"symbols": "SPY", "fields": "quote"}
    assert env.fake.calls[1].headers["authorization"] == "Bearer AT-SECRET-1"
    assert env.added == ["schwab"] and env.accounts_state["refreshed"] == 1
    card = auth.session_card()
    assert card == {"state": "signed_in", "session_expires_at": T0 + 7 * DAY,
                    "accounts": [{"account_ref": "A1", "mask": "••••5678"}]}
    assert auth.is_connected() is True


def test_a_delayed_quote_is_reported_as_delayed(env):
    env.fake.on("GET", QUOTES, {"SPY": {"realtime": False, "quote": {"lastPrice": 1.5}}})
    assert sign_in(env)["test"]["data_state"] == "delayed"


def test_a_quote_without_last_price_fails_the_test_but_keeps_the_session_and_does_not_add_it(env):
    env.fake.on("GET", QUOTES, {"SPY": {"quote": {}}})
    test = sign_in(env)["test"]
    assert test["ok"] is False and test["error_code"] == "schwab.err.upstream" and test["state"] == "error"
    assert env.added == [] and auth.is_connected()


def test_a_denied_quote_reports_the_denied_code(env):
    env.fake.on("GET", QUOTES, (403, {"errors": [{"status": "403"}]}))
    test = sign_in(env)["test"]
    assert test["ok"] is False and test["error_code"] == "connections.schwab_denied" and test["message"]


def test_the_session_lives_only_in_the_store_with_exactly_its_fields(env):
    sign_in(env)
    assert stored(env) == {"access_token": "AT-SECRET-1", "refresh_token": "RT-SECRET-1", "expires_in": 1800,
                           "obtained_at": T0, "signed_in_at": T0, "session_expires_at": T0 + 7 * DAY,
                           "notice_sent_for": None}
    assert "ID-TOKEN-SECRET" not in json.dumps(list(env.ring.items.values()))
    text = env.settings_path.read_text() if env.settings_path.exists() else ""
    assert "SECRET" not in text and "schwab.session" not in text
    assert not list(env.settings_path.parent.glob("*token*"))


def test_a_refused_code_says_so_and_ends_that_start(env):
    env.issuer.status = 400
    state = state_of(auth.start_login()["auth_url"])
    with pytest.raises(SchwabError) as error:
        auth.finish_login(callback(state))
    assert error.value.code == "connections.schwab_code" and error.value.status == 400
    assert ("hokma-market", "schwab.session") not in env.ring.items and env.added == []
    with pytest.raises(SchwabError) as again:
        auth.finish_login(callback(state))
    assert again.value.code == "connections.no_login"


def test_no_network_keeps_the_start_so_the_student_can_try_again(env):
    env.issuer.status = 500
    state = state_of(auth.start_login()["auth_url"])
    with pytest.raises(SchwabError) as error:
        auth.finish_login(callback(state))
    assert error.value.code == "connections.schwab_code"                  # a 5xx on the token endpoint is not a session
    state = state_of(auth.start_login()["auth_url"])
    env.issuer.status = 200
    auth.finish_login(callback(state))
    assert auth.is_connected()


def test_a_new_sign_in_does_not_remove_the_session_until_its_exchange_works(env):
    sign_in(env)
    url = auth.start_login()["auth_url"]
    assert auth.is_connected() and auth.access_token() == "AT-SECRET-1"
    env.issuer.status = 400
    with pytest.raises(SchwabError):
        auth.finish_login(callback(state_of(url)))
    assert auth.session_card()["state"] == "signed_in" and stored(env)["access_token"] == "AT-SECRET-1"
    env.issuer.status = 200
    auth.finish_login(callback(state_of(auth.start_login()["auth_url"])))
    assert stored(env)["access_token"] == "AT-SECRET-2" and stored(env)["signed_in_at"] == T0


def test_an_answer_that_would_not_fit_the_store_fails_without_values(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.issuer.body = {"access_token": "AT-SECRET-" + "x" * 3000, "refresh_token": "RT-SECRET-1", "expires_in": 1800}
    state = state_of(auth.start_login()["auth_url"])
    with pytest.raises(SchwabError) as error:
        auth.finish_login(callback(state))
    assert error.value.code == "connections.store_unavailable" and error.value.status == 503
    assert "AT-SECRET" not in caplog.text and "RT-SECRET" not in caplog.text
    assert ("hokma-market", "schwab.session") not in env.ring.items


def test_without_a_credential_store_the_sign_in_says_store_unavailable(env, monkeypatch):
    sign_in(env)
    monkeypatch.setattr(env.service.store, "set", lambda *a: (_ for _ in ()).throw(auth.StoreUnavailable()))
    env.clock.at += 26 * 60
    with pytest.raises(SchwabError) as error:
        auth.access_token()
    assert error.value.code == "connections.store_unavailable"


def test_open_in_browser_accepts_only_the_address_of_the_live_start(env, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: opened.append(url) or True)
    with pytest.raises(SchwabError) as error:
        auth.open_login_in_browser("https://api.schwabapi.com/v1/oauth/authorize?x=1")
    assert error.value.code == "connections.no_login"
    url = auth.start_login()["auth_url"]
    for other in (url + "&x=1", url.replace("client_id=" + CLIENT_ID, "client_id=other"), "https://evil.example/"):
        with pytest.raises(SchwabError):
            auth.open_login_in_browser(other)
    assert auth.open_login_in_browser(url) is True and opened == [url]
    env.clock.at += 601
    with pytest.raises(SchwabError):
        auth.open_login_in_browser(url)


# -------------------------------------------------------------------------------------------- renewal

def test_the_token_is_renewed_five_minutes_before_it_ends(env):
    sign_in(env)
    env.clock.at = T0 + 1499                                             # 24 min 59 s: 1800 - 300 = 1500
    assert auth.access_token() == "AT-SECRET-1" and len(token_posts(env)) == 1
    env.clock.at = T0 + 1500
    assert auth.access_token() == "AT-SECRET-2" and len(token_posts(env)) == 2
    call = token_posts(env)[1]
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert call.headers["authorization"] == "Basic " + basic
    assert dict(urllib.parse.parse_qsl(call.body.decode())) == {"grant_type": "refresh_token",
                                                                "refresh_token": "RT-SECRET-1"}
    saved = stored(env)
    assert saved["refresh_token"] == "RT-SECRET-2" and saved["obtained_at"] == T0 + 1500
    assert saved["session_expires_at"] == T0 + 7 * DAY and saved["signed_in_at"] == T0
    assert auth.access_token() == "AT-SECRET-2" and len(token_posts(env)) == 2


def test_the_old_refresh_token_is_kept_when_none_comes_back(env):
    sign_in(env)
    env.issuer.refresh_in_answer = False
    env.clock.at = T0 + 1600
    assert auth.access_token() == "AT-SECRET-2"
    assert stored(env)["refresh_token"] == "RT-SECRET-1"
    env.clock.at = T0 + 3200
    auth.access_token()
    assert dict(urllib.parse.parse_qsl(token_posts(env)[2].body.decode()))["refresh_token"] == "RT-SECRET-1"


def test_the_expiry_is_obtained_at_plus_the_expires_in_that_came(env):
    env.issuer.body = {"access_token": "AT-SECRET-9", "refresh_token": "RT-SECRET-9", "expires_in": 600}
    sign_in(env)
    env.clock.at = T0 + 299
    assert auth.access_token() == "AT-SECRET-9" and len(token_posts(env)) == 1
    env.issuer.body = None
    env.clock.at = T0 + 300                                              # 600 - 300: never assumes 30 minutes
    assert auth.access_token() == "AT-SECRET-2" and len(token_posts(env)) == 2


def test_a_401_renews_and_retries_once(env):
    sign_in(env)
    seen: list[str] = []

    def quotes(call):
        seen.append(call.headers["authorization"])
        return (200, {"ok": True}) if call.headers["authorization"] == "Bearer " + (env.issuer.valid or "") \
            and env.issuer.valid != "AT-SECRET-1" else (401, {"errors": [{"status": "401"}]})

    env.fake.on("GET", QUOTES, quotes)
    assert http.get(QUOTES, {"symbols": "SPY"}) == {"ok": True}
    assert seen == ["Bearer AT-SECRET-1", "Bearer AT-SECRET-2"] and len(token_posts(env)) == 2
    env.fake.on("GET", QUOTES, (401, {"errors": [{"status": "401"}]}))
    before = len(token_posts(env))
    with pytest.raises(SchwabError) as error:
        http.get(QUOTES, {"symbols": "SPY"})
    assert error.value.code == "connections.schwab_expired"
    assert len(token_posts(env)) == before + 1                           # one renewal, one retry, no more


def test_two_renewals_at_once_make_one_call(env):
    sign_in(env)
    env.issuer.delay = 0.3
    env.clock.at = T0 + 1600
    results: list[str] = []
    gate = threading.Barrier(6)

    def work():
        gate.wait()
        results.append(auth.access_token())

    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == ["AT-SECRET-2"] * 6 and len(token_posts(env)) == 2


def test_two_forced_renewals_at_once_make_one_call(env):
    sign_in(env)
    env.issuer.delay = 0.3
    results: list[str] = []
    gate = threading.Barrier(4)

    def work():
        gate.wait()
        results.append(auth.access_token(force_refresh=True))

    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == ["AT-SECRET-2"] * 4 and len(token_posts(env)) == 2


@pytest.mark.parametrize("status, body", [(400, {"error": "invalid_grant"}), (401, {"error": "invalid_client"}),
                                          (400, {"error": "unsupported_grant_type"})])
def test_a_refused_renewal_ends_the_session_and_keeps_client_id_and_secret(env, status, body):
    sign_in(env)
    env.issuer.status, env.issuer.body = status, body
    env.clock.at = T0 + 1600
    with pytest.raises(SchwabError) as error:
        auth.access_token()
    assert error.value.code == "connections.schwab_expired" and error.value.status == 401
    assert ("hokma-market", "schwab.session") not in env.ring.items
    assert json.loads(env.ring.items[("hokma-market", "schwab")]) == {"client_id": CLIENT_ID,
                                                                    "client_secret": CLIENT_SECRET}
    assert auth.session_card() == {"state": "expired", "session_expires_at": None, "accounts": []}
    assert auth.is_connected() is False
    with pytest.raises(SchwabError) as again:
        auth.access_token()
    assert again.value.code == "connections.schwab_expired" and len(token_posts(env)) == 2   # no new call


def test_without_a_session_the_token_says_not_connected(env):
    assert auth.is_connected() is False and auth.session_card()["state"] == "signed_out"
    with pytest.raises(SchwabError) as error:
        auth.access_token()
    assert error.value.code == "schwab.err.not_connected"
    assert error.value.status == 409


def test_a_network_failure_on_renewal_keeps_the_session_and_the_still_valid_token(env):
    sign_in(env)
    env.issuer.status = 500
    env.clock.at = T0 + 1600                                             # inside the margin, token still valid
    assert auth.access_token() == "AT-SECRET-1"
    assert auth.session_card()["state"] == "signed_in"
    env.clock.at = T0 + 1801                                             # now the token itself ended
    with pytest.raises(SchwabError) as error:
        auth.access_token()
    assert error.value.code == "schwab.err.upstream" and auth.session_card()["state"] == "signed_in"


def test_sign_out_removes_the_session_only(env):
    sign_in(env)
    auth.sign_out()
    assert ("hokma-market", "schwab.session") not in env.ring.items and ("hokma-market", "schwab") in env.ring.items
    assert auth.session_card()["state"] == "signed_out" and not auth.is_connected()


# -------------------------------------------------------------------------------------------- the seven days

def test_expiring_starts_24_hours_before_the_end_and_expired_at_the_end(env):
    sign_in(env)
    end = T0 + 7 * DAY
    for at, expected in ((T0 + 6 * DAY - 1, "signed_in"), (T0 + 6 * DAY, "expiring"), (end - 1, "expiring"),
                         (end, "expired")):
        env.clock.at = at
        card = auth.session_card()
        assert card["state"] == expected, at
    assert card["session_expires_at"] is None and card["accounts"] == []
    env.clock.at = T0 + 6 * DAY
    assert auth.session_card()["session_expires_at"] == end and auth.is_connected()


def test_after_seven_days_the_session_is_removed_without_a_call(env):
    sign_in(env)
    env.clock.at = T0 + 7 * DAY
    before = len(token_posts(env))
    with pytest.raises(SchwabError) as error:
        auth.access_token()
    assert error.value.code == "connections.schwab_expired" and len(token_posts(env)) == before
    assert ("hokma-market", "schwab.session") not in env.ring.items


def test_the_notice_goes_once_per_session_and_again_after_a_new_sign_in(env):
    sign_in(env)
    env.clock.at = T0 + 6 * DAY - 10
    auth.maintain()
    assert env.notices == []
    env.clock.at = T0 + 6 * DAY + 60
    auth.maintain()
    assert len(env.notices) == 1 and "Schwab" in env.notices[0] and "{when}" not in env.notices[0]
    assert stored(env)["notice_sent_for"] == T0
    env.clock.at += 3600
    auth.maintain()
    auth.maintain()
    assert len(env.notices) == 1
    signed_at = env.clock.at
    sign_in(env)
    assert stored(env)["signed_in_at"] == signed_at and stored(env)["notice_sent_for"] is None
    auth.maintain()
    assert len(env.notices) == 1
    env.clock.at = signed_at + 6 * DAY + 5
    auth.maintain()
    assert len(env.notices) == 2


def test_a_notice_that_cannot_go_out_is_tried_again_next_time(env):
    sign_in(env)
    env.clock.at = T0 + 6 * DAY + 60
    calls: list[str] = []

    def failing(text):
        calls.append(text)
        raise OSError("down")

    auth.set_notifier(failing)
    auth.maintain()
    assert len(calls) == 1 and stored(env)["notice_sent_for"] is None
    auth.set_notifier(env.notices.append)
    auth.maintain()
    assert len(env.notices) == 1 and stored(env)["notice_sent_for"] == T0


def test_maintain_renews_the_token_when_it_is_about_to_end_and_ends_a_refused_session(env):
    sign_in(env)
    auth.maintain()
    assert len(token_posts(env)) == 1
    env.clock.at = T0 + 1600
    auth.maintain()
    assert len(token_posts(env)) == 2 and stored(env)["access_token"] == "AT-SECRET-2"
    env.issuer.status = 400
    env.clock.at = T0 + 3300
    auth.maintain()                                                      # never raises
    assert ("hokma-market", "schwab.session") not in env.ring.items and auth.session_card()["state"] == "expired"
    auth.maintain()


def test_the_default_notice_goes_to_the_owner_s_telegram_outbox(env, monkeypatch):
    queued: list[str] = []

    class Unit:
        class session:
            @staticmethod
            def one(sql):
                return {"x": 1}

    class Db:
        def write(self):
            import contextlib
            return contextlib.nullcontext(Unit())

    monkeypatch.setattr("hokma.db.get_database", lambda: Db())
    monkeypatch.setattr("hokma.telegram.glue.enqueue", lambda unit, text: queued.append(text))
    auth.set_notifier(None)
    sign_in(env)
    env.clock.at = T0 + 6 * DAY + 60
    auth.maintain()
    assert len(queued) == 1 and "Schwab" in queued[0] and "{when}" not in queued[0] and stored(env)["notice_sent_for"] == T0


# -------------------------------------------------------------------------------------------- nothing leaks

def test_nothing_secret_is_logged_or_returned(env, caplog):
    caplog.set_level(logging.DEBUG)
    secrets_ = [CLIENT_SECRET, CODE, "C0.test-code-abcdef%40", "AT-SECRET", "RT-SECRET", "ID-TOKEN-SECRET",
                FAKE_ACCOUNT_NUMBER, FAKE_HASH]
    outputs = [sign_in(env), auth.session_card()]
    env.clock.at = T0 + 1600
    outputs.append(auth.access_token() and "renewed")
    env.issuer.status = 400
    env.clock.at = T0 + 3300
    with pytest.raises(SchwabError) as error:
        auth.access_token()
    outputs += [str(error.value), repr(error.value.__dict__)]
    env.issuer.status = 200
    with pytest.raises(SchwabError) as bad:
        auth.finish_login(callback("x", code=CODE))
    outputs.append(str(bad.value))
    everything = json.dumps(outputs, default=str) + caplog.text
    for secret in secrets_:
        assert secret not in everything, secret
    for secret in secrets_:
        assert secret not in json.dumps(auth.session_card())
