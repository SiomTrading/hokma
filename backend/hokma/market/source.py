"""The seam between Hokma and a market data provider.

Today's provider is Yahoo Finance (`yahoo.py`). A provider that serves bars from SiomTrading's Trading Core with a
per-student key implements the same two methods and is installed with `set_source`; nothing else changes.
"""
from typing import Protocol

from hokma.market.models import Series, SymbolHit


class MarketDataSource(Protocol):
    name: str

    def search(self, query: str) -> list[SymbolHit]:
        """Symbols matching the text. May serve a cached answer. Raises MarketError."""
        ...

    def history(self, symbol: str, timeframe: str) -> Series:
        """Recent bars for `symbol` in one of `TIMEFRAMES`, oldest first. Raises MarketError."""
        ...


# Name used in the specification.
MarketDataProvider = MarketDataSource
