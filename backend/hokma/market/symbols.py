"""Symbol map: the chart symbol (the one the search returns, for example `NQ=F`, `EURUSD=X`, `BTC-USD`, `AAPL`)
to the asset class and to the symbol each provider expects. `to_provider` returns None when the provider has no
translation for the symbol: the hub then falls back to the delayed source and the panel shows the Connections link.
"""
import re
from datetime import date, datetime, timedelta, timezone

CRYPTO = re.compile(r"^([A-Z0-9]{2,10})-(USD|USDT|USDC|EUR|GBP|BTC)$")
FOREX = re.compile(r"^([A-Z]{3})([A-Z]{3})=X$")
FUTURE = re.compile(r"^([A-Z0-9]{1,4})=F$")
EQUITY = re.compile(r"^[A-Z]{1,5}(?:[.-][A-Z])?$")

KIND_CLASS = {"crypto": "crypto", "future": "future", "currency": "forex", "equity": "equity", "etf": "equity"}

# Contract months by root: quarterly index futures, monthly crude, bi-monthly gold.
QUARTERLY = {"NQ", "ES", "YM", "RTY", "MNQ", "MES", "MYM", "M2K"}
MONTH_CODES = "FGHJKMNQUVXZ"
CONTRACT_MONTHS = {"CL": range(1, 13), "MCL": range(1, 13), "NG": range(1, 13), "GC": (2, 4, 6, 8, 10, 12),
                   "MGC": (2, 4, 6, 8, 10, 12), "SI": (3, 5, 7, 9, 12)}
DATABENTO_DATASETS = {"future": "GLBX.MDP3", "equity": "DBEQ.BASIC"}


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


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def front_contract(root: str, today: date | None = None) -> str | None:
    """The front month contract code for a root, for example `NQZ6`. Index futures roll 8 days before the third
    Friday of the expiry month. None for roots this map does not know."""
    today = today or datetime.now(timezone.utc).date()
    months = (3, 6, 9, 12) if root in QUARTERLY else CONTRACT_MONTHS.get(root)
    if months is None:
        return None
    for offset in range(0, 26):
        year, month = today.year + (today.month - 1 + offset) // 12, (today.month - 1 + offset) % 12 + 1
        if month not in months:
            continue
        expiry = _third_friday(year, month) if root in QUARTERLY else date(year, month, 1)
        cutoff = expiry - timedelta(days=8) if root in QUARTERLY else date(year, month, 1) - timedelta(days=10)
        if today < cutoff:
            return f"{root}{MONTH_CODES[month - 1]}{year % 10}"
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
    if provider_id == "databento":
        if asset_class == "future" and (m := FUTURE.match(symbol)):
            return f"{m.group(1)}.c.0"
        return symbol.replace("-", ".") if asset_class == "equity" and EQUITY.match(symbol) else None
    if provider_id == "massive":
        if asset_class == "future" and (m := FUTURE.match(symbol)):
            return front_contract(m.group(1), today)
        return None
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


def databento_dataset(asset_class: str) -> str:
    return DATABENTO_DATASETS[asset_class]
