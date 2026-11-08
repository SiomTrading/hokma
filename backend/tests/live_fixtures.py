"""Fixtures of the live price and Connections tests. Import the ones a test needs:
`from live_fixtures import live, Collector, AUTH  # noqa: F401`."""
import json
import queue
import threading
import time

import keyring
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hokma.connections import runtime
from hokma.core.app import create_app
from hokma.market import api as market_api
from hokma.market import hub as hub_module
from hokma.market import stream
from hokma.market.cache import RateLimiter
from hokma.market.providers import set_catalog
from hokma.market.yahoo import YahooSource
from hokma.rpc.ws import SESSION_TOKEN
from live_support import MemoryKeyring, catalog_with
from market_support import RECORDED_AT, FakeFetcher

AUTH = {"X-Hokma-Token": SESSION_TOKEN}
WS_URL = f"/ws/market?token={SESSION_TOKEN}"


class Collector:
    """Reads a test WebSocket on a thread and keeps every message with the time it arrived."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.items: queue.Queue = queue.Queue()
        self.all: list[dict] = []
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            try:
                text = self.ws.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                self.closed = True
                self.items.put(None)
                return
            message = json.loads(text)
            message["_at"] = time.monotonic()
            self.all.append(message)
            self.items.put(message)

    def send(self, payload: dict) -> None:
        self.ws.send_text(json.dumps(payload))

    def next(self, predicate=None, timeout: float = 5.0) -> dict:
        """The next message that satisfies `predicate` (earlier ones are skipped)."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no matching message; saw {[m['type'] for m in self.all]}")
            try:
                message = self.items.get(timeout=left)
            except queue.Empty:
                continue
            if message is None:
                raise AssertionError("socket closed")
            if predicate is None or predicate(message):
                return message

    def find(self, predicate, timeout: float = 5.0, after: int = 0) -> dict:
        """The first message (among those already received too, from index `after`) that satisfies `predicate`."""
        end = time.monotonic() + timeout
        while True:
            for message in list(self.all)[after:]:
                if predicate(message):
                    return message
            if time.monotonic() >= end:
                raise AssertionError(f"no matching message; saw {[(m['type'], m.get('state')) for m in self.all]}")
            time.sleep(0.01)

    def of(self, kind: str, sub: str | None = None) -> list[dict]:
        return [m for m in list(self.all) if m["type"] == kind and (sub is None or m.get("sub") == sub)]

    def drain(self, quiet: float = 0.4) -> None:
        """Wait until nothing arrives for `quiet` seconds."""
        while True:
            try:
                if self.items.get(timeout=quiet) is None:
                    return
            except queue.Empty:
                return


class Live:
    def __init__(self, tmp_path, monkeypatch) -> None:
        self.tmp_path, self.monkeypatch = tmp_path, monkeypatch
        self.keyring = MemoryKeyring()
        self.fetcher = FakeFetcher()
        self.yahoo_now = RECORDED_AT
        self.clients: list[TestClient] = []

    def use(self, urls: dict[str, str] | None = None, polling_interval: float = 0.2, **overrides) -> None:
        set_catalog(catalog_with(urls or {}, polling_interval, **overrides))
        runtime.reset()

    def app(self) -> TestClient:
        client = TestClient(create_app())
        self.clients.append(client)
        return client

    def connect(self, client: TestClient):
        return client.websocket_connect(WS_URL)


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "en")
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(stream, "BACKOFF_SECONDS", (0.05, 0.05, 0.1))
    monkeypatch.setattr(hub_module, "LINGER_SECONDS", 0)               # the tests that watch the linger set it themselves
    previous = keyring.get_keyring()
    state = Live(tmp_path, monkeypatch)
    keyring.set_keyring(state.keyring)
    market_api.set_source(YahooSource(tmp_path / "cache", fetch=state.fetcher, clock=lambda: state.yahoo_now,
                                      limiter=RateLimiter(limit=100000, window=60)))
    market_api.set_clock(lambda: RECORDED_AT)
    monkeypatch.setattr(market_api, "_requests", RateLimiter(limit=10000, window=60))
    state.use()
    yield state
    runtime.reset()
    set_catalog(None)
    market_api.set_source(None)
    market_api.set_clock(time.time)
    keyring.set_keyring(previous)
