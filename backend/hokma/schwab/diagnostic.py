"""The measuring tool of SPEC-016 T1 and T13: one run reads the raw answers of Schwab for a symbol (daily and 5 minute candles with every
session, one option expiration, the hours of the markets) and listens to the Streamer for a while, then saves what it saw in a file
and answers a summary of the open points of section 2.2 of the spec.

It is read only: market data, never an order. Nothing secret reaches the file: the token, the four ids of the session and the account
numbers are not read here, the raw messages are saved through `stream.sanitize`, and before saving the text is checked against the
token and the ids, and the file is not written if one is in it.
"""
import asyncio
import datetime as dt
import json
import statistics
import time
from pathlib import Path

from hokma.core.config import hokma_data_dir
from hokma.market.providers import get_catalog
from hokma.schwab import endpoints, http, models, stream
from hokma.schwab.http import SchwabError

MAX_RAW = 2500
OFFER_KEYS = ("level2Permissions", "mktDataPermission")
FORBIDDEN_NAMES = ("accounts", "accountNumber", "hashValue", "displayAcctId", "streamerInfo", "streamerSocketUrl", "SchwabClient", "Authorization")
MAX_SECONDS = 300.0
MIN_SECONDS = 5.0
MARKETS = ("equity", "option", "future")
QUARTER_MONTHS = ((3, "H"), (6, "M"), (9, "U"), (12, "Z"))


def front_contract(root: str, today: dt.date) -> str:
    """The next quarterly contract, for example `/NQZ26`. Only a guess to ask the Streamer with; it decides nothing in Hokma."""
    for month, code in QUARTER_MONTHS:
        if today.month <= month:
            return f"/{root}{code}{today.year % 100:02d}"
    return f"/{root}H{(today.year + 1) % 100:02d}"


def _safe(call, *args, **kwargs):
    """The answer of a read as plain JSON, or {"error": code} when Schwab refused or was not reachable."""
    try:
        return models.dump(call(*args, **kwargs))
    except SchwabError as error:
        return {"error": error.code}
    except Exception as error:                      # noqa: BLE001  (one read that fails never stops the rest)
        return {"error": type(error).__name__}


def _chain_rows(chain: dict) -> tuple[float | None, list[dict]]:
    """The strikes of a chain as compact rows, and the underlying price. The shape is read with care: nothing is assumed."""
    rows: list[dict] = []
    for side, key in (("CALL", "callExpDateMap"), ("PUT", "putExpDateMap")):
        for _expiry, strikes in (chain.get(key) or {}).items():
            for strike, contracts in (strikes or {}).items():
                for contract in contracts if isinstance(contracts, list) else []:
                    rows.append({"side": side, "strike": strike, "symbol": contract.get("symbol"), "bid": contract.get("bid"),
                                 "ask": contract.get("ask"), "mark": contract.get("mark"), "last": contract.get("last")})
    underlying = chain.get("underlyingPrice")
    return (underlying if isinstance(underlying, (int, float)) else None), rows


def collect_rest(symbol: str, today: dt.date) -> dict:
    """Blocking (a thread): the raw answers of Schwab that T1 compares with what Hokma showed and with ToS."""
    now = int(time.time() * 1000)
    day = 86400 * 1000
    out: dict = {"symbol": symbol}
    out["daily"] = _safe(endpoints.price_history, symbol, periodType="year", frequencyType="daily", frequency=1,
                         startDate=now - 14 * day, endDate=now, needExtendedHoursData=True)
    out["five_minutes"] = _safe(endpoints.price_history, symbol, frequencyType="minute", frequency=5,
                                startDate=now - 3 * day, endDate=now, needExtendedHoursData=True)
    out["quote_rest"] = _safe(endpoints.quotes, [symbol], fields="quote,reference")
    out["markets"] = {m: _safe(endpoints.market_hours, m) for m in MARKETS}
    out["expirations"] = _safe(endpoints.expirations, symbol)
    expiries = [str(item.get("expirationDate") or item.get("expiration")) for item in (out["expirations"].get("expirationList") or [])
                if isinstance(item, dict)] if isinstance(out["expirations"], dict) else []
    first = next((e for e in expiries if e and e >= today.isoformat()), None)
    out["chain_expiration"] = first
    if first:
        chain = _safe(endpoints.option_chain, symbol, contractType="ALL", fromDate=first, toDate=first, includeUnderlyingQuote=True)
        underlying, rows = _chain_rows(chain if isinstance(chain, dict) else {})
        out["chain"] = {"underlying_price": underlying, "strike_count": len({r["strike"] for r in rows}), "rows": rows,
                        "error": chain.get("error") if isinstance(chain, dict) else None}
    # Only the permissions of the user preferences are kept: never the whole answer (it has account numbers and the ids of the Streamer).
    preference = _safe(endpoints.user_preference)
    offers = preference.get("offers") if isinstance(preference, dict) else None
    out["offers"] = [{k: v for k, v in offer.items() if k in OFFER_KEYS} for offer in offers if isinstance(offer, dict)] if isinstance(offers, list) else {}
    return out


