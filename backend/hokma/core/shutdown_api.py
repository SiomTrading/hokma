"""POST /api/system/shutdown: the "Shut down Hokma" button of the panel.

The answer is `202 {"stopping": true}` with `Cache-Control: no-store`; the engine ends AFTER the answer was sent
(`Lifecycle.shutdown`, the same clean stop as `hokma stop`). Two forms, with different rules:

- With a session: `X-Hokma-Token` is valid. It also needs `Sec-Fetch-Site: same-origin`; Host and Origin are checked by
  the app-wide `LocalOnlyMiddleware`. It does not need `X-Hokma-Owner`: a tab that lost the owner confirmation must be
  able to shut down to come back in with the Hokma icon. Shutting down changes nothing of the owner's data.
- Without a session (the entry screen of a browser that has none): no token, but `Origin` must be present and be the
  engine's own, the call must carry a nonce (`X-Hokma-Shutdown-Nonce`) that the engine handed to this page with
  `GET /api/system/shutdown-nonce` (same origin only; single use, 60 seconds, bound to the client address and
  User-Agent that asked for it), nobody may have Hokma open (zero WebSockets), no turn may be running, and at most one
  such call is accepted every 30 seconds.

Errors are `{"detail": {"code", "message"}}`.
"""
import hashlib
import secrets
import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from hokma.core.origin import origins_for, token_matches
from hokma.db import Actor, DbError, current_version, get_database
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN

router = APIRouter()
NO_SESSION_MIN_GAP_SECONDS = 30.0
NONCE_TTL_SECONDS = 60.0
MAX_NONCES = 8
NONCE_HEADER = "X-Hokma-Shutdown-Nonce"
now = time.monotonic                           # tests replace it to age a nonce
HUMAN = Actor.human("owner")


def _fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _binding(request: Request) -> str:
    """What a nonce is bound to: the client address and the User-Agent of the page that asked for it."""
    host = request.client.host if request.client else ""
    return hashlib.sha256(f"{host}\0{request.headers.get('user-agent', '')}".encode()).hexdigest()


def _issue_nonce(request: Request) -> str:
    nonces: dict = getattr(request.app.state, "shutdown_nonces", None)
    if nonces is None:
        nonces = request.app.state.shutdown_nonces = {}
    stamp = now()
    for old in [n for n, (until, _) in nonces.items() if until <= stamp]:
        del nonces[old]
    while len(nonces) >= MAX_NONCES:
        del nonces[next(iter(nonces))]
    nonce = secrets.token_urlsafe(24)
    nonces[nonce] = (stamp + NONCE_TTL_SECONDS, _binding(request))
    return nonce


def _spend_nonce(request: Request, candidate: str) -> bool:
    """True once for a nonce that exists, has not expired and was issued to this client. It is gone either way."""
    nonces: dict = getattr(request.app.state, "shutdown_nonces", {})
    entry = nonces.pop(candidate, None) if candidate else None
    if entry is None:
        return False
    until, bound = entry
    return until > now() and secrets.compare_digest(bound, _binding(request))


def turns_running() -> int:
    """Chat turns that are running right now (an engine nobody used yet has none)."""
    from hokma.engine import api as engine_api

    engine = engine_api._engine
    return sum(1 for s in engine.sessions.values() if s.state == "running") if engine is not None else 0


def _journal(title: str) -> None:
    try:
        if current_version() == 0:
            return
        with get_database().write() as u:
            u.journal.add("action", title[:200], actor=HUMAN, outcome="success")
    except (DbError, OSError):
        pass                                    # a record, never a reason not to shut down


async def _shut_down(request: Request, journal_title: str) -> None:
    _journal(journal_title)
    await request.app.state.lifecycle.shutdown()


@router.get("/api/system/shutdown-nonce")
async def shutdown_nonce(request: Request) -> JSONResponse:
    """The nonce the entry screen needs for the call without a session. Only a fetch of the page itself gets one."""
    if request.headers.get("sec-fetch-site", "").lower() != "same-origin":
        raise _fail(403, "shutdown.origin", t("shutdown.err.origin"))
    return JSONResponse({"nonce": _issue_nonce(request)}, headers={"Cache-Control": "no-store"})


