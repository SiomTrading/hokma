"""Server for the Connections and live chart E2E: the real app on a free port with the real hub, fake providers and
an in-memory credential store. Nothing here reaches the network.

    python e2e/connections-serve.py <dist-folder> <port>

- Coinbase, Alpaca and Finnhub are local WebSocket servers (backend/tests/live_support.py); the Alpaca fake accepts
  only the key below. Databento, Massive and SiomTrading Futures point at a dead port.
- Yahoo is replayed from e2e/market-replay; BTC-USD is derived from the AAPL recording so the crypto chart has history.
- A small control server (a second port, printed in the first line) lets the suite play the providers: send a trade,
  take a provider down and bring it back, count the subscriptions that reached it.

Prints one JSON line `{"token", "control", "alpaca_key_id", "alpaca_secret"}` once it is listening.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import keyring
import uvicorn

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "backend" / "tests"))

dist, port = Path(sys.argv[1]), int(sys.argv[2])

KEY_ID, SECRET = "AKE2EKEYID123456", "e2e-alpaca-secret-value-24680"

# Yahoo replay: the recordings of the chart suite plus a crypto symbol made from the stock one
replay = Path(tempfile.mkdtemp(prefix="hokma-live-replay-"))
for source in (HERE / "market-replay").iterdir():
    shutil.copy(source, replay / source.name)
for interval in ("1m", "5m", "15m", "1h", "1d"):
    raw = json.loads((HERE / "market-replay" / f"chart_AAPL_{interval}.json").read_text(encoding="utf-8"))
    meta = raw["chart"]["result"][0]["meta"]
    meta.update({"symbol": "BTC-USD", "instrumentType": "CRYPTOCURRENCY", "exchangeName": "CCC", "fullExchangeName": "CCC",
                 "longName": "Bitcoin USD", "shortName": "Bitcoin USD", "exchangeTimezoneName": "UTC", "timezone": "UTC", "gmtoffset": 0})
    scale = 235.0                                              # a stock near 340 becomes a coin near 80,000
    for key in ("regularMarketPrice", "chartPreviousClose", "previousClose", "regularMarketDayHigh", "regularMarketDayLow",
                "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "fulldayPrice"):
        if isinstance(meta.get(key), (int, float)):
            meta[key] = meta[key] * scale
    quote = raw["chart"]["result"][0]["indicators"]["quote"][0]
    for field in ("open", "high", "low", "close"):
        quote[field] = [None if v is None else v * scale for v in quote[field]]
    (replay / f"chart_BTC_USD_{interval}.json").write_text(json.dumps(raw), encoding="utf-8")
os.environ["HOKMA_MARKET_REPLAY_DIR"] = str(replay)

from hokma.connections import runtime  # noqa: E402
import hokma.core.app as app_module  # noqa: E402
from hokma.core.config import hokma_home  # noqa: E402
from hokma.db import migrate, open_database  # noqa: E402
from hokma.market.providers import set_catalog  # noqa: E402
from hokma.projects import service  # noqa: E402
from hokma.core import session  # noqa: E402
from hokma.rpc.ws import SESSION_TOKEN  # noqa: E402
from live_support import (FakeWs, MemoryKeyring, alpaca_script, catalog_with, coinbase_script, finnhub_script,  # noqa: E402
                          trade_alpaca, trade_coinbase)

keyring.set_keyring(MemoryKeyring())
fakes = {
    "coinbase": FakeWs(coinbase_script).start(),
    "alpaca": FakeWs(alpaca_script(KEY_ID, SECRET)).start(),
    "finnhub": FakeWs(finnhub_script).start(),
}
set_catalog(catalog_with({name: fake.url for name, fake in fakes.items()}, polling_interval=2.0))
runtime.reset()

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


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Control(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        result: object = {"ok": True}
        if url.path == "/tick":
            fakes["coinbase"].send_all(trade_coinbase(q.get("symbol", "BTC-USD"), float(q["price"]), iso_now()))
        elif url.path == "/atick":
            fakes["alpaca"].send_all(trade_alpaca(q.get("symbol", "AAPL"), float(q["price"]), iso_now()))
        elif url.path == "/drop":
            fakes[q["provider"]].stop()
        elif url.path == "/up":
            fakes[q["provider"]].restart()
        elif url.path == "/subs":
            fake = fakes[q["provider"]]
            result = {"subscribe": len([m for m in fake.messages() if isinstance(m, dict) and m.get("type") == "subscribe"]),
                      "unsubscribe": len([m for m in fake.messages() if isinstance(m, dict) and m.get("type") == "unsubscribe"]),
                      "clients": len(fake.clients), "at": time.time()}
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


control = ThreadingHTTPServer(("127.0.0.1", 0), Control)
threading.Thread(target=control.serve_forever, daemon=True).start()

print(json.dumps({"token": SESSION_TOKEN, "once": ONCE, "home": str(home), "control": control.server_address[1],
                  "alpaca_key_id": KEY_ID, "alpaca_secret": SECRET}), flush=True)
uvicorn.run(app_module.create_app(port), host="127.0.0.1", port=port, log_level="warning")
