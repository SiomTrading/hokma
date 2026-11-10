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
import urllib.error
import urllib.request
from typing import Any, Iterator


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
            self._resp = urllib.request.urlopen(req, timeout=self.timeout)          # noqa: S310
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


def get_json(url: str, headers: dict[str, str], timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "Hokma", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:                  # noqa: S310
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise WireError(exc.code, exc.read().decode("utf-8", "replace")) from exc
    except urllib.error.URLError as exc:
        raise WireError(0, str(exc.reason)) from exc
