"""Symbol normalization, both ways, for every provider Hokma talks to (SPEC-018: TA-68).

`to_provider(contract_id, provider)` writes the id the way a provider wants it; `from_provider(provider, symbol)` reads a
provider symbol back to the canonical id. Expirations and rolls are the provider's (decision 1251): this module never
works out a front month, it only spells what the provider already said.
"""
from __future__ import annotations

from hokma.contracts.identity import MONTH_CODES, NoContract, Parsed, canonical_id, parse_canonical, parse_symbol

PROVIDERS = ("schwab", "alpaca", "ibkr", "finnhub", "yahoo", "crypto")


def _occ(p: Parsed, pad: bool) -> str:
    root = (p.underlying or "")
    scaled = p.strike * 1000
    if (not 1 <= len(root) <= 6 or not scaled.is_finite() or scaled != scaled.to_integral_value() or not 0 < scaled < 10**8
            or not 2000 <= p.expiration.year <= 2099):
        raise NoContract(root, "this option cannot be written as an OCC symbol without changing it")
    body = f"{p.expiration:%y%m%d}{p.put_call}{int(p.strike * 1000):08d}"
    return f"{root:<6}{body}" if pad else f"{root}{body}"


def _future_code(p: Parsed, digits: int) -> str:
    year = p.expiration.year
    return f"{p.root}{MONTH_CODES[p.expiration.month - 1]}{str(year)[-digits:]}"


def to_provider(contract_id: str, provider: str) -> str:
    """The symbol of `provider` for a canonical id. Raises NoContract when that provider does not carry the instrument."""
    if provider not in PROVIDERS:
        raise NoContract(contract_id, f"unknown provider {provider!r}")
    p = parse_canonical(contract_id)
    kind = p.asset_type
    if provider == "schwab":
        if kind in ("equity", "etf"):
            return p.root.replace(".", "/")
        if kind == "option":
            return _occ(p, pad=True)
        if kind == "future":
            return "/" + _future_code(p, 2)
    elif provider == "alpaca":
        if kind in ("equity", "etf"):
            return p.root
        if kind == "option":
            return _occ(p, pad=False)
        if kind == "crypto":
            return p.root.replace("-", "/")
    elif provider == "ibkr":
        if kind in ("equity", "etf"):
            return p.root.replace(".", " ")
        if kind == "option":
            return _occ(p, pad=True)
        if kind == "future":
            return _future_code(p, 1)
    elif provider == "finnhub":
        if kind in ("equity", "etf"):
            return p.root
        if kind == "crypto":
            base, quote = p.root.split("-")
            return f"BINANCE:{base}{'USDT' if quote == 'USD' else quote}"
    elif provider == "yahoo":
        if kind in ("equity", "etf"):
            return p.root.replace(".", "-")
        if kind == "future":
            return f"{p.root}=F"
        if kind == "crypto":
            return p.root
        if kind == "cfd":
            return f"{p.root}=X"
    elif provider == "crypto":
        if kind == "crypto":
            return p.root.replace("-", "/")
    raise NoContract(contract_id, f"{provider} does not carry this instrument")


def from_provider(provider: str, symbol: str) -> str:
    """The canonical id of a symbol written by `provider`. A continuous future (Yahoo `NQ=F`) has no month: ask the provider
    for the front month through `contracts.resolve`."""
    if provider not in PROVIDERS:
        raise NoContract(symbol, f"unknown provider {provider!r}")
    text = symbol.strip().upper()
    if provider in ("schwab", "ibkr") and " " in text and not text.replace(" ", "")[-8:].isdigit():
        text = text.replace(" ", ".")             # IBKR writes a class share as "BRK B"
    if provider == "yahoo" and "-" in text and not text.endswith(("-USD", "-USDT", "-EUR", "-USDC")):
        text = text.replace("-", ".")
    parsed = parse_symbol(text)
    if parsed.expiration is None and parsed.asset_type == "future":
        return f"FUT:{parsed.root}:*"
    return canonical_id(parsed)
