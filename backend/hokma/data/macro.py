"""Macro series with their vintage (SPEC-017: TA-70, FT-094, FT-095, FT-096, OA-66).

A macro number is asked for as it was published on the run date, not as it was later revised: the series is requested with
the real-time window pinned to `as_of`. A missing observation is skipped and counted, never filled. Macro is an optional
category: a failure degrades to `DATA_UNAVAILABLE` and never aborts a run.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from hokma.data import _lib, pit, sentinels
from hokma.data.providers import (KeyResolver, NoDataError, NotConfiguredError, ProviderConfig, RateLimitError, Transport,
                                  check_status, connections_key, http_transport, route)
from hokma.data.sentinels import DataResult, parse_time, utcnow

FRED_BASE = "https://api.stlouisfed.org/fred"
DEFAULT_LOOKBACK_DAYS = 365
MAX_ROWS = 400

# Aliases to series ids. Anything not listed is used as a raw series id, so no one is limited to this set.
MACRO_ALIASES: dict[str, str] = {
    "fed_funds_rate": "FEDFUNDS", "fed_funds": "DFF", "2y_treasury": "DGS2", "10y_treasury": "DGS10", "30y_treasury": "DGS30",
    "10y_2y_spread": "T10Y2Y", "yield_curve": "T10Y2Y",
    "real_yield_10y": "DFII10", "real_yield_5y": "DFII5", "breakeven_10y": "T10YIE", "breakeven_5y": "T5YIE",
    "inflation_expectations": "T10YIE",
    "cpi": "CPIAUCSL", "core_cpi": "CPILFESL", "pce": "PCEPI", "core_pce": "PCEPILFE",
    "real_gdp": "GDPC1", "gdp": "GDP", "industrial_production": "INDPRO", "retail_sales": "RSAFS",
    "unemployment_rate": "UNRATE", "unemployment": "UNRATE", "nonfarm_payrolls": "PAYEMS", "payrolls": "PAYEMS", "initial_claims": "ICSA",
    "personal_income": "PI", "housing_starts": "HOUST", "mortgage_rate_30y": "MORTGAGE30US", "recession": "USREC",
    "home_prices": "CSUSHPINSA", "m2": "M2SL", "vix": "VIXCLS", "dollar_index": "DTWEXBGS", "consumer_sentiment": "UMCSENT",
}


def resolve_series_id(indicator: str) -> str:
    """Map an alias to a series id or pass a plausible raw id through. Raises ValueError for a phrase that is neither."""
    key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
    if key in MACRO_ALIASES:
        return MACRO_ALIASES[key]
    raw = indicator.strip().upper()
    if not raw or len(raw) > 30 or any(ch.isspace() for ch in raw):
        raise ValueError(f"{indicator!r} is not a known alias or a valid series id")
    return raw


def _provider_today() -> str:
    """The provider's calendar date. Its clock is US Central; UTC-6 is never ahead of it, so the pin is never in its future."""
    return datetime.now(timezone(timedelta(hours=-6))).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------------------------------- TA-70
