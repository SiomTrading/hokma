"""Tools that expose the data module to agents: bars, snapshot, fundamentals, filings, macro, universe and a filings watch
(SPEC-017, B9).

Each tool calls `hokma.data`, which reads prices only through `hokma.market` and returns the value with its source, the time
it was received and the cut-off it respects. A missing value is an error with a class (`no_data`, `unavailable`, `stale`,
`bad_input`) and suggestions; a gap is never filled. Answers are scrubbed of keys and account ids, and lists are capped.
Only Hokma's own tools are published here: the tools, skills and agents that the filings library brings are not exposed.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from hokma.kit.tool_registry import ToolError, ToolSet

TOOLSET = ToolSet()
MAX_ROWS = 2_000                 # rows of a list in one answer
MAX_SYMBOLS = 20
MAX_FORMS = 10
MAX_SEEN = 1_000
RESERVED_FILTERS = frozenset({"directory", "base_url", "cache_dir", "clock", "path", "url", "file", "folder"})  # where to read or what time it is: never an agent choice
MAX_BAR_DAYS = 3_660             # ten years of calendar days in one request
INTERVALS = ("1m", "5m", "15m", "1h", "1D", "1d")
STATUS_CODE = {"NO_DATA": "no_data", "DATA_UNAVAILABLE": "unavailable", "STALE": "stale"}
ACCOUNT_ID = re.compile(r"\b(?:account|acct)[ _-]?(?:id|number|no)?\s*[:=]\s*\S+", re.I)


def _s(desc: str = "", **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def _texts(desc: str = "") -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


def _when(value: Any, field: str):
    from hokma.data.sentinels import parse_time

    try:
        return parse_time(value)
    except ValueError as exc:
        raise ToolError("bad_input", f"{field}: {exc}", ["Use an ISO date such as 2026-01-31 or 2026-01-31T15:00:00Z"]) from None


def _symbol(value: Any) -> str:
    from hokma.desks.core.safety import safe_ticker_component

    try:
        return safe_ticker_component(str(value).strip().upper())
    except ValueError as exc:
        raise ToolError("bad_input", f"symbol: {exc}", ["Examples: AAPL, NQ=F, BTC-USD"]) from None


def _clean(value: Any) -> Any:
    """Cap the lists and take account identifiers out of every text."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_clean(v) for v in value[:MAX_ROWS]]
        return items
    if isinstance(value, str):
        return ACCOUNT_ID.sub("[account removed]", value)
    return value


def _answer(result: Any) -> str:
    payload = result.to_dict()
    if isinstance(payload.get("value"), list) and len(payload["value"]) > MAX_ROWS:
        payload["warnings"] = list(payload.get("warnings", [])) + [f"list cut to the first {MAX_ROWS} rows"]
    if payload["status"] != "ok":
        code = STATUS_CODE.get(payload["status"], "unavailable")
        suggestions = {"no_data": ["Try another date range or another symbol"],
                       "unavailable": ["Check the connection in Connections and try again in a minute"],
                       "stale": ["Ask for an older as_of or wait for fresh data"]}[code]
        raise ToolError(code, f"{payload['source']}: {payload['reason']}", suggestions)
    return json.dumps(_clean(payload), ensure_ascii=False, default=str)


@TOOLSET.tool("data_bars", "OHLCV bars of a symbol from the connected market sources, closed bars only, cut at as_of. Returns the source and the time the data was received.", {
    "symbol": _s("Symbol."), "interval": _s("Bar size.", enum=list(INTERVALS)), "start": _s("ISO start."), "end": _s("ISO end."),
    "as_of": _s("ISO cut-off; nothing after it is used. Defaults to end.")}, ["symbol", "interval", "start", "end"])
def data_bars(u, c, a: dict) -> str:
    import hokma.data as data

    if a["interval"] not in INTERVALS:
        raise ToolError("bad_input", f"interval must be one of {', '.join(INTERVALS)}")
    start, end = _when(a["start"], "start"), _when(a["end"], "end")
    if end < start:
        raise ToolError("bad_input", "end is before start")
    if end - start > timedelta(days=MAX_BAR_DAYS):
        raise ToolError("bad_input", f"the range is longer than {MAX_BAR_DAYS} days", ["Ask in several smaller ranges"])
    as_of = _when(a["as_of"], "as_of") if a.get("as_of") else end
    try:
        return _answer(data.bars(_symbol(a["symbol"]), a["interval"], start=start, end=end, as_of=as_of))
    except ValueError as exc:
        if isinstance(exc, ToolError):
            raise
        raise ToolError("bad_input", str(exc)) from None


@TOOLSET.tool("data_snapshot", "A verified, hashed picture of a symbol at as_of, by category (market, fundamentals). The same numbers always give the same content hash.", {
    "symbol": _s("Symbol."), "as_of": _s("ISO cut-off."), "categories": _texts("market, fundamentals.")}, ["symbol", "as_of", "categories"])
def data_snapshot(u, c, a: dict) -> str:
    import hokma.data as data

    cats = a["categories"]
    if not cats or len(cats) > 10:
        raise ToolError("bad_input", "categories needs between 1 and 10 names", ["market", "fundamentals"])
    try:
        snap = data.snapshot(_symbol(a["symbol"]), as_of=_when(a["as_of"], "as_of"), categories=list(cats))
    except ValueError as exc:
        raise ToolError("bad_input", str(exc)) from None
    payload = snap.to_dict()
    if all(s["status"] != "ok" for s in payload["sections"].values()):
        first = next(iter(payload["sections"].values()))
        code = STATUS_CODE.get(first["status"], "unavailable")
        raise ToolError(code, f"{first['source']}: {first['reason']}", ["Check Connections for the provider of this data"])
    return json.dumps(_clean(payload), ensure_ascii=False, default=str)


