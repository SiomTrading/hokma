"""Symbol map: the chart symbol (the one the search returns, for example `NQ=F`, `EURUSD=X`, `BTC-USD`, `AAPL`)
to the asset class and to the symbol each provider expects. `to_provider` returns None when the provider has no
translation for the symbol: the hub then falls back to the delayed source and the panel shows the Connections link.
"""
import re
from datetime import date

CRYPTO = re.compile(r"^([A-Z0-9]{2,10})-(USD|USDT|USDC|EUR|GBP|BTC)$")
FOREX = re.compile(r"^([A-Z]{3})([A-Z]{3})=X$")
FUTURE = re.compile(r"^([A-Z0-9]{1,4})=F$")
EQUITY = re.compile(r"^[A-Z]{1,5}(?:[.-][A-Z])?$")

KIND_CLASS = {"crypto": "crypto", "future": "future", "currency": "forex", "equity": "equity", "etf": "equity"}


def classify(symbol: str, kind: str | None = None) -> str | None:
    """`equity`, `crypto`, `forex`, `future`, or None (indices and anything else no provider streams)."""
    if CRYPTO.match(symbol):
        return "crypto"
    if FOREX.match(symbol):
        return "forex"
    if FUTURE.match(symbol):
        return "future"
    if symbol.startswith("^"):
        return None
    if kind in KIND_CLASS:
        return KIND_CLASS[kind]
    if kind is None and EQUITY.match(symbol):
        return "equity"
    return None


def to_provider(provider_id: str, symbol: str, asset_class: str | None, today: date | None = None) -> str | None:
    if asset_class is None:
        return None
    if provider_id == "coinbase":
        return symbol if asset_class == "crypto" else None
    if provider_id == "alpaca":
        return symbol.replace("-", ".") if asset_class == "equity" and EQUITY.match(symbol) else None
    if provider_id == "finnhub":
        return _finnhub(symbol, asset_class)
    if provider_id == "siomtrading_futures":
        # The server resolves the current contract from the root.
        return FUTURE.match(symbol).group(1) if asset_class == "future" and FUTURE.match(symbol) else None
    return None


def _finnhub(symbol: str, asset_class: str) -> str | None:
    if asset_class == "equity":
        return symbol.replace("-", ".") if EQUITY.match(symbol) else None
    if asset_class == "forex" and (m := FOREX.match(symbol)):
        return f"OANDA:{m.group(1)}_{m.group(2)}"
    if asset_class == "crypto" and (m := CRYPTO.match(symbol)):
        base, quote = m.groups()
        return f"BINANCE:{base}{'USDT' if quote == 'USD' else quote}"
    return None

