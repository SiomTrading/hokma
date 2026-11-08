"""Server for the chart-state E2E: the real app (create_app) on a free port and a compiled dashboard folder, with a
FAKE market provider plugged in the way the engine tests do it (`register_adapter`, `set_source`). No network is used.

    python e2e/chart-state-serve.py <dist-folder> <port>

The fake provider:
- serves bars for any symbol that looks like a crypto pair (`BTC-USD`, `S01-USD`), built from one price curve of the
  time, so a 7m bar and the 1m bars it is made of agree;
- streams one trade per symbol every 250 ms (4 messages per second). The clock of those trades runs
  HOKMA_E2E_VIRTUAL_MS (default 30000) per trade, so a 7m bar closes every few seconds and a test does not wait for
  real minutes;
- counts what the engine asks of it: `connect`, `subscribe`, `unsubscribe` (GET /api/e2e/counters);
- drops its connection on demand (POST /api/e2e/drop) and comes back by itself, which makes the hub send a new snapshot.

Prints one JSON line with the session token once it is listening, then serves until it is killed.
"""
import asyncio
import json
import math
import os
import sys
import time
from pathlib import Path

import uvicorn

dist, port = Path(sys.argv[1]), int(sys.argv[2])

import hokma.core.app as app_module  # noqa: E402
from hokma.core import session  # noqa: E402
from hokma.core.config import hokma_home  # noqa: E402
from hokma.db import migrate, open_database  # noqa: E402
from hokma.market import set_source  # noqa: E402
from hokma.market.adapters import register_adapter  # noqa: E402
from hokma.market.models import Candle, Series, SessionPeriod, SymbolHit  # noqa: E402
from hokma.market.stream import LiveSource, SourceCapabilities, SourceStatus, Tick  # noqa: E402
from hokma.projects import service  # noqa: E402
from hokma.rpc.ws import SESSION_TOKEN  # noqa: E402

VIRTUAL_MS = int(os.environ.get("HOKMA_E2E_VIRTUAL_MS", "30000"))
TICK_SECONDS = 0.25
KNOWN = ("BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD")

COUNTERS = {"connect": 0, "subscribe": 0, "unsubscribe": 0, "sessions": 0, "subscribed": []}
DROP = {"event": None}


def curve(symbol: str, ts: float) -> float:
    """One price curve per symbol, a function of the time only."""
    seed = sum(ord(c) for c in symbol) % 50
    return 100 + seed + 20 * math.sin(ts / 17000) + 4 * math.sin(ts / 900) + 1.5 * math.sin(ts / 97)


class FakeHistory:
    name = "E2E Provider"

    def search(self, query: str) -> list[SymbolHit]:
        text = query.strip().lower()
        return [SymbolHit(s, s.split("-")[0] + " (E2E)", "crypto", "E2E") for s in KNOWN if text in s.lower()]

    def history(self, symbol: str, timeframe: str) -> Series:
        seconds = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1D": 86400}[timeframe]
        count = {"1m": 1500, "5m": 900, "15m": 600, "1h": 500, "1D": 400}[timeframe]
        now = int(time.time())
        last = now // seconds * seconds
        candles = []
        for k in range(count - 1, -1, -1):
            t = last - k * seconds
            o, c = curve(symbol, t), curve(symbol, t + seconds - 1)
            candles.append(Candle(t, o, max(o, c) + 0.3, min(o, c) - 0.3, c, 10.0))
        return Series(
            symbol=symbol, name=symbol + " (E2E)", kind="crypto", exchange="E2E", currency="USD", timezone="UTC",
            utc_offset_seconds=0, candles=candles, previous_close=None, last_trade_at=now,
            session=SessionPeriod(now - 3600, now + 86400), fetched_at=now, stale=False, source="E2E Provider",
        )


class FakeLive(LiveSource):
    name = "E2E Provider"
    provider_id = "coinbase"
    data_state = "live"
    capabilities = SourceCapabilities(True, 100, ("crypto",), 0)

    def __init__(self) -> None:
        super().__init__(backoff=(0.5,))
        self.virtual_ms = int(time.time() * 1000)
        self.count = 0

    async def connect(self) -> None:
        COUNTERS["connect"] += 1
        await super().connect()

    async def subscribe(self, symbols: list[str]) -> None:
        COUNTERS["subscribe"] += 1
        COUNTERS["subscribed"].append(list(symbols))
        await super().subscribe(symbols)

    async def unsubscribe(self, symbols: list[str]) -> None:
        COUNTERS["unsubscribe"] += 1
        await super().unsubscribe(symbols)

    async def _session(self) -> None:
        COUNTERS["sessions"] += 1
        DROP["event"] = asyncio.Event()
        self._mark_ready()
        while True:
            try:
                await asyncio.wait_for(DROP["event"].wait(), TICK_SECONDS)
                raise OSError("e2e drop")
            except asyncio.TimeoutError:
                pass
            self.count += 1
            self.virtual_ms += VIRTUAL_MS
            for symbol in sorted(self._symbols):
                price = curve(symbol, self.virtual_ms / 1000) + (self.count % 1000) * 0.01
                self.emit(Tick(symbol, self.virtual_ms, round(price, 2), 1.0))


# The clock of the fake trades runs about 120 times faster than the real one, so a feed kept alive for a minute after its
# last chart closed would hold bars far ahead of the history. These tests are about the view, not about the linger of the
# tick tape (that has its own tests in backend/tests/test_market_ticks_hub.py): the engine here lets go at once.
import hokma.market.hub as hub_module  # noqa: E402

hub_module.LINGER_SECONDS = 0
register_adapter("coinbase", lambda spec, secret, url: FakeLive())
set_source(FakeHistory())

app_module.dashboard_dist = lambda: dist
ONCE = session.new_owner_secret()                       # the one-time code of the start link, for the tab
session.arm_owner_secret_for_tests(ONCE)
migrate()
home = hokma_home()
home.mkdir(parents=True, exist_ok=True)
db = open_database()
with db.write() as u:
    u.profile.set_owner("E2E Owner", "E2E Fund")
    u.profile.complete_setup()
service.ensure_default(db)
db.close()

app = app_module.create_app(port)


async def counters() -> dict:
    return COUNTERS


async def drop() -> dict:
    if DROP["event"] is not None:
        DROP["event"].set()
    return {"ok": True}


# In front of the catch-all that serves the dashboard.
from fastapi.routing import APIRoute  # noqa: E402

app.router.routes.insert(0, APIRoute("/api/e2e/counters", counters, methods=["GET"]))
app.router.routes.insert(0, APIRoute("/api/e2e/drop", drop, methods=["POST"]))

print(json.dumps({"token": SESSION_TOKEN, "once": ONCE, "home": str(home)}), flush=True)
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