@TOOLSET.tool("data_fundamentals", "Financial statements that were public on as_of (selected by filing date, so no future data).", {
    "symbol": _s("Symbol."), "as_of": _s("ISO cut-off."), "statement": _s("income, balance or cashflow."), "period": _s("annual or quarterly.")},
    ["symbol", "as_of", "statement", "period"])
def data_fundamentals(u, c, a: dict) -> str:
    import hokma.data as data

    return _answer(data.fundamentals(_symbol(a["symbol"]), as_of=_when(a["as_of"], "as_of"), statement=str(a["statement"])[:40], period=str(a["period"])[:20]))


@TOOLSET.tool("data_filings", "SEC filings of a form (10-K, 10-Q, 8-K, 4, 13F-HR ...) that were public on as_of.", {
    "symbol": _s("Symbol."), "form": _s("SEC form."), "as_of": _s("ISO cut-off.")}, ["symbol", "form", "as_of"])
def data_filings(u, c, a: dict) -> str:
    import hokma.data as data

    form = str(a["form"]).strip().upper()
    if not re.fullmatch(r"[0-9A-Z][0-9A-Z/\-]{0,11}", form):
        raise ToolError("bad_input", "form is not a SEC form name", ["10-K", "10-Q", "8-K", "4", "13F-HR"])
    return _answer(data.filings(_symbol(a["symbol"]), form=form, as_of=_when(a["as_of"], "as_of")))


@TOOLSET.tool("data_macro", "A macro series as it was published on as_of (the vintage is fixed, later revisions are not used).", {
    "series_id": _s("Series id, e.g. CPIAUCSL."), "as_of": _s("ISO cut-off.")}, ["series_id", "as_of"])
def data_macro(u, c, a: dict) -> str:
    import hokma.data as data

    series = str(a["series_id"]).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,40}", series):
        raise ToolError("bad_input", "series_id has characters a series id never has")
    return _answer(data.macro(series, as_of=_when(a["as_of"], "as_of")))


@TOOLSET.tool("data_universe", "First universe filter over the instrument catalog (asset class, sector, country, exchange ...). Describes the present: with a past date it warns about survivorship bias.", {
    "filters": {"type": "object", "description": "Filter names and values."}}, [])
def data_universe(u, c, a: dict) -> str:
    import hokma.data as data

    filters = a.get("filters") or {}
    if not isinstance(filters, dict) or len(filters) > 20:
        raise ToolError("bad_input", "filters must be an object with at most 20 entries")
    clean: dict[str, Any] = {}
    for key, value in filters.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z_]{0,39}", key) or key in RESERVED_FILTERS:
            raise ToolError("bad_input", f"filter {str(key)[:40]!r} is not allowed", ["Use catalog columns such as sector, industry, country, exchange"])
        if isinstance(value, bool) or isinstance(value, (int, float)):
            clean[key] = value
        elif isinstance(value, str) and len(value) <= 200:
            clean[key] = re.escape(value)          # the catalog matches text as a regular expression: an agent only ever gets a literal match
        else:
            raise ToolError("bad_input", f"filter {key!r} must be a short text, a number or true or false")
    if "as_of" in clean:
        clean["as_of"] = _when(clean["as_of"], "as_of")
    return _answer(data.universe(**clean))


@TOOLSET.tool("data_filings_watch", "Check for new SEC filings of the given forms for a list of symbols. Pass back the accession numbers already seen (seen) so only new ones return.", {
    "symbols": _texts("Up to 20 symbols."), "forms": _texts("Up to 10 forms."), "seen": _texts("Accession numbers already reported."),
    "as_of": _s("ISO cut-off; defaults to now.")}, ["symbols", "forms"])
def data_filings_watch(u, c, a: dict) -> str:
    from hokma.data.sec import live
    from hokma.data.sec.client import SecClient
    from hokma.data.sentinels import utcnow

    symbols, forms = a["symbols"], a["forms"]
    seen = a.get("seen") or []
    if not symbols or len(symbols) > MAX_SYMBOLS or not forms or len(forms) > MAX_FORMS or len(seen) > MAX_SEEN:
        raise ToolError("bad_input", f"symbols up to {MAX_SYMBOLS}, forms up to {MAX_FORMS}, seen up to {MAX_SEEN}")
    as_of = _when(a["as_of"], "as_of") if a.get("as_of") else utcnow()
    got = live.check_alerts(SecClient(), [_symbol(s) for s in symbols], [str(f).upper() for f in forms], {str(s) for s in seen}, as_of=as_of)
    if got.status != "ok":
        raise ToolError(STATUS_CODE.get(got.status, "unavailable"), got.reason, ["Check that the SEC identity is saved in Connections"])
    value = got.value
    alerts = [{"symbol": al.symbol, "accession": al.ref.accession, "form": getattr(al.ref, "form", None),
               "filed": str(getattr(al.ref, "filing_date", ""))} for al in value["alerts"]][:MAX_ROWS]
    return json.dumps({"alerts": alerts, "seen": sorted(value["seen"])[:MAX_SEEN], "warnings": list(got.warnings), "as_of": as_of.isoformat()}, default=str)


TOOLS = TOOLSET.tools
