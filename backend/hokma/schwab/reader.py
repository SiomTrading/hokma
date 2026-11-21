"""The read layer of the Schwab API, shared by the internal API (routes.py) and the agent tools (kit/hokma_tools.py)
(SPEC-015 5.10 and 5.11). One entry point, read(name, params), returns the envelope both of them send out:

    {"source": "Schwab", "as_of": ISO UTC, "delayed": bool (only where Schwab says it), "data": JSON}

It calls the blocking functions of endpoints.py (one per read endpoint) and the calculations of analytics.py, keeps a
cache per endpoint (quotes 2 s, chains 30 s, expiration chain and markets 15 min, instruments 1 day, movers 60 s,
accounts 15 s, orders and transactions 30 s, user preference 1 hour) and passes everything through accounts.public, so
no hash, account number, token or trimmed field of UserDetails and StreamerInfo ever leaves. Nothing here can place,
change or cancel an order: the order routes that write have no code anywhere in Hokma.

Call it from a thread: it blocks. SchwabError carries the catalog code; message_of gives its text in the active
language.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hokma.i18n import t
from hokma.market.messages import CONNECTION_CODES, connection_message
from hokma.market.tz import NEW_YORK
from hokma.schwab import accounts, analytics, auth, endpoints, models
from hokma.schwab.http import (BAD_REQUEST, FORBIDDEN_ROUTE, INVALID_PARAM, NOT_CONNECTED, NOT_FOUND, UPSTREAM,
                               SchwabError)

SOURCE = "Schwab"
CACHE_MAX_ENTRIES = 512
QUOTE_CHUNK = 100                           # option symbols per /quotes call of the portfolio greeks
ACTIVITY_DEFAULT_DAYS = 30
HISTORY_DEFAULT_WINDOWS = (10, 20, 30, 60)
HISTORY_WINDOW_MAX = 400
MARKET_LOOKAHEAD_DAYS = 5

_clock: Callable[[], float] = time.monotonic
_lock = threading.Lock()
_cache: dict[str, tuple[float, str, Any]] = {}


def set_clock(clock: Callable[[], float] | None = None) -> None:
    """Tests only: the clock of the cache (seconds). None restores the real one."""
    global _clock
    _clock = clock or time.monotonic


def clear_cache() -> None:
    with _lock:
        _cache.clear()


# ------------------------------------------------------------------------------------------------------- errors


def _bad(detail: str) -> SchwabError:
    return SchwabError(INVALID_PARAM, 422, detail=detail)


_INVALID = re.compile(r"^(\S+) must be (.+?)\.?$")


REASON_CODES = (BAD_REQUEST, NOT_FOUND, UPSTREAM)      # a refusal or a failure of the call: the sign-in states keep their own text
REASON_ITEMS = 5                            # errors[] entries that reach the text
REASON_FIELD = 300                          # characters kept of each field of an entry
REASON_TOTAL = 900
_REASON_KEYS = ("title", "detail")
# C0 and C1 controls (NEL included), DEL and the two Unicode line separators: none of them may reach a message, a tool answer or a route
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
# What a word of the reason must not look like (and, exact, the account numbers and hashes of the table: see _hide_exact) (a careless or hostile answer may echo what it should not): an account number or
# any id of 7 digits or more, a hash (16 hex or more), a word that ends in a number of 4 digits or more (customer, login, client
# and user ids), an address with @, or a secret or a token with a mark of its own. Such a word is hidden, not the sentence.
_OCC_SYMBOL = re.compile(r"[A-Z0-9]{0,6}\d{6}[CP]\d{8}")        # an option symbol is not an id
_SECRET_SHAPES = re.compile(r"\d{7,}|[0-9A-Fa-f]{16,}|[A-Za-z][-_.]?\d{4,}|@|(?i:[-_.](?:secret|token)\b|\b(?:secret|token)[-_.]\d)")


_LONG_WORD = re.compile(r"[A-Za-z0-9]{20,}")             # any alphanumeric word of 20 characters or more, whatever it holds
_INPUT_MAX = 5000                                         # read at most this much of a field before the exact redaction
_HIDDEN = "[hidden]"
_SOURCE_KEYS = ("parameter", "header")


def _unquote(text: str) -> str:
    """The text with its %XX escapes decoded (up to three layers, for a value encoded more than once)."""
    for _ in range(3):
        decoded = urllib.parse.unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def _alnum(text: str) -> tuple[str, list[int]]:
    """The letters and digits of the text in lower case, and the position of each one in the text."""
    chars, where = [], []
    for position, char in enumerate(text):
        if char.isalnum():
            chars.append(char.lower())
            where.append(position)
    return "".join(chars), where


def _hide_exact(text: str) -> str:
    """Hides every account number and hash of the table wherever it shows: any case, with separators between its characters
    (hyphens, spaces, dots, slashes) or inside a longer word."""
    values = {_alnum(v)[0] for v in accounts.sensitive_values()}
    values.discard("")
    if not values:
        return text
    flat, where = _alnum(text)
    spans: list[tuple[int, int]] = []
    for value in values:
        start = flat.find(value)
        while start != -1:
            spans.append((where[start], where[start + len(value) - 1] + 1))
            start = flat.find(value, start + 1)
    if not spans:
        return text
    spans.sort()
    out, cursor = [], 0
    for begin, end in spans:
        if end <= cursor:
            continue
        out.append(text[cursor:max(begin, cursor)])
        out.append(_HIDDEN)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _hide_word(word: str) -> str:
    if _OCC_SYMBOL.fullmatch(word.strip(".,;:()")):
        return word
    return _HIDDEN if _SECRET_SHAPES.search(word) or _LONG_WORD.search(word) else word


def _redact(text: str) -> str:
    # controls are removed before AND after the decoding: %0A, %0D, %00, %1B or %E2%80%A8 become controls only after it
    text = _hide_exact(_CONTROL.sub(" ", _unquote(_CONTROL.sub(" ", text[:_INPUT_MAX]))))
    return re.sub(r"\S+", lambda m: _hide_word(m.group(0)), text)


def _field(value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return _redact(str(value)).strip()[:REASON_FIELD]


def _field_name(text: str) -> str:
    """The name of the field a source points to. A pointer or a path keeps only its last segment that is not sensitive (an
    account number or hash segment, or the one after /accounts/, never shows): '/accounts/<hash>/orders' gives 'orders'."""
    segments = [s for s in _unquote(text).split("/") if s.strip()]
    if len(segments) <= 1 and "/" not in text:
        return _field(text)
    safe = [s for i, s in enumerate(segments)
            if _redact(s) == s.strip() and not (i and segments[i - 1].lower() == "accounts" and re.search(r"\d", s))]
    return _field(safe[-1]) if safe else ""


def _source_names(source: Any) -> list[str]:
    """The source of an error entry reduced to field names: a text, a list of texts, or the 'parameter' and 'header' of an
    object. Anything else is left out, never dumped."""
    if isinstance(source, str):
        return [name for name in (_field_name(source),) if name]
    if isinstance(source, dict):
        return [name for k in _SOURCE_KEYS if isinstance(source.get(k), str) for name in (_field_name(source[k]),) if name]
    if isinstance(source, (list, tuple)):
        names: list[str] = []
        for item in source:
            names += _source_names(item) if isinstance(item, (str, dict)) else []
        return names
    return []


def reason_of(error: SchwabError) -> str:
    """Schwab's own reason for a refusal: the `errors[]` that http.py kept (id, status, title, detail, source; they carry
    no secret) on one line, cut to a safe size. Empty when Schwab sent none."""
    parts = []
    for item in (error.errors or [])[:REASON_ITEMS]:
        if not isinstance(item, dict):
            continue
        text = ": ".join(_field(item[k]) for k in _REASON_KEYS if item.get(k) not in (None, ""))
        shown = [(k, _field(item[k])) for k in ("id", "status") if item.get(k) not in (None, "")]
        names = _source_names(item.get("source"))
        if names:
            shown.append(("source", ", ".join(names)))
        extra = ", ".join(f"{k} {v}" for k, v in shown if v)
        line = f"{text} ({extra})" if text and extra else text or extra
        if line:
            parts.append(line)
    return "; ".join(parts)[:REASON_TOTAL]


def message_of(error: SchwabError) -> str:
    """The catalog text of a SchwabError in the active language, plus the reason Schwab gave when it gave one (C4: the agent
    and the routes get the reason of a rejection, not only the generic text)."""
    base = _catalog_text(error)
    reason = reason_of(error) if error.code in REASON_CODES else ""
    return t("schwab.err.reason", base=base, reason=reason) if reason else base


def _catalog_text(error: SchwabError) -> str:
    """schwab.err.* codes use their own text; the connections.* codes of http.py use the Connections text; a refused
    parameter says which and why."""
    code = error.code
    if code == NOT_CONNECTED:
        return t("schwab.err.not_connected")
    if code == BAD_REQUEST:
        return t("schwab.err.bad_request")
    if code == NOT_FOUND:
        return t("schwab.err.not_found")
    if code == UPSTREAM:
        return t("schwab.err.upstream")
    if code == FORBIDDEN_ROUTE:
        return t("schwab.err.forbidden_route")
    if code == INVALID_PARAM:
        found = _INVALID.match(error.detail or "")
        if found:
            return t("schwab.err.invalid_param", name=found.group(1), rule=found.group(2))
        base = t("schwab.err.bad_request")
        return f"{base} ({error.detail})" if error.detail else base
    if code in CONNECTION_CODES:
        return connection_message(code)
    return t("schwab.err.upstream")


def status_of(error: SchwabError) -> int:
    return 409 if error.code == NOT_CONNECTED else error.status


def not_connected_text() -> str:
    return t("schwab.err.not_connected")


# ----------------------------------------------------------------------------------------------- parameters


def coerce(name: str, kind: str, value: Any) -> Any:
    """A parameter as its kind: 'str', 'int', 'num', 'bool', 'id' (a whole number kept as text), 'list' (list of texts)
    or 'ints'. Text from a query string is converted; a typed value passes. SchwabError INVALID_PARAM otherwise."""
    if kind == "str":
        if not isinstance(value, str):
            raise _bad(f"{name} must be text")
        return value
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise _bad(f"{name} must be true or false")
    if kind == "int":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,18}", value.strip()):
            return int(value)
        raise _bad(f"{name} must be an integer")
    if kind == "num":
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return value
        if isinstance(value, str):
            try:
                number = float(value)
            except ValueError:
                number = math.nan
            if math.isfinite(number):
                return number
        raise _bad(f"{name} must be a number")
    if kind == "id":
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value.strip()):
            return value.strip()
        raise _bad(f"{name} must be a number")
    if kind in ("list", "ints"):
        if isinstance(value, str):
            items: list = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, (list, tuple)):
            items = list(value)
        else:
            raise _bad(f"{name} must be a list")
        if kind == "ints":
            return [coerce(name, "int", item) for item in items]
        if not all(isinstance(item, str) for item in items):
            raise _bad(f"{name} must be a list of texts")
        return items
    raise ValueError(kind)


# ------------------------------------------------------------------------------------------------------ cache


@dataclass(frozen=True)
class Spec:
    fn: Callable[..., Any]
    ttl: float                                    # seconds; 0 = not cached
    params: dict[str, str]                        # name -> kind (see coerce)
    required: tuple[str, ...] = ()


def _key(name: str, params: dict) -> str:
    return name + "|" + json.dumps(params, sort_keys=True, default=str)


def _stamp() -> str:
    return endpoints._format_time(endpoints._now())


def fetch(name: str, params: dict | None = None) -> tuple[Any, str]:
    """The raw result (objects of models, or JSON for the calculations) and the instant it was read, through the cache of
    the endpoint. Raw results are shared: never change them."""
    spec = SPECS[name]
    params = dict(params or {})
    key = _key(name, params)
    now = _clock()
    if spec.ttl > 0:
        with _lock:
            hit = _cache.get(key)
            if hit is not None and hit[0] > now:
                return hit[2], hit[1]
    result = spec.fn(**params)
    stamp = _stamp()
    if spec.ttl > 0:
        with _lock:
            if len(_cache) >= CACHE_MAX_ENTRIES:
                for stale in [k for k, v in _cache.items() if v[0] <= now]:
                    del _cache[stale]
                while len(_cache) >= CACHE_MAX_ENTRIES:
                    del _cache[next(iter(_cache))]
            _cache[key] = (now + spec.ttl, stamp, result)
    return result, stamp


def _delayed(name: str, data: Any) -> bool | None:
    if name == "chains" and isinstance(data, dict):
        flag = data.get("isDelayed")
        return flag if isinstance(flag, bool) else None
    if name in ("quotes", "quote") and isinstance(data, dict):
        nodes = [data] if name == "quote" else [v for v in data.values() if isinstance(v, dict)]
        flags = [n["realtime"] for n in nodes if isinstance(n.get("realtime"), bool)]
        return (not all(flags)) if flags else None
    return None


def read(name: str, params: dict | None = None) -> dict:
    """One read: checks the parameters (names, kinds, required ones) and the session, reads through the cache and returns
    the envelope. SchwabError INVALID_PARAM for a bad parameter, NOT_CONNECTED without a session."""
    spec = SPECS.get(name)
    if spec is None:
        raise _bad(f"{name} must be a known read")
    if not auth.is_connected():
        raise SchwabError(NOT_CONNECTED, 409)
    given = {k: v for k, v in (params or {}).items() if v is not None}
    unknown = sorted(set(given) - set(spec.params))
    if unknown:
        raise _bad(f"{unknown[0]} must be a parameter of this read")
    missing = [k for k in spec.required if k not in given]
    if missing:
        raise _bad(f"{missing[0]} must be given")
    clean = {k: coerce(k, spec.params[k], v) for k, v in given.items()}
    result, stamp = fetch(name, clean)
    data = accounts.public(result)
    delayed = data.pop("_delayed", None) if isinstance(data, dict) and "_delayed" in data else _delayed(name, data)
    out: dict[str, Any] = {"source": SOURCE, "as_of": stamp}
    if delayed is not None:
        out["delayed"] = delayed
    out["data"] = data
    return out


# -------------------------------------------------------------------------------- endpoints that need a wrapper


def _account_list() -> list[dict]:
    return accounts.listing()


def _markets(markets: Any, date: Any = None):
    """One market as text goes to /markets/{market_id}, several to /markets."""
    if isinstance(markets, list):
        markets = markets[0] if len(markets) == 1 else ",".join(markets)
    return endpoints.market_hours(markets, date)


def _all_types() -> str:
    return ",".join(models.enum_values("TransactionType"))


def _transactions(account_ref: str, types: Any = None, startDate: Any = None, endDate: Any = None,  # noqa: N803
                  symbol: str | None = None):
    """Schwab requires types; without it every type is asked for."""
    return endpoints.transactions(account_ref, types if types is not None else _all_types(), startDate, endDate, symbol)


# -------------------------------------------------------------------------------------------------- composites


def _now_dt() -> dt.datetime:
    return endpoints._now()


def _epoch() -> float:
    return _now_dt().timestamp()


def _fetch_json(name: str, params: dict) -> Any:
    return models.dump(fetch(name, params)[0])


def volatility_history(symbol: str, windows: Any = None) -> dict:
    """16.12: historical volatility of the daily closes for each window (10, 20, 30 and 60 by default), from
    /pricehistory only."""
    text = endpoints._symbol("symbol", symbol)
    chosen = coerce("windows", "ints", windows) if windows is not None else list(HISTORY_DEFAULT_WINDOWS)
    if not chosen or any(n < 3 or n > HISTORY_WINDOW_MAX for n in chosen):
        raise _bad(f"windows must be whole numbers from 3 to {HISTORY_WINDOW_MAX}")
    years = 1 if max(chosen) <= 240 else 2
    history = _fetch_json("pricehistory", {"symbol": text, "periodType": "year", "period": years,
                                           "frequencyType": "daily", "frequency": 1})
    candles = (history.get("candles") or []) if isinstance(history, dict) else []
    return {"symbol": text, "windows": chosen, "candles": len(candles), **analytics.volatility_history(candles, chosen)}


def _positions(account_ref: str | None) -> tuple[list[dict], dict[str, str]]:
    """The accounts with positions as JSON (the real numbers stay inside) and the number to account_ref table."""
    params: dict[str, Any] = {"fields": "positions"}
    if account_ref:
        params["account_ref"] = account_ref
    listed = models.dump(fetch("accounts", params)[0])
    listed = listed if isinstance(listed, list) else [listed]
    refs: dict[str, str] = {}
    for item in listed:
        number = ((item or {}).get("securitiesAccount") or {}).get("accountNumber")
        ref = accounts.identity(number)["account_ref"] if number is not None else None
        if ref:
            refs[str(number)] = ref
    return listed, refs


def portfolio_greeks(account_ref: str | None = None) -> dict:
    """16.16: greeks of the positions in share equivalents, from /accounts?fields=positions and the /quotes of the
    option symbols."""
    listed, refs = _positions(account_ref)
    symbols: list[str] = []
    for item in listed:
        for position in ((item or {}).get("securitiesAccount") or {}).get("positions") or []:
            instrument = position.get("instrument") or {}
            if instrument.get("assetType") == "OPTION" and instrument.get("symbol") not in symbols:
                symbols.append(instrument["symbol"])
    quotes: dict = {}
    for start in range(0, len(symbols), QUOTE_CHUNK):
        part = _fetch_json("quotes", {"symbols": symbols[start:start + QUOTE_CHUNK]})
        quotes.update({k: v for k, v in part.items() if k != "errors"} if isinstance(part, dict) else {})
    return analytics.portfolio_greeks(listed, quotes, refs)


def exposure_and_pnl(account_ref: str | None = None) -> dict:
    """16.17: weight per symbol, day result and open result, per account and in total."""
    listed, refs = _positions(account_ref)
    return analytics.exposure_and_pnl(listed, refs)


def _instant(name: str, value: Any, end: bool) -> Any:
    """A date yyyy-MM-dd as the start or the end of that day (UTC); an instant passes as it is."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        endpoints._ymd(name, value.strip())
        return value.strip() + ("T23:59:59.000Z" if end else "T00:00:00.000Z")
    return value