def _guard(request: Request, token: str, nonce: str, gap_attr: str) -> bool:
    """The checks of the shutdown for the browser call; True when it came with a session."""
    state, headers = request.app.state, request.headers
    if headers.get("sec-fetch-site", "").lower() != "same-origin":
        raise _fail(403, "shutdown.origin", t("shutdown.err.origin"))
    if token:
        if not token_matches(token, SESSION_TOKEN):
            raise _fail(401, "shutdown.unauthorized", t("shutdown.err.unauthorized"))
        return True
    origin = headers.get("origin", "").lower()
    if not origin or origin not in origins_for(state.allowed_hosts):
        raise _fail(403, "shutdown.origin", t("shutdown.err.origin"))
    if not _spend_nonce(request, nonce):
        raise _fail(403, "shutdown.nonce", t("shutdown.err.nonce"))
    stamp, last = now(), getattr(state, gap_attr, None)
    if last is not None and stamp - last < NO_SESSION_MIN_GAP_SECONDS:
        raise _fail(429, "shutdown.too_soon", t("shutdown.err.too_soon"))
    if state.lifecycle.open_sockets > 0 or turns_running() > 0:
        raise _fail(409, "shutdown.in_use", t("shutdown.err.in_use"))
    setattr(state, gap_attr, stamp)
    return False


async def _ask_supervisor_for_browser() -> None:
    import asyncio

    from hokma.core import control

    await asyncio.to_thread(control.request, "browser")


@router.post("/api/system/browser", status_code=202)
async def open_in_browser(request: Request, x_hokma_token: str = Header(default=""),
                          x_hokma_shutdown_nonce: str = Header(default="")) -> JSONResponse:
    """"Use Hokma here in the browser": the supervisor opens the default browser with a fresh session (same rules
    as the shutdown: with a session, or from the entry screen with a nonce and nobody inside)."""
    _guard(request, x_hokma_token, x_hokma_shutdown_nonce, "browser_at")
    if turns_running() > 0:                      # the engine restarts: not while a turn is running, with a session either
        raise _fail(409, "shutdown.in_use", t("shutdown.err.in_use"))
    return JSONResponse({"opening": True}, status_code=202, headers={"Cache-Control": "no-store"},
                        background=BackgroundTask(_ask_supervisor_for_browser))


@router.get("/api/system/browser-pref")
async def browser_pref_get(request: Request, x_hokma_token: str = Header(default="")) -> JSONResponse:
    from hokma.core import config

    _guard_session(x_hokma_token)
    return JSONResponse({"always": config.browser_preferred()}, headers={"Cache-Control": "no-store"})


@router.put("/api/system/browser-pref")
async def browser_pref_put(request: Request, x_hokma_token: str = Header(default="")) -> JSONResponse:
    from hokma.core import config

    _guard_session(x_hokma_token)
    body = await request.json()
    always = body.get("always") if isinstance(body, dict) else None
    if not isinstance(always, bool):
        raise _fail(422, "browser.bad_value", t("shutdown.err.origin"))
    path = config.browser_pref_path()
    if always:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1", encoding="ascii")
    else:
        path.unlink(missing_ok=True)
    return JSONResponse({"always": always}, headers={"Cache-Control": "no-store"})


def _guard_session(token: str) -> None:
    if not token_matches(token, SESSION_TOKEN):
        raise _fail(401, "shutdown.unauthorized", t("shutdown.err.unauthorized"))


@router.post("/api/system/shutdown", status_code=202)
async def shutdown(request: Request, x_hokma_token: str = Header(default=""),
                   x_hokma_shutdown_nonce: str = Header(default="")) -> JSONResponse:
    state = request.app.state
    headers = request.headers
    if headers.get("sec-fetch-site", "").lower() != "same-origin":
        raise _fail(403, "shutdown.origin", t("shutdown.err.origin"))
    with_session = bool(x_hokma_token)
    if with_session:
        if not token_matches(x_hokma_token, SESSION_TOKEN):
            raise _fail(401, "shutdown.unauthorized", t("shutdown.err.unauthorized"))
    else:
        origin = headers.get("origin", "").lower()
        if not origin or origin not in origins_for(state.allowed_hosts):
            raise _fail(403, "shutdown.origin", t("shutdown.err.origin"))
        if not _spend_nonce(request, x_hokma_shutdown_nonce):
            raise _fail(403, "shutdown.nonce", t("shutdown.err.nonce"))
        stamp = now()
        last = getattr(state, "shutdown_at", None)
        if last is not None and stamp - last < NO_SESSION_MIN_GAP_SECONDS:
            raise _fail(429, "shutdown.too_soon", t("shutdown.err.too_soon"))
        if state.lifecycle.open_sockets > 0 or turns_running() > 0:
            raise _fail(409, "shutdown.in_use", t("shutdown.err.in_use"))
        state.shutdown_at = stamp                 # no await between the checks and this line: one accepted call at a time
    title = t("shutdown.journal.panel") if with_session else t("shutdown.journal.entry")
    return JSONResponse({"stopping": True}, status_code=202, headers={"Cache-Control": "no-store"},
                        background=BackgroundTask(_shut_down, request, title))