def sensitive_values() -> list[str]:
    """Blocking (a thread): what must never be in the file: the account numbers, their hashes and the display ids. Held in memory for the check only."""
    found: list[str] = []
    preference = _safe(endpoints.user_preference)
    for account in (preference.get("accounts") or []) if isinstance(preference, dict) else []:
        if isinstance(account, dict):
            found += [str(account[k]) for k in ("accountNumber", "displayAcctId") if account.get(k)]
    numbers = _safe(endpoints.account_numbers)
    for item in numbers if isinstance(numbers, list) else []:
        if isinstance(item, dict):
            found += [str(item[k]) for k in ("accountNumber", "hashValue") if item.get(k)]
    return [v for v in found if len(v) >= 4]


def _option_keys(rest: dict, count: int) -> list[str]:
    rows = (rest.get("chain") or {}).get("rows") or []
    price = (rest.get("chain") or {}).get("underlying_price")
    calls = [r for r in rows if r["side"] == "CALL" and isinstance(r.get("symbol"), str)]
    if price is not None:
        calls.sort(key=lambda r: abs(float(r["strike"]) - price))
    return [r["symbol"] for r in calls[:count]]


def _pad_option(symbol: str) -> str:
    """The key the guide describes: the root padded with spaces to 6 characters (21 in all)."""
    if len(symbol) == 21 or len(symbol) < 15:
        return symbol
    root, rest = symbol[:-15].strip(), symbol[-15:]
    return root.ljust(6) + rest


def analyse(raw: list[dict], stats: dict, rest: dict, symbol: str, option_keys: list[str]) -> dict:
    """The seven open points of section 2.2, answered from what was captured. A point without data says so."""
    data = [(b.get("service"), c) for m in raw for b in (m.get("data") or []) if isinstance(b, dict)
            for c in (b.get("content") or []) if isinstance(c, dict)]
    answers = {(r.get("service"), r.get("command"), (r.get("content") or {}).get("code")) for m in raw for r in (m.get("response") or [])}
    gaps = stats.get("heartbeat_gaps_ms") or []
    chart_times: dict = {}
    for service, content in data:
        if service == stream.CHART_EQUITY and content.get("key") == symbol:
            chart_times.setdefault(content.get("7"), []).append((content.get("4"), content.get("5")))
    in_progress = any(len(set(values)) > 1 for values in chart_times.values())
    delayed = {}
    for service, content in data:
        delayed.setdefault(service, set()).add(bool(content.get("delayed")))
    option_echo = {c.get("key") for s, c in data if s == stream.LEVELONE_OPTIONS}
    return {
        "heartbeat_interval_ms": {"samples": len(gaps), "median": int(statistics.median(gaps)) if gaps else None,
                                  "min": min(gaps) if gaps else None, "max": max(gaps) if gaps else None},
        "chart_equity": {"minutes_seen": len(chart_times), "mode": "in_progress" if in_progress else
                         ("one_message_per_minute" if chart_times else "no_data")},
        "delayed_by_service": {service: sorted(values) for service, values in delayed.items()},
        "answers_seen": sorted(f"{s} {c} code={code}" for s, c, code in answers),
        "option_keys_asked": option_keys, "option_keys_answered": sorted(k for k in option_echo if k),
        "futures_data": {s: sum(1 for x, _ in data if x == s) for s in (stream.LEVELONE_FUTURES, stream.CHART_FUTURES)},
        "not_measured": ["max symbols (code 19): needs a list of valid symbols, run with probe_symbols", "token expiry on an open session: needs a run of 30 minutes or more"],
    }