def account_activity(account_ref: str | None = None, startDate: Any = None, endDate: Any = None) -> dict:  # noqa: N803
    """16.18: transactions and orders of the range (the last 30 days by default) of one account, or of every account,
    with the net amount by type, the fees by type, the average execution price per order, and the exposure and result of
    the positions (16.17)."""
    end_at = endpoints._parse_time("endDate", _instant("endDate", endDate, True)) if endDate is not None else _now_dt()
    start_at = (endpoints._parse_time("startDate", _instant("startDate", startDate, False)) if startDate is not None
                else end_at - dt.timedelta(days=ACTIVITY_DEFAULT_DAYS))
    start, end = endpoints._format_time(start_at), endpoints._format_time(end_at)
    refs = [account_ref] if account_ref else [item["account_ref"] for item in accounts.listing()]
    transactions: list[dict] = []
    orders: list[dict] = []
    for ref in refs:
        transactions += models.dump(fetch("transactions", {"account_ref": ref, "startDate": start, "endDate": end})[0])
        orders += models.dump(fetch("orders", {"account_ref": ref, "fromEnteredTime": start, "toEnteredTime": end})[0])
    out = analytics.account_activity(transactions, orders)
    return {"from": start, "to": end, "accounts": refs, "transactions": len(transactions), "orders_count": len(orders),
            **out, "exposure": exposure_and_pnl(account_ref)}