class FredProvider:
    """FRED series pinned to a vintage. Needs the student's free key (Connections, provider `fred`)."""

    name = "fred"

    def __init__(self, *, key: KeyResolver = connections_key, transport: Transport = http_transport,
                 today: Callable[[], str] = _provider_today) -> None:
        self._key, self._transport, self._today = key, transport, today

    def _request(self, path: str, params: Mapping[str, Any]) -> dict:
        secret = self._key("fred")
        if not secret:
            raise NotConfiguredError(self.name)
        status, body = self._transport(f"{FRED_BASE}/{path}", {**params, "api_key": secret, "file_type": "json"}, {})
        check_status(self.name, status, body, secret=secret)
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise NoDataError(str(params.get("series_id")), None, "provider answered with something that is not JSON") from error

    def series(self, series_id: str, as_of: datetime, look_back_days: int = DEFAULT_LOOKBACK_DAYS) -> dict[str, Any]:
        """Observations in [as_of - look_back, as_of] as published on `as_of`. The vintage pin on the metadata AND the
        observations request is what keeps later revisions out."""
        cut = parse_time(as_of)
        day = cut.strftime("%Y-%m-%d")
        pin = min(day, self._today())
        realtime = {"realtime_start": pin, "realtime_end": pin}
        meta = (self._request("series", {"series_id": series_id, **realtime}).get("seriess") or [])
        if not meta:
            raise NoDataError(series_id, None, "series not found or not yet published at this vintage")
        start = (cut - timedelta(days=look_back_days)).strftime("%Y-%m-%d")
        payload = self._request("series/observations", {"series_id": series_id, "observation_start": start,
                                                        "observation_end": day, **realtime})
        points, missing = [], 0
        for row in payload.get("observations") or []:
            try:
                points.append({"date": row["date"], "value": float(row["value"])})
            except (KeyError, TypeError, ValueError):
                missing += 1          # FRED writes "." for an observation it does not have; it stays missing
        info = meta[0]
        return {"series_id": series_id, "title": info.get("title", series_id), "units": info.get("units", ""),
                "frequency": info.get("frequency", ""), "vintage": pin, "observations": points[-MAX_ROWS:],
                "missing_observations": missing}


def _summarise(series: dict[str, Any]) -> dict[str, Any]:
    points = series["observations"]
    series = dict(series)
    series["latest"] = points[-1] if points else None
    series["change"] = (points[-1]["value"] - points[0]["value"]) if len(points) >= 2 else None
    return series


def macro(series_id: str, *, as_of: datetime, look_back_days: int = DEFAULT_LOOKBACK_DAYS,
          providers: Mapping[str, Callable[..., Any]] | None = None, config: ProviderConfig | None = None,
          key: KeyResolver = connections_key, transport: Transport = http_transport,
          clock: Callable[[], datetime] = utcnow) -> DataResult:
    """A macro series as published on `as_of` (vintage fixed). `series_id` is an alias or a raw FRED id.

    The value holds the observations, the latest one and the vintage used. No key: `DATA_UNAVAILABLE` with the reason
    `provider_key_missing`. An unknown series or no observation in the window: `NO_DATA`.
    """
    cut = parse_time(as_of)
    try:
        resolved = resolve_series_id(series_id)
    except ValueError as error:
        return sentinels.no_data("fred", "unknown_series", as_of=cut, received_at=clock(), warnings=(str(error),))
    fred = FredProvider(key=key, transport=transport)
    impls = dict(providers) if providers is not None else {"fred": lambda sid, when, days: _summarise(fred.series(sid, when, days))}
    chain = (config or ProviderConfig()).chain_for("macro", "macro", available=impls)
    result = route("macro", impls, chain, resolved, cut, look_back_days, category="macro", as_of=cut, clock=clock)
    if result.ok and not result.value.get("observations"):
        return sentinels.no_data(result.source, "no_observations_in_window", as_of=cut, received_at=result.received_at)
    if result.ok and cut.strftime("%Y-%m-%d") > _provider_today():
        return result.with_warnings("as_of_after_provider_today_vintage_clamped")
    return result


# ---------------------------------------------------------------------------------------------------- OA-66
CURATED_BOARD: tuple[tuple[str, str, str], ...] = (
    ("DFF", "Fed Funds Rate", "percent"), ("DGS2", "2Y Treasury", "percent"), ("DGS10", "10Y Treasury", "percent"),
    ("T10Y2Y", "10Y-2Y Spread", "percent"), ("UNRATE", "Unemployment Rate", "percent"), ("ICSA", "Initial Jobless Claims", "count"),
    ("DTWEXBGS", "Dollar Index (Broad)", "index"), ("PAYEMS", "Nonfarm Payrolls", "thousands"), ("T10YIE", "10Y Breakeven Inflation", "percent"),
    ("UMCSENT", "Consumer Sentiment", "index"),
)


