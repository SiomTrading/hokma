"""Sign-in with Schwab and the session that keeps it alive (SPEC-015 5.2 and 5.3).

Only the three-legged `authorization_code` flow exists. Client ID and Secret are saved by `ConnectionService.put_key` (JSON in
`hokma-market/schwab`); the session lives in its own credential-store entry `schwab.session`, as JSON with
`access_token, refresh_token, expires_in, obtained_at, signed_in_at, session_expires_at, notice_sent_for` (UTC seconds).
Nothing else of Schwab's answer is kept (no `id_token`). Nothing here is ever logged or returned: tokens, Secret, codes.

`http.request` is the only door to the network; this module only builds the authorization address (the user opens it) and
calls `http.request("POST", "/v1/oauth/token", authed=False)` with the Basic header.

Blocking: call it from a thread. `set_clock`, `set_notifier` and `reset` exist for tests only.
"""
import base64
import hmac
import json
import logging
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime

from hokma.connections.store import StoreUnavailable
from hokma.schwab import http
from hokma.schwab.http import SchwabError

log = logging.getLogger("hokma.schwab")

PROVIDER = "schwab"
SESSION_ENTRY = "schwab.session"
AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_PATH = "/v1/oauth/token"
REDIRECT_URI = "https://127.0.0.1:8182"
REDIRECT_HOST, REDIRECT_PORT = "127.0.0.1", 8182
LOGIN_SECONDS = 600                                   # a started sign-in lives 10 minutes
RENEW_MARGIN = 300                                    # the access token is renewed 5 minutes before it ends
SESSION_SECONDS = 7 * 24 * 3600                       # REF E.1 (not official; SPEC-015 12, point 5)
NOTICE_SECONDS = 24 * 3600                            # "expiring" starts 24 h before session_expires_at
STORE_LIMIT_BYTES = 2560                              # what Windows keeps per credential
TEST_SYMBOL = "SPY"

# Catalog codes (messages.py maps them for the panel).
NO_KEY = "connections.no_key"
NO_LOGIN = "connections.no_login"
BAD_URL = "connections.schwab_url"
BAD_STATE = "connections.schwab_state"
BAD_CODE = "connections.schwab_code"
STORE_UNAVAILABLE = "connections.store_unavailable"

_FIELDS = ("access_token", "refresh_token", "expires_in", "obtained_at", "signed_in_at", "session_expires_at",
           "notice_sent_for")

_clock: Callable[[], float] = time.time
_notifier: Callable[[str], None] | None = None

_lock = threading.RLock()                             # renewals, session writes and the cache
_login_lock = threading.Lock()                        # one `finish_login` at a time
_cache_store: object | None = None
_cache: dict | None = None                            # the session in memory; None = signed out
_expired = False                                      # the last renewal was refused: the card says "expired"
_pending: dict | None = None                          # {"state", "url", "at"} of the live sign-in


def set_clock(clock: Callable[[], float] | None) -> None:
    """Tests only: a clock returning UTC seconds."""
    global _clock
    _clock = clock or time.time


def set_notifier(notifier: Callable[[str], None] | None) -> None:
    """Tests only: where the "session is ending" notice goes (default: the owner's Telegram outbox / mail)."""
    global _notifier
    _notifier = notifier


def reset() -> None:
    """Tests only: forget everything held in memory (the store is not touched)."""
    global _cache_store, _cache, _expired, _pending
    with _lock:
        _cache_store, _cache, _expired, _pending = None, None, False, None


def now() -> int:
    return int(_clock())


# ---------------------------------------------------------------------------------------------- the stored session

def _store():
    from hokma.connections import runtime

    return runtime.get_service().store


def _load() -> dict | None:
    """The session (cached per store). Blocking the first time: asks the system store."""
    global _cache_store, _cache
    store = _store()
    held, mine = _cache_store, _cache                     # read without the lock: a renewal in progress must not block it
    if held is store:
        return mine
    with _lock:
        if _cache_store is not store:
            _cache_store, _cache = store, _parse(store.get(SESSION_ENTRY))
        return _cache


def _parse(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) \
            or not isinstance(data.get("refresh_token"), str) or not isinstance(data.get("obtained_at"), (int, float)) \
            or not isinstance(data.get("expires_in"), (int, float)) \
            or not isinstance(data.get("session_expires_at"), (int, float)):
        return None
    return {k: data.get(k) for k in _FIELDS}


