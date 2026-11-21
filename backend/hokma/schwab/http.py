"""The only door to `api.schwabapi.com` (SPEC-015 5.8 and 18.7). Nothing else in the backend writes that host.

Every request is checked BEFORE the network is opened against the list of its consumer in `kit/hardblock_brokers.json`
(exact method, exact path or `path_template`): the default consumer, `hokma_backend`, is held to `read_exceptions` (the reads
and the token call), and the order module (`schwab/orders.py`, the only one that names the other consumer) to
`order_exceptions` (send, replace, cancel and preview). A route that is not in the list of its consumer, a write to an order
route through the default consumer, raises `SchwabError` without a call. The access token comes from `hokma.schwab.auth` (or the test provider); a 401 renews
it once and retries. Logs carry the method, the path with markers, the HTTP status, the milliseconds and Schwab's
`Schwab-Client-CorrelId`; never a token, secret, code, account number, hash or body.

Blocking: call it from a thread (`asyncio.to_thread`). `set_base` and `set_token_provider` exist for tests only.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hokma.core import tls
from hokma.kit.hardblock import exception_match, unsafe_segment

log = logging.getLogger("hokma.schwab")

HOST = "api.schwabapi.com"
BASE = "https://" + HOST
CONSUMER = "hokma_backend"
MARKETDATA = "/marketdata/v1"
TRADER = "/trader/v1"
TIMEOUT_SECONDS = 15
MAX_BODY_BYTES = 16 * 1024 * 1024
CALLS_PER_MINUTE = 100                                  # below the 120 that REF E.1 cites (not official)
RATE_PAUSE_SECONDS = 60.0

# Codes of SchwabError (catalog codes; the API and the tools translate them).
NOT_CONNECTED = "schwab.err.not_connected"
EXPIRED = "connections.schwab_expired"
DENIED = "connections.schwab_denied"
RATE_LIMITED = "connections.rate_limited"
UNREACHABLE = "connections.unreachable"
BAD_REQUEST = "schwab.err.bad_request"
NOT_FOUND = "schwab.err.not_found"
UPSTREAM = "schwab.err.upstream"
INVALID_PARAM = "schwab.err.invalid_param"              # a parameter refused locally, before any call
FORBIDDEN_ROUTE = "schwab.err.forbidden_route"          # not in `read_exceptions`: never leaves the machine


@dataclass(frozen=True)
class Reply:
    """A call answered in full (`request(..., reply=True)`): the decoded body (None when empty), the HTTP status, Schwab's
    `Schwab-Client-CorrelId` and the `Location` header (the new order of a 201, REF 3.2). No token is ever in it."""
    body: Any
    status: int
    correl_id: str | None = None
    location: str | None = None


class SchwabError(Exception):
    """`code` is a catalog code, `status` the HTTP status for the API. `errors` is Schwab's `errors[]` reduced to
    `id`, `status`, `title`, `detail` and `source` (they carry no secret); `correl_id` is `Schwab-Client-CorrelId`."""

    def __init__(self, code: str, status: int = 502, *, correl_id: str | None = None, errors: list | None = None,
                 detail: str | None = None) -> None:
        super().__init__(code)
        self.code, self.status = code, status
        self.correl_id, self.errors, self.detail = correl_id, errors or [], detail


_base_override: str | None = None
_provider: Callable[[], str] | None = None
_refresher: Callable[[], str | None] | None = None
_limiter: Any = None                      # a market.cache.RateLimiter, built on first use (see `_allowed`)
_paused_until = 0.0
_lock = threading.Lock()
_clock: Callable[[], float] = time.monotonic


def set_base(url: str | None) -> None:
    """Tests only: send the calls to a local fake server (`http://127.0.0.1:port`). The `read_exceptions` check keeps
    using `api.schwabapi.com`, so the same rules apply to the fake."""
    global _base_override
    _base_override = url.rstrip("/") if url else None


def set_token_provider(provider: Callable[[], str] | None, refresher: Callable[[], str | None] | None = None) -> None:
    """Tests only: where the access token comes from, and how a 401 renews it (returns the new token or None)."""
    global _provider, _refresher
    _provider, _refresher = provider, refresher


def reset_limits(clock: Callable[[], float] | None = None, limit: int = CALLS_PER_MINUTE) -> None:
    """Tests only: forget the pause and the call count, optionally with a fake clock."""
    global _limiter, _paused_until, _clock
    _clock = clock or time.monotonic
    _limiter = _new_limiter(limit)
    _paused_until = 0.0


def _new_limiter(limit: int) -> Any:
    # imported here: hokma.market imports hokma.schwab.endpoints, which imports this module (circular at load time)
    from hokma.market.cache import RateLimiter

    return RateLimiter(limit, 60.0, clock=_clock)


def _allowed() -> bool:
    global _limiter
    if _limiter is None:
        _limiter = _new_limiter(CALLS_PER_MINUTE)
    return _limiter.allow()


def _token(force: bool = False) -> str:
    if _provider is not None:
        if force:
            token = _refresher() if _refresher else None
            if not token:
                raise SchwabError(EXPIRED, 401)
            return token
        return _provider()
    from hokma.schwab import auth

    return auth.access_token(force_refresh=force)


def _query_text(query: dict | None) -> str:
    """`key=value` pairs: `None` skipped, booleans as `true`/`false`, `$` as `%24` and spaces as `%20` (REF 2.1)."""
    pairs: list[tuple[str, str]] = []
    for key, value in (query or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        pairs.append((str(key), str(value)))
    return urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote, safe=",:")


def request(method: str, path: str, query: dict | None = None, *, body: bytes | None = None,
            headers: dict | None = None, authed: bool = True, consumer: str = CONSUMER, reply: bool = False) -> Any:
    """One call. `path` is the full path (`/marketdata/v1/quotes`); an already encoded segment (`%24SPX`) is kept.
    `consumer` picks the list that must allow it (see the module docstring). Returns the decoded JSON, or None for an
    empty body; with `reply=True` a `Reply` that also carries the status, the correlation id and `Location`.
    Raises `SchwabError`."""
    global _paused_until
    method = method.upper()
    segments = path.split("/")[1:] if path.startswith("/") else []
    shaped = bool(segments) and all(s and not unsafe_segment(s) for s in segments) and "?" not in path and "#" not in path
    rule = exception_match(HOST, method, path, consumer) if shaped else None    # no empty, dot, encoded dot or slash, or query segments
    if rule is None:
        log.warning("schwab call refused before the network: %s", method)
        raise SchwabError(FORBIDDEN_ROUTE, 403)
    logged = str(rule.get("path"))                        # the template: no account hash, no id, no symbol
    if _clock() < _paused_until or not _allowed():
        raise SchwabError(RATE_LIMITED, 429)
    attempts = 2 if authed else 1
    for attempt in range(attempts):
        send_headers = {"User-Agent": "Hokma", "Accept": "application/json", **(headers or {})}
        if authed:
            send_headers["Authorization"] = "Bearer " + _token(force=attempt > 0)
        base = _base_override or BASE
        url = base + path + (("?" + _query_text(query)) if query else "")
        started = time.monotonic()
        sent = _send(method, url, send_headers, body)
        status, text, correl = sent[:3]
        location = sent[3] if len(sent) > 3 else None          # a fourth item: the Location header (tests may return three)
        log.info("schwab %s %s -> %s in %d ms correl=%s", method, logged, status,
                 int((time.monotonic() - started) * 1000), correl or "-")
        if status == 401 and authed and attempt == 0:
            continue
        break
    if 200 <= status < 300:
        decoded = None
        if text.strip():
            try:
                decoded = json.loads(text)
            except ValueError:
                raise SchwabError(UPSTREAM, 502, correl_id=correl) from None
        return Reply(decoded, status, correl, location) if reply else decoded
    errors = _errors(text)
    if status == 401:
        raise SchwabError(EXPIRED, 401, correl_id=correl, errors=errors)
    if status == 403:
        raise SchwabError(DENIED, 403, correl_id=correl, errors=errors)
    if status == 429:
        _paused_until = _clock() + RATE_PAUSE_SECONDS
        raise SchwabError(RATE_LIMITED, 429, correl_id=correl, errors=errors)
    if status == 404:
        raise SchwabError(NOT_FOUND, 404, correl_id=correl, errors=errors)
    if 400 <= status < 500:
        raise SchwabError(BAD_REQUEST, status, correl_id=correl, errors=errors, detail=_oauth_error(text))
    raise SchwabError(UPSTREAM, 502, correl_id=correl, errors=errors)


def get(path: str, query: dict | None = None) -> Any:
    return request("GET", path, query)


def _send(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, str, str | None, str | None]:
    from hokma.market.history import _KeepKeysOnHost    # deferred: see `_new_limiter`

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with tls.opener(_KeepKeysOnHost).open(req, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read(MAX_BODY_BYTES).decode("utf-8", "replace"), \
                response.headers.get("Schwab-Client-CorrelId"), response.headers.get("Location")
    except urllib.error.HTTPError as error:
        try:
            text = error.read(MAX_BODY_BYTES).decode("utf-8", "replace")
        except OSError:
            text = ""
        return error.code, text, error.headers.get("Schwab-Client-CorrelId") if error.headers else None, None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SchwabError(UNREACHABLE, 502) from None


def _errors(text: str) -> list[dict]:
    """`errors[]` of REF 5, kept to the fields that carry no secret."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data.get("errors") if isinstance(data, dict) else None
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            out.append({k: item[k] for k in ("id", "status", "title", "detail", "source") if k in item})
        elif isinstance(item, str):
            out.append({"detail": item})
    return out


def _oauth_error(text: str) -> str | None:
    """The `error` of an OAuth answer (`invalid_grant`...), short and without values."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    value = data.get("error") if isinstance(data, dict) else None
    return value[:64] if isinstance(value, str) else None
