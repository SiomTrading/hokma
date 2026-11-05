"""Serves recorded Yahoo answers from a folder instead of the network. Used by the end-to-end tests
(`HOKMA_MARKET_REPLAY_DIR`); it never runs unless that variable is set.

Files: `chart_<SYMBOL>_<interval>.json` and `search_<query>.json`, names with every character outside
letters and digits replaced by `_`. A missing chart file answers 404, a missing search file answers an empty list.
"""
import re
import urllib.parse
from pathlib import Path

from hokma.market.errors import NETWORK, MarketError

EMPTY_SEARCH = '{"quotes": []}'


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", text)


class ReplayFetcher:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.calls: list[str] = []

    def __call__(self, url: str) -> tuple[int, str]:
        self.calls.append(url)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if "/v8/finance/chart/" in parsed.path:
            symbol = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            interval = (query.get("interval") or [""])[0]
            return self._read(f"chart_{_safe(symbol)}_{_safe(interval)}.json", missing=(404, "{}"))
        if "/v1/finance/search" in parsed.path:
            term = (query.get("q") or [""])[0].lower()
            return self._read(f"search_{_safe(term)}.json", missing=(200, EMPTY_SEARCH))
        raise MarketError(NETWORK, 502)

    def _read(self, name: str, missing: tuple[int, str]) -> tuple[int, str]:
        try:
            return 200, (self.folder / name).read_text(encoding="utf-8")
        except OSError:
            return missing