def year_over_year(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Percent change against the same date a year earlier, where that date exists. Dates without a year-ago match are
    skipped, not interpolated."""
    by_date = {o["date"]: o["value"] for o in observations}
    out = []
    for o in observations:
        year, rest = o["date"][:4], o["date"][4:]
        if not year.isdigit():
            continue
        base = by_date.get(f"{int(year) - 1}{rest}")
        if isinstance(base, (int, float)) and base != 0:
            out.append({"date": o["date"], "value": (o["value"] / base - 1) * 100})
    return out


def macro_board(*, as_of: datetime, look_back_days: int = 800, fetch: Callable[..., DataResult] | None = None,
                clock: Callable[[], datetime] = utcnow, **kwargs: Any) -> DataResult:
    """Regime inputs on one screen: rates, curve, labour, inflation, dollar. Each card is one series at its own vintage;
    CPI year over year is derived from the index. A series that fails is a card with its reason; if all fail the board is
    `DATA_UNAVAILABLE`."""
    cut = parse_time(as_of)
    get = fetch or macro
    cards, failures = [], {}
    for series_id, label, unit in CURATED_BOARD:
        got = get(series_id, as_of=cut, look_back_days=look_back_days, clock=clock, **kwargs)
        if got.ok:
            cards.append({"id": series_id, "label": label, "unit": unit, "latest": got.value["latest"], "vintage": got.value["vintage"]})
        else:
            failures[series_id] = {"status": got.status, "reason": got.reason}
    cpi = get("CPIAUCSL", as_of=cut, look_back_days=look_back_days, clock=clock, **kwargs)
    cpi_yoy = year_over_year(cpi.value["observations"]) if cpi.ok else []
    if cpi.ok:
        cards.append({"id": "CPI_YOY", "label": "CPI year over year", "unit": "percent",
                      "latest": cpi_yoy[-1] if cpi_yoy else None, "vintage": cpi.value["vintage"]})
    else:
        failures["CPIAUCSL"] = {"status": cpi.status, "reason": cpi.reason}
    if not cards:
        reason = next(iter(failures.values()))["reason"]
        return sentinels.unavailable("fred", reason, as_of=cut, received_at=clock(), meta={"failures": failures})
    return sentinels.ok({"cards": cards, "failures": failures}, source="fred", as_of=cut, received_at=clock(),
                        warnings=tuple(f"{k}:{v['reason']}" for k, v in failures.items()))


class CachedBoard:
    """Cache for slow data behind a quota: fresh inside the TTL, and when the upstream fails the last good value is served
    marked `stale`. With no last good value the error stays loud."""

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._ttl, self._clock = ttl_seconds, clock
        self._entries: dict[str, tuple[datetime, Any]] = {}

    def get(self, key: str, loader: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
        now = self._clock()
        entry = self._entries.get(key)
        if entry and (now - entry[0]).total_seconds() < self._ttl:
            return entry[1], {"cached_at": entry[0].isoformat(), "stale": False}
        try:
            value = loader()
        except Exception:
            if entry:
                return entry[1], {"cached_at": entry[0].isoformat(), "stale": True}
            raise
        self._entries[key] = (now, value)
        return value, {"cached_at": None, "stale": False}


GLOBAL_INDICATORS = ("cpi_yoy", "short_rate", "composite_leading_indicator")


def normalize_global_cell(indicator: str, value: float) -> float:
    """Display percent. Short rates come as fractions (0.0372) and CPI year over year already as percent (3.81)."""
    return value * 100.0 if indicator == "short_rate" else value


def build_global_macro(raw: Mapping[str, Sequence[Mapping[str, Any]]], countries: Sequence[str]) -> list[dict[str, Any]]:
    """One row per country with the latest cell of each indicator, from rows `{date, country, value}`. A country with no
    observation for an indicator has an empty cell, not a copy of another country's."""
    rows = []
    for country in countries:
        row: dict[str, Any] = {"country": country}
        for indicator in GLOBAL_INDICATORS:
            mine = [r for r in raw.get(indicator, []) if r.get("country") == country and isinstance(r.get("value"), (int, float))]
            latest = max(mine, key=lambda r: r["date"], default=None)
            row[indicator] = {"value": normalize_global_cell(indicator, latest["value"]), "date": latest["date"]} if latest else {"value": None, "date": None}
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------------------------------- FT-095
COUNTRY_KINDS: dict[str, str] = {
    "consumer_price_index": "get_consumer_price_index", "inflation_rate": "get_inflation_rate",
    "gross_domestic_product": "get_gross_domestic_product", "gdp_growth": "get_gross_domestic_product_growth",
    "central_bank_policy_rate": "get_central_bank_policy_rate", "long_term_interest_rate": "get_long_term_interest_rate",
    "composite_leading_indicator": "get_composite_leading_indicator", "house_prices": "get_house_prices",
    "industrial_production": "get_industrial_production_index", "consumer_confidence": "get_consumer_confidence_index",
    "business_confidence": "get_business_confidence_index", "unemployment_claims": "get_initial_jobless_claims",
    "exchange_rates": "get_exchange_rates", "money_supply": "get_money_supply",
}


def _frame_records(frame: Any) -> list[dict[str, Any]]:
    table = frame.reset_index() if hasattr(frame, "reset_index") else pd.DataFrame(frame)
    return json.loads(table.to_json(orient="records", date_format="iso", default_handler=str))


def _cut_frame(frame: Any, as_of: datetime) -> Any:
    """Rows dated on or before `as_of` (period or date index). Forecast rows are dropped with the rest of the future."""
    index = frame.index
    if isinstance(index, pd.PeriodIndex):
        stamps = index.to_timestamp(how="start")
    elif isinstance(index, pd.DatetimeIndex):
        stamps = index
    else:
        return frame
    stamps = stamps.tz_localize("UTC") if stamps.tz is None else stamps.tz_convert("UTC")
    return frame[stamps <= pd.Timestamp(as_of)]


def _call_library(label: str, func: Callable[[], Any]) -> Any:
    try:
        return func()
    except ValueError as error:
        if "api key" in str(error).lower():
            raise NotConfiguredError(label, str(error)) from error
        raise
    except OSError as error:
        raise RateLimitError(f"{label} unreachable: {type(error).__name__}", reason=sentinels.REASON_UNREACHABLE) from error


def country_series(kind: str, *, as_of: datetime, start: datetime | None = None, factory: Callable[..., Any] | None = None,
                   key: KeyResolver = connections_key, clock: Callable[[], datetime] = utcnow, **kwargs: Any) -> DataResult:
    """Macro series of other countries (OECD, GMDB, economy data of a free provider) through the economics module. Rows after
    `as_of` are cut. The source is named `economics`."""
    cut = parse_time(as_of)
    if kind not in COUNTRY_KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {sorted(COUNTRY_KINDS)}")
    make = factory or _lib.economics
    begin = (start or cut - timedelta(days=DEFAULT_LOOKBACK_DAYS * 3)).strftime("%Y-%m-%d")

    def load() -> Any:
        client = make(start_date=begin, end_date=cut.strftime("%Y-%m-%d"), fred_api_key=key("fred") or "")
        return getattr(client, COUNTRY_KINDS[kind])(**kwargs)

    try:
        frame = _call_library("economics", load)
    except NotConfiguredError as error:
        return sentinels.unavailable("economics", sentinels.REASON_KEY_MISSING, as_of=cut, received_at=clock(), warnings=(str(error),))
    except RateLimitError as error:
        return sentinels.unavailable("economics", error.reason, as_of=cut, received_at=clock(), warnings=(str(error),))
    if frame is None or len(frame) == 0:
        return sentinels.no_data("economics", sentinels.REASON_EMPTY, as_of=cut, received_at=clock())
    cutoff = _cut_frame(frame, cut)
    if len(cutoff) == 0:
        return sentinels.no_data("economics", "no_rows_by_as_of", as_of=cut, received_at=clock())
    return sentinels.ok(_frame_records(cutoff), source="economics", as_of=cut, received_at=clock(),
                        warnings=("economy_series_are_latest_revision_not_vintage",))


# ---------------------------------------------------------------------------------------------------- FT-096
# kind -> (library method, whether the answer is a dated series that can be cut at as_of)
FIXED_INCOME_KINDS: dict[str, tuple[str, bool]] = {
    "treasury_rates": ("get_treasury_rates", True), "government_bond_yield": ("get_government_bond_yield", True),
    "government_bond_yield_curve": ("get_government_bond_yield_curve", True), "yield_curve_spread": ("get_yield_curve_spread", True),
    "federal_reserve_rates": ("get_federal_reserve_rates", True), "european_central_bank_rates": ("get_european_central_bank_rates", True),
    "euribor_rates": ("get_euribor_rates", True), "bank_of_england_yield_curve": ("get_bank_of_england_yield_curve", True),
    "corporate_bond_yields": ("get_corporate_bond_yields", True), "breakeven_inflation_rate": ("get_breakeven_inflation_rate", True),
    "option_adjusted_spread": ("get_ice_bofa_option_adjusted_spread", True), "term_premium": ("get_term_premium", True),
    "zero_coupon_curve": ("get_zero_coupon_curve", True),
    "duration": ("get_duration", False), "yield_to_maturity": ("get_yield_to_maturity", False), "par_yield": ("get_par_yield", False),
    "forward_rate": ("get_forward_rate", False),
}


def fixed_income(kind: str, *, as_of: datetime, factory: Callable[..., Any] | None = None, key: KeyResolver = connections_key,
                 today: Callable[[], str] = _provider_today, clock: Callable[[], datetime] = utcnow, **kwargs: Any) -> DataResult:
    """Fixed income data: curves, spreads, central bank and money market rates, duration and yield to maturity.

    Dated series are cut at `as_of`. Duration and yield to maturity describe bonds as they are today and carry no vintage:
    for an `as_of` in the past they are withheld.
    """
    cut = parse_time(as_of)
    if kind not in FIXED_INCOME_KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {sorted(FIXED_INCOME_KINDS)}")
    method, dated = FIXED_INCOME_KINDS[kind]
    if not dated and cut.strftime("%Y-%m-%d") < today():
        return sentinels.no_data("fixed_income", sentinels.REASON_UNSUPPORTED, as_of=cut, received_at=clock(),
                                 warnings=(f"{kind} has no vintage: it describes bonds as they are today",))
    make = factory or _lib.fixed_income

    def load() -> Any:
        client = make(start_date=(cut - timedelta(days=DEFAULT_LOOKBACK_DAYS * 3)).strftime("%Y-%m-%d"),
                      end_date=cut.strftime("%Y-%m-%d"), fred_api_key=key("fred") or "", api_key=key("fmp") or "")
        return getattr(client, method)(**kwargs)

    try:
        frame = _call_library("fixed_income", load)
    except NotConfiguredError as error:
        return sentinels.unavailable("fixed_income", sentinels.REASON_KEY_MISSING, as_of=cut, received_at=clock(), warnings=(str(error),))
    except RateLimitError as error:
        return sentinels.unavailable("fixed_income", error.reason, as_of=cut, received_at=clock(), warnings=(str(error),))
    if frame is None or len(frame) == 0:
        return sentinels.no_data("fixed_income", sentinels.REASON_EMPTY, as_of=cut, received_at=clock())
    if dated:
        frame = _cut_frame(frame, cut)
        if len(frame) == 0:
            return sentinels.no_data("fixed_income", "no_rows_by_as_of", as_of=cut, received_at=clock())
    return sentinels.ok(_frame_records(frame), source="fixed_income", as_of=cut, received_at=clock())


sentinels.make_module_callable(__name__, "macro")