def _save(session: dict) -> None:
    global _cache_store, _cache
    store = _store()
    raw = json.dumps({k: session.get(k) for k in _FIELDS}, separators=(",", ":"))
    if len(raw.encode("utf-8")) > STORE_LIMIT_BYTES:
        log.error("schwab session not saved: it is larger than the credential store allows (%d bytes)", STORE_LIMIT_BYTES)
        raise SchwabError(STORE_UNAVAILABLE, 503)
    try:
        store.set(SESSION_ENTRY, raw)
    except StoreUnavailable:
        log.error("schwab session not saved: the credential store refused it")
        raise SchwabError(STORE_UNAVAILABLE, 503) from None
    with _lock:
        _cache_store, _cache = store, dict(session)


def _drop() -> None:
    """Forget the session (store and memory). Client ID and Secret stay."""
    global _cache_store, _cache
    store = _store()
    try:
        store.delete(SESSION_ENTRY)
    except StoreUnavailable:
        log.error("schwab session could not be removed from the credential store")
    with _lock:
        _cache_store, _cache = store, None


def _credentials() -> tuple[str, str]:
    """(client_id, client_secret) as `put_key` saved them. SchwabError(NO_KEY) when they are not there."""
    raw = _store().get(PROVIDER)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    client_id = data.get("client_id") if isinstance(data, dict) else None
    secret = data.get("client_secret") if isinstance(data, dict) else None
    if not isinstance(client_id, str) or not isinstance(secret, str) or not client_id or not secret:
        raise SchwabError(NO_KEY, 409)
    return client_id, secret


def _basic(client_id: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()


# ---------------------------------------------------------------------------------------------- state

def _state(session: dict | None) -> str:
    if session is None:
        return "expired" if _expired else "signed_out"
    at = _clock()
    end = session["session_expires_at"]
    if at >= end:
        return "expired"
    return "expiring" if at >= end - NOTICE_SECONDS else "signed_in"


def is_connected() -> bool:
    """Cheap: a session is held and its 7 days have not run out."""
    session = _load()
    return session is not None and _clock() < session["session_expires_at"]


def session_card() -> dict:
    """`{state, session_expires_at, accounts}` of the Schwab card. Never a token, a number or a hash."""
    session = _load()
    state = _state(session)
    ends = int(session["session_expires_at"]) if session is not None and state != "expired" else None
    accounts: list[dict] = []
    if session is not None and state in ("signed_in", "expiring"):
        try:
            from hokma.schwab import accounts as accounts_module

            accounts = list(accounts_module.listing())
        except Exception:                                     # noqa: BLE001  (the card must always be built)
            accounts = []
    return {"state": state, "session_expires_at": ends, "accounts": accounts}


# ---------------------------------------------------------------------------------------------- token calls

def _token_call(form: dict[str, str]) -> dict:
    """POST to the token endpoint with the Basic header. Returns the decoded answer; raises SchwabError."""
    client_id, secret = _credentials()
    answer = http.request("POST", TOKEN_PATH, None, body=urllib.parse.urlencode(form).encode(),
                          headers={"Authorization": _basic(client_id, secret),
                                   "Content-Type": "application/x-www-form-urlencoded"}, authed=False)
    if not isinstance(answer, dict):
        raise SchwabError(http.UPSTREAM, 502)
    return answer


def _tokens_of(answer: dict) -> tuple[str, str | None, int]:
    access, refresh, seconds = answer.get("access_token"), answer.get("refresh_token"), answer.get("expires_in")
    if not isinstance(access, str) or not access or isinstance(seconds, bool) \
            or not isinstance(seconds, (int, float)) or seconds <= 0:
        raise SchwabError(http.UPSTREAM, 502)
    return access, (refresh if isinstance(refresh, str) and refresh else None), int(seconds)


def _refused(error: SchwabError) -> bool:
    """The token endpoint said no (400 `invalid_grant`, 401, 403): the session cannot be renewed."""
    return error.code in (http.BAD_REQUEST, http.EXPIRED, http.DENIED)


def _renew(session: dict) -> dict:
    """One renewal (caller holds the lock). Keeps the refresh_token when none comes back; `session_expires_at` does not move.
    A refusal ends the session."""
    global _expired
    try:
        answer = _token_call({"grant_type": "refresh_token", "refresh_token": session["refresh_token"]})
        access, refresh, seconds = _tokens_of(answer)
    except SchwabError as error:
        if _refused(error):
            log.warning("schwab session ended: the renewal was refused (HTTP %s)", error.status)
            _drop()
            _expired = True
            raise SchwabError(http.EXPIRED, 401, correl_id=error.correl_id) from None
        raise
    renewed = {**session, "access_token": access, "refresh_token": refresh or session["refresh_token"],
               "expires_in": seconds, "obtained_at": now()}
    _save(renewed)
    log.info("schwab access token renewed")
    return renewed


def _ends_at(session: dict) -> float:
    return session["obtained_at"] + session["expires_in"]


def access_token(force_refresh: bool = False) -> str:
    """The access token, renewed first when it ends in less than 5 minutes (or when `force_refresh`, after a 401).
    SchwabError NOT_CONNECTED without a session, EXPIRED when the session ended. Two threads asking at once make one call."""
    global _expired
    session = _load()
    if session is None:
        raise SchwabError(http.EXPIRED, 401) if _expired else SchwabError(http.NOT_CONNECTED, 409)
    seen = session["access_token"]
    if not force_refresh and _clock() < _ends_at(session) - RENEW_MARGIN and _clock() < session["session_expires_at"]:
        return session["access_token"]
    with _lock:
        session = _load()
        if session is None:                                   # another thread ended the session meanwhile
            raise SchwabError(http.EXPIRED, 401) if _expired else SchwabError(http.NOT_CONNECTED, 409)
        if _clock() >= session["session_expires_at"]:
            log.warning("schwab session ended: its 7 days ran out")
            _drop()
            _expired = True
            raise SchwabError(http.EXPIRED, 401)
        if session["access_token"] != seen or (not force_refresh and _clock() < _ends_at(session) - RENEW_MARGIN):
            return session["access_token"]                    # another thread renewed it while this one waited
        try:
            return _renew(session)["access_token"]
        except SchwabError as error:
            if error.code in (http.UNREACHABLE, http.RATE_LIMITED, http.UPSTREAM) and _clock() < _ends_at(session):
                return session["access_token"]                # transient: the token we hold still works
            raise


# ---------------------------------------------------------------------------------------------- sign-in

def _live_pending() -> dict | None:
    global _pending
    if _pending is not None and _clock() - _pending["at"] >= LOGIN_SECONDS:
        _pending = None
    return _pending


def start_login() -> dict:
    """`{"auth_url"}` to open in the browser. A new start replaces the one in progress; a session already held stays
    until the exchange of the new one works."""
    global _pending
    client_id, _ = _credentials()
    state = secrets.token_urlsafe(32)                         # 32 random bytes, base64url
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {"client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code", "state": state})
    with _lock:
        _pending = {"state": state, "url": url, "at": _clock()}
    return {"auth_url": url}


