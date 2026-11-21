"""Derived calculations over Schwab data (SPEC-015 section 16): pure functions, no network, no implicit clock.

Everything here takes data already read (`/pricehistory` candles, `/markets`, `/accounts`, `/transactions`, `/orders`, `/quotes`,
all as the JSON Schwab sends) and returns plain JSON-able values. "Now" is always a parameter (epoch seconds). Standard
library only.

What is here: historical volatility (16.12), market hours (16.15), portfolio greeks (16.16), exposure and day result
(16.17) and account activity (16.18). The expected move and the probability of expiring in the money already live in
`market/options.py`.

Option positioning analytics (max pain, put/call ratios, term structure, skew, unusual volume, dealer exposure such as
GEX and DEX) are not part of Hokma 1.0.9: they are left for GammaContext in a future version. Adding one is a new function
over the whole chain that `schwab_option_chain` already returns (every contract with its greeks and implied volatility).

Conventions (spec 12): Schwab's "no data" for a greek or a price is -999 or less and becomes `None`.
`VOLATILITY_UNIT` is read by `market/options.py`: Schwab does not publish the unit of `volatility` (point 9), "percent"
means it is divided by 100 to get the fraction sigma.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

VOLATILITY_UNIT = "percent"
NO_DATA = -999.0
DEFAULT_MULTIPLIER = 100.0
TRADING_DAYS = 252


# ---------------------------------------------------------------------------------------------- numbers

def _num(value: Any) -> float | None:
    """A finite float or None (bool, text that is not a number, NaN and infinity are None)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean(value: Any) -> float | None:
    """A greek or price: Schwab's "no data" (<= -999) is None."""
    number = _num(value)
    return None if number is None or number <= NO_DATA else number


