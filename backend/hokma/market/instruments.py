"""Tick size per instrument, read from `instruments.json` (editing a value there needs no code change).

Classes without a known tick (crypto, indices) and futures roots missing from the table return None: the chart
then offers range bars in price only.
"""
import json
from functools import lru_cache
from pathlib import Path

from .symbols import FOREX, FUTURE

TABLE_PATH = Path(__file__).with_name("instruments.json")


@lru_cache(maxsize=1)
def _table() -> dict:
    data = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    futures = {str(root): float(tick) for root, tick in data["future"].items()}
    forex = {str(key): float(tick) for key, tick in data["forex"].items()}
    equity = float(data["equity"])
    if any(t <= 0 for t in (*futures.values(), *forex.values(), equity)):
        raise ValueError("instruments.json: every tick size must be greater than zero")
    return {"future": futures, "equity": equity, "forex": forex}


def roots() -> list[str]:
    """Futures roots with a known tick size, in table order."""
    return list(_table()["future"])


def tick_size(symbol: str, asset_class: str | None) -> float | None:
    """Tick size of `symbol` (chart format: `NQ=F`, `EURUSD=X`, `BTC-USD`, `AAPL`) or None when unknown.
    `asset_class` is the value of `symbols.classify`: `future`, `equity`, `forex`, `crypto` or None (indices)."""
    table = _table()
    if asset_class == "future":
        match = FUTURE.match(symbol)
        return table["future"].get(match.group(1)) if match else None
    if asset_class == "equity":
        return table["equity"]
    if asset_class == "forex":
        match = FOREX.match(symbol)
        if not match:
            return None
        return table["forex"]["jpy"] if "JPY" in match.groups() else table["forex"]["default"]
    return None