def open_login_in_browser(url: str) -> bool:
    """For the desktop window: the system browser opens the address. Only the address of the live start is accepted."""
    with _lock:
        pending = _live_pending()
    if pending is None or not isinstance(url, str) or not hmac.compare_digest(url, pending["url"]):
        raise SchwabError(NO_LOGIN, 409)
    import webbrowser

    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:                                         # noqa: BLE001
        return False


def _code_of(text: str, state: str) -> str:
    """The code of what the student pasted: the full callback address, or just the code."""
    text = (text or "").strip()
    if not text:
        raise SchwabError(BAD_URL, 400)
    if "://" not in text and not text.startswith("?") and "code=" not in text:
        code = urllib.parse.unquote(text) if "%" in text else text     # the code alone (no state to compare)
        if any(c.isspace() for c in code):
            raise SchwabError(BAD_URL, 400)
        return code
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        raise SchwabError(BAD_URL, 400) from None
    if parts.scheme != "https" or parts.hostname != REDIRECT_HOST or port != REDIRECT_PORT:
        raise SchwabError(BAD_URL, 400)
    query = urllib.parse.parse_qs(parts.query)                # decoded once
    codes = query.get("code") or []
    if len(codes) != 1 or not codes[0]:
        raise SchwabError(BAD_URL, 400)
    states = query.get("state") or []
    if len(states) != 1 or not hmac.compare_digest(states[0].encode(), state.encode()):
        raise SchwabError(BAD_STATE, 400)
    return codes[0]