def market_hours(market: str = "equity", product: str = "EQ", date: Any = None) -> dict:
    """16.15: is the regular session open and when does it change, from /markets (holidays included). Reads the day
    and the next ones until an opening shows."""
    chosen = endpoints._choice("market", market, "Market")
    if not isinstance(product, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", product):
        raise _bad("product must be a short code")
    now = _epoch()
    first = _now_dt().astimezone(NEW_YORK).date() if date is None else dt.date.fromisoformat(endpoints._ymd("date", date))
    payloads: list = []
    out: dict = {}
    for step in range(MARKET_LOOKAHEAD_DAYS):
        payloads.append(_fetch_json("markets", {"markets": chosen, "date": (first + dt.timedelta(days=step)).isoformat()}))
        out = analytics.market_hours(payloads, now, chosen, product)
        if out["next_change"] is not None:
            break
    return out


# ------------------------------------------------------------------------------------------------------ table

_CHAIN = {"symbol": "str", "contractType": "str", "strikeCount": "int", "includeUnderlyingQuote": "bool",
          "strategy": "str", "interval": "num", "strike": "num", "range": "str", "fromDate": "str", "toDate": "str",
          "volatility": "num", "underlyingPrice": "num", "interestRate": "num", "daysToExpiration": "int",
          "expMonth": "str", "optionType": "str", "entitlement": "str"}
_HISTORY = {"symbol": "str", "periodType": "str", "period": "int", "frequencyType": "str", "frequency": "int",
            "startDate": "int", "endDate": "int", "needExtendedHoursData": "bool", "needPreviousClose": "bool"}
_MIN, _HOUR, _DAY = 60.0, 3600.0, 86400.0

SPECS: dict[str, Spec] = {
    # Market Data (REF 2)
    "quotes": Spec(endpoints.quotes, 2, {"symbols": "list", "fields": "str", "indicative": "bool"}, ("symbols",)),
    "quote": Spec(endpoints.quote, 2, {"symbol": "str", "fields": "str"}, ("symbol",)),
    "chains": Spec(endpoints.option_chain, 30, _CHAIN, ("symbol",)),
    "expirationchain": Spec(endpoints.expirations, 15 * _MIN, {"symbol": "str"}, ("symbol",)),
    "pricehistory": Spec(endpoints.price_history, 0, _HISTORY, ("symbol",)),
    "movers": Spec(endpoints.movers, 60, {"index": "str", "sort": "str", "frequency": "int"}, ("index",)),
    "markets": Spec(_markets, 15 * _MIN, {"markets": "list", "date": "str"}, ("markets",)),
    "instruments": Spec(endpoints.instruments, _DAY, {"symbol": "str", "projection": "str"}, ("symbol", "projection")),
    "instrument": Spec(endpoints.instrument_by_cusip, _DAY, {"cusip": "str"}, ("cusip",)),
    # Accounts and Trading (REF 3), read only
    "account_list": Spec(_account_list, 0, {}),
    "accounts": Spec(endpoints.accounts, 15, {"account_ref": "str", "fields": "str"}),
    "orders": Spec(endpoints.orders, 30, {"account_ref": "str", "fromEnteredTime": "str", "toEnteredTime": "str",
                                          "status": "str", "maxResults": "int"}),
    "order": Spec(endpoints.order, 30, {"account_ref": "str", "order_id": "id"}, ("account_ref", "order_id")),
    "transactions": Spec(_transactions, 30, {"account_ref": "str", "types": "list", "startDate": "str",
                                             "endDate": "str", "symbol": "str"}, ("account_ref",)),
    "transaction": Spec(endpoints.transaction, 30, {"account_ref": "str", "transaction_id": "id"},
                        ("account_ref", "transaction_id")),
    "user_preference": Spec(endpoints.user_preference, _HOUR, {}),
    # Calculations (16)
    "volatility_history": Spec(volatility_history, 0, {"symbol": "str", "windows": "ints"}, ("symbol",)),
    "portfolio_greeks": Spec(portfolio_greeks, 0, {"account_ref": "str"}),
    "exposure_and_pnl": Spec(exposure_and_pnl, 0, {"account_ref": "str"}),
    "account_activity": Spec(account_activity, 0, {"account_ref": "str", "startDate": "str", "endDate": "str"}),
    "market_hours": Spec(market_hours, 0, {"market": "str", "product": "str", "date": "str"}),
}

ENDPOINT_READS = ("quotes", "quote", "chains", "expirationchain", "pricehistory", "movers", "markets", "instruments",
                  "instrument", "account_list", "accounts", "orders", "order", "transactions", "transaction",
                  "user_preference")
ANALYTICS_READS = tuple(name for name in SPECS if name not in ENDPOINT_READS)