class DiagnosticBusy(RuntimeError):
    """A run is already going: two at once would record each other's subscriptions."""


_running = False


def leaks(text: str, secrets: list[str]) -> str | None:
    """What the file must not carry, or None. The values come from the session (token, ids) and from the accounts (numbers, hashes); the NAMES of the
    fields of an account or of the Streamer's session may not appear either, whatever their value."""
    for secret in secrets:
        if secret and secret in text:
            return "a secret value"
    for name in FORBIDDEN_NAMES:
        if f'"{name}' in text:
            return "the field " + name
    return None


async def run(symbol: str = "QQQ", seconds: float = 75.0, option_count: int = 2, probe_symbols: list[str] | None = None, hub=None) -> dict:
    """One measuring run, from the Streamer session THE HUB ALREADY HAS (Schwab allows one connection per user: a second would get code 12 and cut the
    charts). It adds a recorder to that session and asks, through it, for the keys it needs; what it added it takes away. Without a session in the hub it
    asks the hub to open the only one. Returns the summary and the path of the file."""
    global _running
    if _running:
        raise DiagnosticBusy()
    _running = True
    try:
        return await _run(symbol, seconds, option_count, probe_symbols, hub)
    finally:
        _running = False


async def _run(symbol: str, seconds: float, option_count: int, probe_symbols: list[str] | None, hub) -> dict:
    if hub is None:
        from hokma.connections import runtime

        hub = await runtime.prepare()
    source = await hub.provider_source("schwab")
    if source is None or source.streamer is None:
        raise SchwabError(http.NOT_CONNECTED, 409)
    seconds = max(MIN_SECONDS, min(float(seconds), MAX_SECONDS))
    today = dt.datetime.now(dt.timezone.utc).date()
    rest = await asyncio.to_thread(collect_rest, symbol, today)
    sensitive = await asyncio.to_thread(sensitive_values)
    options = _option_keys(rest, option_count)
    asked_options = options + [_pad_option(k) for k in options if _pad_option(k) != k]
    futures = ["/NQ", front_contract("NQ", today)]
    raw: list[dict] = []

    def tap(message: dict) -> None:
        if len(raw) < MAX_RAW:
            raw.append(message)

    streamer = source.streamer
    heartbeats_before = len(streamer.stats.get("heartbeat_gaps_ms", []))
    streamer.add_tap(tap)
    plan = {stream.LEVELONE_EQUITIES: [symbol], stream.CHART_EQUITY: [symbol], stream.LEVELONE_FUTURES: futures, stream.CHART_FUTURES: futures}
    if asked_options:
        plan[stream.LEVELONE_OPTIONS] = asked_options
    if probe_symbols:
        plan[stream.LEVELONE_EQUITIES] = [symbol, *probe_symbols]
    added = source.diagnostic_want(plan)
    try:
        await asyncio.sleep(seconds)
        stats = {**streamer.stats, "heartbeat_gaps_ms": list(streamer.stats.get("heartbeat_gaps_ms", []))[heartbeats_before:]}
        secrets = [await asyncio.to_thread(http._token, False), *[v for k, v in streamer._ids.items() if k != "url" and v], *sensitive]
    finally:
        source.diagnostic_drop(added)
        streamer.remove_tap(tap)
    summary = analyse(raw, stats, rest, symbol, asked_options)
    document = {"symbol": symbol, "seconds": seconds, "rest": rest, "summary": summary, "raw_messages": raw,
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    text = json.dumps(document, indent=1, default=str)
    found = leaks(text, secrets)
    if found:
        raise RuntimeError(f"the diagnostic would have saved {found}: nothing was written")
    folder = Path(hokma_data_dir()) / "diagnostics"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"schwab-streaming-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(text, encoding="utf-8")
    return {"ok": True, "file": str(path), "messages": len(raw), "summary": summary}
