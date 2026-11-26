"""Data of the Company panel of a desk (SPEC-025, delivery 3): one answer with the blocks of the panel.

Every block comes from a function of hokma.kit.company_tools (the same ones the agents use) or hokma.market.options: no panel and no route
here calls a library or a provider directly. The price is not here: the screen takes it from the stream of the engine. A block whose provider
cannot answer says so (ok false, the provider it asked and the reason in the words of the catalog); it never carries a 0 in place of a missing number.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from hokma.desks.errors import DeskError
from hokma.kit import company_tools

NEWS_DAYS = 30
NEWS_ITEMS = 10
RESULT_QUARTERS = 8
CALENDAR_DAYS = 92
EXPIRATIONS_SHOWN = 6
# the ratios of the table, in order: key shown -> key of basic_financials
RATIOS = (("market_cap_millions", "marketCapitalization"), ("beta", "beta"), ("pe", "peTTM"), ("eps", "epsTTM"),
          ("operating_margin", "operatingMarginTTM"), ("debt_to_equity", "totalDebt/totalEquityQuarterly"),
          ("revenue_growth", "revenueGrowthTTMYoy"))


def _sources(tool: str) -> str:
    """Names of the providers that have this data, for the line "No data from X"."""
    if tool == "options":
        return "Schwab / Alpaca"
    names = [name for name, tools in company_tools.PROVIDERS.values() if tool in tools]
    return " / ".join(names) if names else "-"


def _block(tool: str, ask: Callable[[], dict], shape: Callable[[dict], dict]) -> dict[str, Any]:
    """Ask once; a refusal of the provider becomes a block without data, never an error of the whole panel."""
    try:
        data = ask()
    except (LookupError, ValueError) as exc:
        return {"ok": False, "source": _sources(tool), "reason": str(exc)}
    return {"ok": True, "source": data.get("source") or _sources(tool), "fetched_at": data.get("fetched_at"), **shape(data)}


def _expirations(symbol: str, desk_id: int) -> dict[str, Any]:
    from hokma.market import options

    return _block("options", lambda: options.expirations(symbol, desk_id),
                  lambda d: {"items": [{"date": str(r.get("date") or r.get("expiration")), "days": r.get("days")}
                                       for r in (d.get("expirations") or [])[:EXPIRATIONS_SHOWN]]})


def _ratios(data: dict, profile: dict | None) -> dict:
    metrics = data.get("metrics") or {}
    rows = {key: metrics.get(source) for key, source in RATIOS}
    if rows["market_cap_millions"] is None and profile and profile.get("ok"):
        rows["market_cap_millions"] = profile.get("market_cap_millions")
    return {"ratios": rows}


def company(desk_id: int, symbol: Any) -> dict[str, Any]:
    """The blocks of the panel for one asset of the desk. A symbol that is not a stock symbol is refused (400); the rest never fails as a whole."""
    try:
        sym = company_tools._symbol(symbol)
    except ValueError as exc:
        raise DeskError("bad_symbol", 400) from exc
    today = date.today()
    profile = _block("company_profile", lambda: company_tools.company_profile(sym, desk_id),
                     lambda d: {k: d.get(k) for k in ("name", "exchange", "industry", "currency", "market_cap_millions")})
    expirations = _expirations(sym, desk_id)
    upcoming = _block("earnings_calendar", lambda: company_tools.earnings_calendar(
        today.isoformat(), (today + timedelta(days=CALENDAR_DAYS)).isoformat(), sym, desk_id),
        lambda d: {"next": next((r for r in d.get("earnings", []) if r.get("symbol") in (None, sym) and str(r.get("date")) >= today.isoformat()), None)})
    return {
        "symbol": sym,
        "profile": profile,
        "fundamentals": _block("basic_financials", lambda: company_tools.basic_financials(sym, desk_id),
                               lambda d: _ratios(d, profile)),
        "events": {"earnings": upcoming, "expirations": expirations},
        "results": _block("earnings_surprises", lambda: company_tools.earnings_surprises(sym, RESULT_QUARTERS, desk_id),
                          lambda d: {"rows": d.get("surprises", [])}),
        "news": _block("company_news", lambda: company_tools.company_news(
            sym, (today - timedelta(days=NEWS_DAYS)).isoformat(), today.isoformat(), NEWS_ITEMS, desk_id),
            lambda d: {"items": d.get("news", [])}),
    }