def finish_login(redirect_url: str) -> dict:
    """Exchange the pasted address (or code) for the session, save it, run the test and read the accounts.
    Returns `{"test": {...}}`. Codes: NO_LOGIN, BAD_URL, BAD_STATE, BAD_CODE."""
    global _pending, _expired
    with _login_lock:
        with _lock:
            pending = _live_pending()
        if pending is None:
            raise SchwabError(NO_LOGIN, 409)
        code = _code_of(redirect_url, pending["state"])
        try:
            answer = _token_call({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI})
            access, refresh, seconds = _tokens_of(answer)
        except SchwabError as error:
            if _refused(error) or error.code == http.UPSTREAM:
                with _lock:
                    _pending = None                           # a code works once: the next try starts again
                log.warning("schwab sign-in: the code was not accepted (HTTP %s)", error.status)
                raise SchwabError(BAD_CODE, 400, correl_id=error.correl_id) from None
            raise                                             # no network, rate limit: the start stays for a retry
        if refresh is None:
            with _lock:
                _pending = None
            raise SchwabError(BAD_CODE, 400)
        at = now()
        _save({"access_token": access, "refresh_token": refresh, "expires_in": seconds, "obtained_at": at,
               "signed_in_at": at, "session_expires_at": at + SESSION_SECONDS, "notice_sent_for": None})
        with _lock:
            _pending, _expired = None, False
        log.info("schwab signed in")
    result = run_test()
    try:
        from hokma.schwab import accounts as accounts_module

        accounts_module.refresh()
    except Exception as error:                                # noqa: BLE001  (the sign-in stands without the accounts)
        log.warning("schwab accounts could not be read after signing in: %s", type(error).__name__)
    if result["ok"]:
        from hokma.connections import runtime

        runtime.get_service().mark_added(PROVIDER)
    return {"test": result}


def run_test() -> dict:
    """The connection test, in the shape of `hub.test_provider`: one quote of SPY, ok when `quote.lastPrice` arrives."""
    from hokma.market.messages import connection_message

    started = time.monotonic()
    try:
        answer = http.get(http.MARKETDATA + "/quotes", {"symbols": TEST_SYMBOL, "fields": "quote"})
    except SchwabError as error:
        return _failure(error.code, connection_message(error.code) if error.code.startswith("connections.") else "")
    entry = answer.get(TEST_SYMBOL) if isinstance(answer, dict) else None
    quote = entry.get("quote") if isinstance(entry, dict) else None
    price = quote.get("lastPrice") if isinstance(quote, dict) else None
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return _failure(http.UPSTREAM, "")
    delayed = isinstance(entry, dict) and entry.get("realtime") is False
    return {"ok": True, "state": "connected", "data_state": "delayed" if delayed else "live",
            "latency_ms": max(0, int((time.monotonic() - started) * 1000)), "symbol": TEST_SYMBOL,
            "first_data": True, "error_code": None, "message": ""}


def _failure(code: str, message: str) -> dict:
    if not message:
        from hokma.i18n import t

        try:
            message = t(code)
        except Exception:                                     # noqa: BLE001
            message = ""
    return {"ok": False, "state": "error", "data_state": "delayed", "latency_ms": None, "symbol": TEST_SYMBOL,
            "first_data": False, "error_code": code, "message": message}


def sign_out() -> None:
    """End the session. Client ID and Secret stay."""
    global _pending, _expired
    with _lock:
        _pending, _expired = None, False
        _drop()
    try:
        from hokma.schwab import accounts as accounts_module

        accounts_module.reset()                               # the account table belongs to the session that ended
    except Exception:                                         # noqa: BLE001  (signing out never fails for this)
        pass
    log.info("schwab signed out")


# ---------------------------------------------------------------------------------------------- upkeep

def maintain() -> None:
    """Called often by the engine: renews the token when it is about to end and, once per session, tells the owner that the
    session ends within 24 h (the same channel as `notify_owner`)."""
    session = _load()
    if session is None:
        return
    if _clock() >= session["session_expires_at"] or _clock() >= _ends_at(session) - RENEW_MARGIN:
        try:
            access_token()
        except SchwabError:
            pass                                              # expired, or the network is down: the next round tries again
        session = _load()
        if session is None:
            return
    if _state(session) == "expiring" and session.get("notice_sent_for") != session["signed_in_at"]:
        _send_notice(session)


def _send_notice(session: dict) -> None:
    from hokma.i18n import t

    when = datetime.fromtimestamp(session["session_expires_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
    text = t("notice.schwab.expiring", when=when)
    try:
        (_notifier or _notify_owner)(text)
    except Exception as error:                                # noqa: BLE001  (a failed notice is tried again next round)
        log.warning("schwab expiring notice not sent: %s", type(error).__name__)
        return
    with _lock:
        current = _load()
        if current is not None and current["signed_in_at"] == session["signed_in_at"]:
            _save({**current, "notice_sent_for": session["signed_in_at"]})


def _notify_owner(text: str) -> None:
    """The owner's linked Telegram chat (its outbox, delivered by the sender; the mail fallback is the owner's choice
    in Settings). With no Telegram link the mail notice is tried; with neither there is nobody to tell and the notice counts as given."""
    from hokma.db import get_database
    from hokma.mail import notify as mail_notify
    from hokma.telegram.glue import enqueue

    with get_database().write() as unit:
        if unit.session.one("SELECT 1 AS x FROM telegram_link WHERE id = 1") is not None:
            enqueue(unit, text)
            return
    mail_notify.fallback(text)
