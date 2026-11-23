"""The client of the official web API of IBKR, through the gateway that runs on the student's own computer (SPEC-018 B5: OA-55).

Only a gateway on this machine is ever called: the base address must be `https://localhost:PORT/...` or `https://127.0.0.1:PORT/...`. The
gateway keeps the login session, so there is no token or key here. Certificates are always verified with the same context as the other outbound
calls of Hokma (`hokma.core.tls`): the gateway needs a certificate that the computer trusts. A redirect is never followed.

This module only builds and sends requests (`request`, `get`, `post_json`, `delete`); which calls place, change or cancel an order is decided in
`hokma.broker.ibkr`. Logs carry the method, the HTTP status and the milliseconds, never a path, a body or an identifier.
The transport is injectable: `Gateway(transport=fn)` with `fn(method, url, headers, body) -> (status, text)`, so tests answer recorded bodies.
Blocking: call it from a thread.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from hokma.core import tls

log = logging.getLogger("hokma.broker")

DEFAULT_BASE = "https://localhost:5000/v1/api"
LOOPBACK = ("localhost", "127.0.0.1")
TIMEOUT_SECONDS = 15
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024

UNAVAILABLE = "ibkr.unavailable"            # the gateway does not answer
NOT_SIGNED_IN = "ibkr.not_signed_in"        # the gateway answers but there is no login session (401, 403)
BAD_REQUEST = "ibkr.bad_request"
NOT_FOUND = "ibkr.not_found"
UPSTREAM = "ibkr.upstream"
BAD_BASE = "ibkr.bad_base"
BAD_PATH = "ibkr.bad_path"

Transport = Callable[[str, str, dict, "bytes | None"], "tuple[int, str]"]


class IbkrError(Exception):
    """`code` is one of the words above; `status` is the HTTP status of the gateway (0 when it did not answer). No text of the gateway is kept."""

    def __init__(self, code: str, status: int = 0) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with tls.opener(_NoRedirect).open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read(MAX_BODY_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            text = error.read(MAX_BODY_BYTES).decode("utf-8", "replace")
        except OSError:
            text = ""
        return error.code, text
    except (urllib.error.URLError, TimeoutError, OSError):
        raise IbkrError(UNAVAILABLE) from None


def check_base(base: str) -> str:
    """The base address, when it is a gateway on this machine; IbkrError(BAD_BASE) otherwise."""
    parts = urllib.parse.urlsplit(base)
    if parts.scheme != "https" or parts.hostname not in LOOPBACK or parts.username or parts.password or parts.query or parts.fragment:
        raise IbkrError(BAD_BASE)
    try:
        parts.port
    except ValueError:
        raise IbkrError(BAD_BASE) from None
    return base.rstrip("/")


def _segment_ok(segment: str) -> bool:
    return bool(segment) and segment not in (".", "..") and all(c.isalnum() or c in "-_." for c in segment)


class Gateway:
    def __init__(self, base: str = DEFAULT_BASE, transport: Transport | None = None) -> None:
        self.base = check_base(base)
        self._transport = transport or _urllib_transport

    def request(self, method: str, path: str, query: dict[str, Any] | None = None, *, body: Any = None) -> Any:
        """One call. `path` starts with "/" and each of its segments is letters, digits, dash, underscore or dot (the caller quotes nothing: a
        segment with anything else is refused before the network is opened). Returns the decoded JSON, or None for an empty body."""
        method = method.upper()
        segments = path.split("/")[1:] if path.startswith("/") else []
        if method not in ("GET", "POST", "DELETE") or not segments or not all(_segment_ok(s) for s in segments):
            raise IbkrError(BAD_PATH)
        pairs = [(str(k), "true" if v is True else "false" if v is False else str(v)) for k, v in (query or {}).items() if v is not None]
        url = self.base + path + (("?" + urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)) if pairs else "")
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        if data is not None and len(data) > MAX_REQUEST_BYTES:
            raise IbkrError(BAD_REQUEST)
        headers = {"User-Agent": "Hokma", "Accept": "application/json", **({"Content-Type": "application/json"} if data is not None else {})}
        started = time.monotonic()
        status, text = self._transport(method, url, headers, data)
        log.info("ibkr %s -> %s in %d ms", method, status, int((time.monotonic() - started) * 1000))
        if 200 <= status < 300:
            if not text.strip():
                return None
            try:
                return json.loads(text)
            except ValueError:
                raise IbkrError(UPSTREAM, status) from None
        if status in (401, 403):
            raise IbkrError(NOT_SIGNED_IN, status)
        if status == 404:
            raise IbkrError(NOT_FOUND, status)
        if 400 <= status < 500:
            raise IbkrError(BAD_REQUEST, status)
        raise IbkrError(UPSTREAM, status)

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, query)

    def post_json(self, path: str, payload: Any, query: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, query, body=payload)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)
