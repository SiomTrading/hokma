"""Shared pieces of the market tests. The tests never touch the network: answers are real Yahoo responses recorded
once into `market_fixtures/`, and every URL is served from those files."""
import json
import urllib.parse
from pathlib import Path

from hokma.market.errors import NETWORK, MarketError

FIXTURES = Path(__file__).parent / "market_fixtures"
# Moment the fixtures were recorded (epoch seconds). The US stock market was closed, CME futures were trading.
RECORDED_AT = int((FIXTURES / "RECORDED_AT").read_text().strip())

CHART_FILES = {
    ("AAPL", "5m"): "aapl_5m.json",
    ("AAPL", "1d"): "aapl_1d.json",
    ("NQ=F", "5m"): "nqf_5m.json",
    ("NQ=F", "1m"): "nqf_1m.json",
    ("BTC-USD", "1h"): "btc_1h.json",
}


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> dict:
    return json.loads(fixture_text(name))


class FakeFetcher:
    """Callable with the `Fetcher` shape. Serves the recorded files, counts calls and can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: str | None = None  # "network" | "429" | "500"
        self.overrides: dict[str, tuple[int, str]] = {}

    def __call__(self, url: str) -> tuple[int, str]:
        self.calls.append(url)
        if self.fail == "network":
            raise MarketError(NETWORK, 502)
        if self.fail == "429":
            return 429, "{}"
        if self.fail == "500":
            return 500, "oops"
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if "/v8/finance/chart/" in parsed.path:
            symbol = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            interval = query["interval"][0]
            if symbol in self.overrides:
                return self.overrides[symbol]
            name = CHART_FILES.get((symbol, interval))
            if name:
                return 200, fixture_text(name)
            recorded = fixture_json("unknown_symbol.json")
            return recorded["status"], json.dumps(recorded["body"])
        if "/v1/finance/search" in parsed.path:
            term = query["q"][0]
            return 200, fixture_text("search_nasdaq.json" if term == "nasdaq" else "search_empty.json")
        raise AssertionError(f"unexpected url {url}")