def _r(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def _first(*values: Any) -> float | None:
    for value in values:
        number = _clean(value)
        if number is not None:
            return number
    return None


def _iso_s(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ratio(top: float, bottom: float) -> float | None:
    return None if bottom == 0 else top / bottom


# ---------------------------------------------------------------------------------------------- 16.12: HV

def historical_volatility(candles: Iterable[Mapping], n: int) -> float | None:
    """16.12: HV_n = stdev(ln(close_t / close_t-1)) * sqrt(252) over the last `n` daily candles (n - 1 returns, sample
    standard deviation). None with fewer than `n` usable candles or fewer than two returns."""
    rows = sorted((c for c in candles if isinstance(c, Mapping)), key=lambda c: _num(c.get("datetime")) or 0.0)
    closes = [x for x in (_num(c.get("close")) for c in rows) if x is not None and x > 0]
    if n < 3 or len(closes) < n:
        return None
    window = closes[-n:]
    returns = [math.log(b / a) for a, b in zip(window, window[1:])]
    return statistics.stdev(returns) * math.sqrt(TRADING_DAYS)


def volatility_history(candles: Iterable[Mapping], windows: Iterable[int] = (10, 20, 30, 60)) -> dict:
    """16.12: HV_n of the daily candles for each window, keyed by the window as text."""
    rows = list(candles)
    return {"hv": {str(n): _r(historical_volatility(rows, n)) for n in windows}}


# ---------------------------------------------------------------------------------------------- 16.15: market hours

def _intervals(payload: Mapping, market: str, product: str) -> tuple[list[tuple[float, float]], bool | None]:
    section = payload.get(market)
    entry = section.get(product) if isinstance(section, Mapping) else None
    if not isinstance(entry, Mapping):
        return [], None
    hours = entry.get("sessionHours")
    spans = []
    for item in (hours.get("regularMarket") if isinstance(hours, Mapping) else None) or []:
        try:
            spans.append((datetime.fromisoformat(item["start"]).timestamp(), datetime.fromisoformat(item["end"]).timestamp()))
        except (KeyError, ValueError, TypeError):
            continue
    flag = entry.get("isOpen")
    return spans, flag if isinstance(flag, bool) else None


def market_hours(payloads: Mapping | Iterable[Mapping], now: float, market: str = "equity", product: str = "EQ") -> dict:
    """16.15: from the answer(s) of `GET /markets` (one per date), whether the regular session is open at `now` and the
    next change: the `end` of the interval that contains now, or the next `start`. A holiday comes with `isOpen` false and
    no regular interval: closed, and the next change is the next date's opening when that date's answer is given.
    Replaces `options.market_open`, which does not know holidays."""
    items = [payloads] if isinstance(payloads, Mapping) else list(payloads)
    spans: list[tuple[float, float]] = []
    flags = []
    for item in items:
        got, flag = _intervals(item, market, product)
        spans += got
        flags.append(flag)
    spans.sort()
    current = next(((s, e) for s, e in spans if s <= now < e), None)
    if spans:
        is_open = current is not None
    else:
        is_open = bool(flags and flags[0])           # no interval at all: trust the flag (a holiday says false)
    change, kind = None, None
    if current is not None:
        change, kind = current[1], "close"
    else:
        later = [s for s, _ in spans if s > now]
        if later:
            change, kind = later[0], "open"
    return {"market": market, "product": product, "is_open": is_open, "next_change": None if change is None else _iso_s(change),
            "next_change_kind": kind}


# ---------------------------------------------------------------------------------------------- 16.16 and 16.17

def _accounts(accounts: Any) -> list[Mapping]:
    items = [accounts] if isinstance(accounts, Mapping) else list(accounts or [])
    out = []
    for item in items:
        inner = item.get("securitiesAccount") if isinstance(item, Mapping) else None
        out.append(inner if isinstance(inner, Mapping) else item)
    return [a for a in out if isinstance(a, Mapping)]


def _ref(account: Mapping, index: int, refs: Mapping[str, str] | None) -> str:
    """The public name of an account: the caller's `account_ref` for its number, else A1, A2... by order. The number
    itself never leaves this module."""
    number = str(account.get("accountNumber") or "")
    return (refs or {}).get(number) or f"A{index + 1}"


def _quote_for(quotes: Mapping, symbol: str) -> Mapping:
    entry = quotes.get(symbol)
    if not isinstance(entry, Mapping):
        squashed = symbol.replace(" ", "")
        entry = next((v for k, v in quotes.items() if isinstance(k, str) and k.replace(" ", "") == squashed), None)
    return entry if isinstance(entry, Mapping) else {}


def portfolio_greeks(accounts: Any, quotes: Mapping, account_refs: Mapping[str, str] | None = None) -> dict:
    """16.16: position greeks in share equivalents. Option: (longQuantity - shortQuantity) * m * quote greek, with the
    greeks of `/quotes` for the option symbol and m = `reference.multiplier` (else `optionMultiplier`, else 100).
    Stock or ETF: delta = longQuantity - shortQuantity, the rest 0. Totals by account and by underlying; an option
    without a quote greek is listed in `missing_greeks` and that greek is left out of its sums."""
    rows, missing = [], []
    for index, account in enumerate(_accounts(accounts)):
        ref = _ref(account, index, account_refs)
        for pos in account.get("positions") or []:
            instrument = pos.get("instrument") if isinstance(pos.get("instrument"), Mapping) else {}
            symbol = str(instrument.get("symbol") or "")
            asset = str(instrument.get("assetType") or "")
            net = (_num(pos.get("longQuantity")) or 0.0) - (_num(pos.get("shortQuantity")) or 0.0)
            if asset == "OPTION":
                entry = _quote_for(quotes, symbol)
                node = entry.get("quote") if isinstance(entry.get("quote"), Mapping) else entry
                reference = entry.get("reference") if isinstance(entry.get("reference"), Mapping) else {}
                mult = _first(reference.get("multiplier"), instrument.get("optionMultiplier")) or DEFAULT_MULTIPLIER
                greeks = {g: _clean(node.get(g)) for g in ("delta", "gamma", "theta", "vega")}
                row = {g: None if v is None else net * mult * v for g, v in greeks.items()}
                if any(v is None for v in row.values()):
                    missing.append(symbol)
                underlying = str(instrument.get("underlyingSymbol") or symbol)
            elif asset in ("EQUITY", "COLLECTIVE_INVESTMENT", "MUTUAL_FUND"):
                mult = 1.0
                row = {"delta": net, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
                underlying = symbol
            else:
                continue
            rows.append({"account": ref, "symbol": symbol, "underlying": underlying, "asset_type": asset,
                         "quantity": net, "multiplier": mult, **{g: _r(v, 6) for g, v in row.items()}})

    def total(group: list[dict]) -> dict:
        return {g: _r(sum(r[g] for r in group if r[g] is not None), 6) for g in ("delta", "gamma", "theta", "vega")}

    by_account: dict[str, list[dict]] = {}
    by_underlying: dict[str, list[dict]] = {}
    for r in rows:
        by_account.setdefault(r["account"], []).append(r)
        by_underlying.setdefault(r["underlying"], []).append(r)
    return {"positions": rows, "by_account": {k: total(v) for k, v in by_account.items()},
            "by_underlying": {k: total(v) for k, v in by_underlying.items()}, "total": total(rows),
            "missing_greeks": missing}


def exposure_and_pnl(accounts: Any, account_refs: Mapping[str, str] | None = None) -> dict:
    """16.17: weight per symbol = marketValue / sum of marketValue, day result = sum currentDayProfitLoss, open result =
    sum longOpenProfitLoss + shortOpenProfitLoss; per account and for all accounts together. Weight is None when the sum
    of market values is 0."""
    def summary(positions: list[Mapping]) -> dict:
        values: dict[str, float] = {}
        for p in positions:
            instrument = p.get("instrument") if isinstance(p.get("instrument"), Mapping) else {}
            symbol = str(instrument.get("symbol") or "")
            values[symbol] = values.get(symbol, 0.0) + (_num(p.get("marketValue")) or 0.0)
        whole = sum(values.values())
        return {"market_value": _r(whole, 4),
                "weights": [{"symbol": s, "market_value": _r(v, 4), "weight": _r(_ratio(v, whole))}
                            for s, v in values.items()],
                "day_pnl": _r(sum(_num(p.get("currentDayProfitLoss")) or 0.0 for p in positions), 4),
                "open_pnl": _r(sum((_num(p.get("longOpenProfitLoss")) or 0.0) + (_num(p.get("shortOpenProfitLoss")) or 0.0)
                                   for p in positions), 4)}

    by_account, every = {}, []
    for index, account in enumerate(_accounts(accounts)):
        positions = [p for p in account.get("positions") or [] if isinstance(p, Mapping)]
        by_account[_ref(account, index, account_refs)] = summary(positions)
        every += positions
    return {"by_account": by_account, "total": summary(every)}


# ---------------------------------------------------------------------------------------------- 16.18

def account_activity(transactions: Iterable[Mapping] = (), orders: Iterable[Mapping] = ()) -> dict:
    """16.18: over the range the caller read: sum of `netAmount` by `type`; fees = sum of `transferItems[].cost` of the
    items that carry a `feeType`, by `feeType`; and per order the average execution price = sum(price * quantity) /
    sum(quantity) over `orderActivityCollection[].executionLegs[]` (None with no executed quantity)."""
    net_by_type: dict[str, float] = {}
    fees: dict[str, float] = {}
    for tx in transactions:
        if not isinstance(tx, Mapping):
            continue
        kind = str(tx.get("type") or "UNKNOWN")
        net_by_type[kind] = net_by_type.get(kind, 0.0) + (_num(tx.get("netAmount")) or 0.0)
        for item in tx.get("transferItems") or []:
            fee = item.get("feeType") if isinstance(item, Mapping) else None
            if fee:
                fees[str(fee)] = fees.get(str(fee), 0.0) + (_num(item.get("cost")) or 0.0)
    prices = []
    for order in orders:
        if not isinstance(order, Mapping):
            continue
        qty = value = 0.0
        for activity in order.get("orderActivityCollection") or []:
            for leg in (activity.get("executionLegs") if isinstance(activity, Mapping) else None) or []:
                q, p = _num(leg.get("quantity")), _num(leg.get("price"))
                if q and p is not None:
                    qty += q
                    value += q * p
        prices.append({"order_id": order.get("orderId"), "filled_quantity": _r(qty),
                       "avg_execution_price": _r(_ratio(value, qty))})
    return {"net_by_type": {k: _r(v, 4) for k, v in net_by_type.items()},
            "fees_by_type": {k: _r(v, 4) for k, v in fees.items()}, "fees_total": _r(sum(fees.values()), 4),
            "orders": prices}
