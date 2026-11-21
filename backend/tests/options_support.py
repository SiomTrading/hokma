"""A fake Alpaca for the option chain tests (SPEC-014): serves the files of options_fixtures/ by URL, keeps every request.

Contracts are filtered and paged like the real endpoint (`expiration_date`, `_gte`, `_lte`, `type`, strikes, `limit`, `page_token`);
snapshots are filtered the same way. The trading hosts and the data host answer on their own paths only."""
import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hokma.market.errors import NETWORK, MarketError

FIXTURES = Path(__file__).parent / "options_fixtures"
OPEN_NOW = datetime(2026, 11, 20, 16, 0, 5, tzinfo=timezone.utc).timestamp()      # Friday 11:00 in New York
CLOSED_NOW = datetime(2026, 11, 21, 17, 0, 0, tzinfo=timezone.utc).timestamp()    # Saturday
DEMO_KEY = {"key_id": "PKDEMOKEYID1234567", "secret": "demoSECRETvalueXYZ98765432"}
REAL_KEY = {"key_id": "AKREALKEYID1234567", "secret": "realSECRETvalueABC12345678"}
PAPER, LIVE, DATA = "paper-api.alpaca.markets", "api.alpaca.markets", "data.alpaca.markets"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Connections:
    loaded = True

    def __init__(self, keys: dict | None = None, off: tuple[str, ...] = (), options: tuple[str, ...] = ("schwab", "alpaca"),
                 signed: bool = True) -> None:
        self.keys, self.off, self.options, self.signed = keys or {}, off, options, signed

    def order(self, use: str) -> list[str]:
        """The order of the use "options" of Connections (4.4); Schwab first by default."""
        assert use == "options"
        return list(self.options)

    def signed_in(self, provider_id: str) -> bool:
        return provider_id == "schwab" and self.signed

    def is_enabled(self, provider_id: str) -> bool:
        return provider_id not in self.off

    def has_key(self, provider_id: str) -> bool:
        return provider_id in self.keys

    def read_secret(self, provider_id: str) -> str | None:
        return self.keys.get(provider_id)


class FakeAlpaca:
    def __init__(self, snapshots: str = "snapshots_indicative_open.json", opra: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.contracts = load("contracts_page1.json")["option_contracts"] + load("contracts_page2.json")["option_contracts"]
        self.by_symbol = {c["symbol"]: c for c in self.contracts}
        self.indicative = load(snapshots)["snapshots"]
        self.opra_file = load("snapshots_opra_open.json")["snapshots"]
        self.opra = opra                         # the account has OPRA: feed=opra answers 200
        self.page_size: int | None = None
        self.status: dict[str, int] = {}         # host -> forced status (401, 429, 500 ...)
        self.stock_status = 200
        self.fail = False
        self.no_open_interest = False

    # ---- helpers for the tests ----
    def hosts(self) -> list[str]:
        return [urllib.parse.urlsplit(u).netloc for u, _ in self.calls]

    def paths(self, host: str) -> list[str]:
        return [urllib.parse.urlsplit(u).path for u, _ in self.calls if urllib.parse.urlsplit(u).netloc == host]

    def queries(self, host: str) -> list[dict]:
        return [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query)) for u, _ in self.calls
                if urllib.parse.urlsplit(u).netloc == host]

    # ---- the server ----
    def _match(self, c: dict, q: dict) -> bool:
        if q.get("expiration_date") and c["expiration_date"] != q["expiration_date"]:
            return False
        if q.get("expiration_date_gte") and c["expiration_date"] < q["expiration_date_gte"]:
            return False
        if q.get("expiration_date_lte") and c["expiration_date"] > q["expiration_date_lte"]:
            return False
        if q.get("type") and c["type"] != q["type"]:
            return False
        if q.get("strike_price_gte") and float(c["strike_price"]) < float(q["strike_price_gte"]):
            return False
        if q.get("strike_price_lte") and float(c["strike_price"]) > float(q["strike_price_lte"]):
            return False
        return True

    def _page(self, rows: list, q: dict) -> tuple[list, str | None]:
        size = min(int(q.get("limit", 100)), self.page_size or 10000)
        start = int(q.get("page_token") or 0)
        chunk = rows[start:start + size]
        return chunk, (str(start + size) if start + size < len(rows) else None)

    def __call__(self, url: str, headers: dict) -> tuple[int, str]:
        self.calls.append((url, dict(headers)))
        if self.fail:
            raise MarketError(NETWORK, 502)
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        forced = self.status.get(parts.netloc)
        if forced:
            return forced, json.dumps({"message": "forced"})
        if parts.netloc in (PAPER, LIVE) and parts.path == "/v2/options/contracts":
            rows = [c for c in self.contracts if self._match(c, q)]
            chunk, token = self._page(rows, q)
            if self.no_open_interest:
                chunk = [{k: v for k, v in c.items() if not k.startswith("open_interest")} for c in chunk]
            return 200, json.dumps({"option_contracts": chunk, "next_page_token": token})
        if parts.netloc == DATA and parts.path.startswith("/v1beta1/options/snapshots/"):
            if q.get("feed") == "opra" and not self.opra:
                return 403, (FIXTURES / "opra_403.json").read_text(encoding="utf-8")
            source = self.opra_file if q.get("feed") == "opra" else self.indicative
            symbols = [s for s in source if self._match(self.by_symbol[s], q)]
            chunk, token = self._page(symbols, q)
            return 200, json.dumps({"snapshots": {s: source[s] for s in chunk}, "next_page_token": token})
        if parts.netloc == DATA and parts.path.endswith("/snapshot") and parts.path.startswith("/v2/stocks/"):
            if self.stock_status != 200:
                return self.stock_status, "{}"
            return 200, (FIXTURES / "stock_snapshot.json").read_text(encoding="utf-8")
        return 404, "{}"


QUOTE = {"symbol": "AAPL", "name": "Apple", "last": 228.4, "change": -0.65, "change_pct": -0.28, "previous_close": 229.05,
         "high": 229.9, "low": 227.8, "volume": 731132, "range": "1D", "currency": "USD", "time": int(datetime(2026, 11, 20, 20, 59, 59, tzinfo=timezone.utc).timestamp()),
         "market_open": False, "delayed": False, "stale": False, "source": "Yahoo Finance"}


def install(monkeypatch, keys: dict | None = DEMO_KEY, *, snapshots: str = "snapshots_indicative_open.json",
            now: float = OPEN_NOW, opra: bool = False, off: tuple[str, ...] = (), quote: dict | None = QUOTE,
            allowed=None) -> FakeAlpaca:
    """Plug the fake network, a fixed clock, the saved key and the chart quote into the module."""
    from hokma.connections import runtime
    from hokma.kit import market_tools
    from hokma.market import options

    monkeypatch.setenv("HOKMA_LANG", "en")
    options.reset()
    fake = FakeAlpaca(snapshots, opra)
    connections = Connections({"alpaca": json.dumps(keys, separators=(",", ":"))} if keys else {}, off)
    monkeypatch.setattr(options, "_fetch", fake)
    monkeypatch.setattr(options, "_clock", lambda: fake.now)
    fake.now = now
    monkeypatch.setattr(runtime, "get_service", lambda: connections)
    monkeypatch.setattr(runtime, "_desk_providers", lambda desk_id: allowed)

    def fake_quote(symbol, desk_id=None):
        if quote is None:
            raise LookupError("no market data")
        return {**quote, "symbol": symbol}

    monkeypatch.setattr(market_tools, "quote", fake_quote)
    fake.connections = connections
    return fake


@pytest.fixture
def alpaca(monkeypatch):
    return install(monkeypatch)
