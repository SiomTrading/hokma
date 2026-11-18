"""HTTP streaming shared by the two wires, on the standard library (no new dependency).

Neutral messages (what the transcript keeps):
  {"role": "user", "text": str}
  {"role": "assistant", "text": str, "calls": [{"id", "name", "input"}]}
  {"role": "tool", "results": [{"id", "name", "text", "is_error"}]}
A wire turns them into its own shape and turns the stream back into items:
  ("text", str) | ("call", {"id","name","input"}) | ("usage", {...}) | ("stop", reason)
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Iterator
from hokma.core import tls

MAX_JSON_BYTES = 8 * 1024 * 1024        # a provider's model list; more than this is not a list (Vera B2, 0.0.39)
MAX_ERROR_BYTES = 64 * 1024


class WireError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status, self.body = status, body


class Stream:
    """One streaming POST. `close()` from another thread ends a read in progress (a stop from the owner)."""

    def __init__(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout: float = 120.0) -> None:
        self.url, self.headers, self.body, self.timeout = url, headers, body, timeout
        self._resp: Any = None
        self.closed = False

    def lines(self) -> Iterator[str]:
        req = urllib.request.Request(self.url, data=json.dumps(self.body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                                              "User-Agent": "Hokma", **self.headers})
        try:
            self._resp = tls.urlopen(req, timeout=self.timeout)          # noqa: S310
        except urllib.error.HTTPError as exc:
            raise WireError(exc.code, exc.read().decode("utf-8", "replace")) from exc
        except urllib.error.URLError as exc:
            raise WireError(0, str(exc.reason)) from exc
        try:
            for raw in self._resp:
                if self.closed:
                    return
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    yield line[5:].strip()
        except (OSError, ValueError):
            if not self.closed:
                raise
        finally:
            self.close()

    def close(self) -> None:
        self.closed = True
        resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def _read_capped(resp: Any, limit: int, deadline: float) -> bytes:
    """At most `limit` bytes, and never past `deadline` in total: a server that drips its answer one byte at a time
    cannot hold the caller longer than that (a per-read timeout alone would let it)."""
    chunks: list[bytes] = []
    size = 0
    while True:
        if time.monotonic() > deadline:
            raise WireError(0, "the answer took too long")
        sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
        chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise WireError(0, f"the answer is larger than {limit} bytes")
        chunks.append(chunk)


def get_json(url: str, headers: dict[str, str], timeout: float = 20.0) -> Any:
    """GET a JSON document with a cap on its size and on the total time (connect, wait and read)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Hokma", **headers})
    deadline = time.monotonic() + timeout
    try:
        with tls.urlopen(req, timeout=timeout) as resp:                  # noqa: S310
            return json.loads(_read_capped(resp, MAX_JSON_BYTES, deadline).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_ERROR_BYTES)                 # one bounded read: the start of the refusal is enough
        except (OSError, ValueError):
            body = b""
        raise WireError(exc.code, (body or b"").decode("utf-8", "replace")) from exc
    except urllib.error.URLError as exc:
        raise WireError(0, str(exc.reason)) from exc
    except (TimeoutError, OSError) as exc:
        raise WireError(0, f"{type(exc).__name__}: {exc}") from exc
