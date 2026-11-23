"""Helpers for the data module tests: recorded answers, a fake market source, a transport that records calls, and a guard
against any real network connection."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pytest

from hokma.market.models import Candle, Series

FIXTURES = Path(__file__).parent / "fixtures"
UTC = timezone.utc


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def daily_candles() -> list[Candle]:
    frame = pd.read_csv(FIXTURES / "bars_daily.csv")
    return [Candle(int(r.time), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)) for r in frame.itertuples()]


class FakeMarketSource:
    """A market source built on a recorded answer. Counts the calls it receives."""

    name = "fake"

    def __init__(self, candles: list[Candle] | None = None, *, stale: bool = False, fetched_at: int | None = None,
                 error: Exception | None = None) -> None:
        self.candles = daily_candles() if candles is None else candles
        self.stale, self.fetched_at, self.error = stale, fetched_at, error
        self.calls: list[tuple[str, str]] = []

    def search(self, query: str) -> list:
        return []

    def history(self, symbol: str, timeframe: str) -> Series:
        self.calls.append((symbol, timeframe))
        if self.error is not None:
            raise self.error
        return Series(symbol=symbol, name=symbol, kind="equity", exchange="TEST", currency="USD", timezone="UTC",
                      utc_offset_seconds=0, candles=list(self.candles), previous_close=None, last_trade_at=None, session=None,
                      fetched_at=self.fetched_at or int(datetime(2024, 4, 1, tzinfo=UTC).timestamp()), stale=self.stale, source="recorded")


class RecordedTransport:
    """A transport `(url, params, headers) -> (status, body)` that answers from a function and records every call."""

    def __init__(self, answer: Callable[[str, dict, dict], tuple[int, bytes]]) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url: str, params: Any, headers: Any) -> tuple[int, bytes]:
        self.calls.append((url, dict(params), dict(headers)))
        return self.answer(url, dict(params), dict(headers))


def constant(status: int, body: bytes | str = b"") -> Callable[[str, dict, dict], tuple[int, bytes]]:
    data = body.encode() if isinstance(body, str) else body
    return lambda url, params, headers: (status, data)


def when(day: str) -> datetime:
    """A UTC-aware datetime from an ISO date or time (a naive text is read as UTC)."""
    moment = datetime.fromisoformat(day.replace("Z", "+00:00"))
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any attempt to open a connection fails the test. Returns the (empty) list of attempts."""
    attempts: list[str] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        attempts.append("connection")
        raise AssertionError("a test opened a network connection")

    import http.client
    import socket
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)
    return attempts


@pytest.fixture
def market_source(monkeypatch: pytest.MonkeyPatch):
    """Install a fake market source in the public API of `hokma.market` and remove it afterwards."""
    from hokma.market import api as market_api

    def install(source: FakeMarketSource) -> FakeMarketSource:
        market_api.set_source(source)
        return source

    yield install
    market_api.set_source(None)
