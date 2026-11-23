"""Snapshots: verified market data, as-of snapshots with a staleness warning, point-in-time fundamentals and the combined
`Snapshot` (SPEC-017: TA-32, OA-60, AH-46).

Everything here is computed without a language model and is the source of truth for exact numbers. Rows after `as_of` are
excluded before anything is computed, a snapshot that is behind its anchor says how many trading days, and a missing piece
is a sentinel in its section: nothing is filled.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from hokma.data import sentinels
from hokma.data.freshness import trading_days_between
from hokma.data.models import FinancialMetrics, PeriodFundamentals
from hokma.data.sentinels import DataResult, parse_time, utcnow
from hokma.desks.core.hashing import content_hash

DEFAULT_INDICATORS: tuple[str, ...] = (
    "close_10_ema", "close_50_sma", "close_200_sma", "rsi", "boll", "boll_ub", "boll_lb", "macd", "macds", "macdh", "atr",
)
MIN_PERIODS = 4   # less than a year of trailing periods supports no defensible statement


# ---------------------------------------------------------------------------------------------------- indicators
def _wilder(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def compute_indicator(frame: pd.DataFrame, name: str) -> float | None:
    """Latest value of an indicator over the frame, or None when there are not enough rows.

    Names: `close_<n>_sma`, `close_<n>_ema`, `rsi` (14, Wilder), `boll` (20-bar mean), `boll_ub` and `boll_lb` (two
    population standard deviations), `macd` (12, 26), `macds` (9), `macdh`, `atr` (14, Wilder).
    """
    close = frame["close"].astype(float)
    if close.empty:
        return None
    match = re.fullmatch(r"close_(\d+)_(sma|ema)", name)
    value: float | None
    if match:
        length, kind = int(match.group(1)), match.group(2)
        if len(close) < length:
            return None
        series = close.rolling(length).mean() if kind == "sma" else close.ewm(span=length, adjust=False, min_periods=length).mean()
        value = series.iloc[-1]
    elif name == "rsi":
        change = close.diff()
        gain, loss = _wilder(change.clip(lower=0), 14), _wilder((-change).clip(lower=0), 14)
        last_gain, last_loss = gain.iloc[-1], loss.iloc[-1]
        if pd.isna(last_gain) or pd.isna(last_loss):
            return None
        value = 100.0 if last_loss == 0 else 100.0 - 100.0 / (1.0 + last_gain / last_loss)
    elif name in ("boll", "boll_ub", "boll_lb"):
        if len(close) < 20:
            return None
        mean, std = close.rolling(20).mean().iloc[-1], close.rolling(20).std(ddof=0).iloc[-1]
        value = {"boll": mean, "boll_ub": mean + 2 * std, "boll_lb": mean - 2 * std}[name]
    elif name in ("macd", "macds", "macdh"):
        if len(close) < 26:
            return None
        line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        signal = line.ewm(span=9, adjust=False).mean()
        value = {"macd": line.iloc[-1], "macds": signal.iloc[-1], "macdh": line.iloc[-1] - signal.iloc[-1]}[name]
    elif name == "atr":
        if len(close) < 15:
            return None
        previous = close.shift(1)
        high, low = frame["high"].astype(float), frame["low"].astype(float)
        true_range = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
        value = _wilder(true_range, 14).iloc[-1]
    else:
        raise ValueError(f"unknown indicator {name!r}")
    return None if value is None or pd.isna(value) else float(value)


def _cut(frame: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    """Rows on or before `as_of`, sorted, with a close. Re-applied here because a verification path must not trust its input."""
    cutoff = pd.Timestamp(as_of)
    index = frame.index
    if getattr(index, "tz", None) is None:
        index = index.tz_localize("UTC")
        frame = frame.set_axis(index)
    out = frame[frame.index <= cutoff].sort_index()
    return out[out["close"].notna()]


# ---------------------------------------------------------------------------------------------------- TA-32
def verified_market_snapshot(frame: pd.DataFrame, as_of: datetime, *, look_back: int = 30,
                             indicators: Sequence[str] = DEFAULT_INDICATORS) -> dict[str, Any]:
    """Ground truth of the market data at `as_of`: the newest row on or before it, a fixed set of indicators and the recent
    closes. Treat it as the source of truth for any exact price or indicator claim."""
    rows = _cut(frame, parse_time(as_of))
    if rows.empty:
        raise ValueError("no rows on or before as_of")
    latest = rows.iloc[-1]
    window = max(1, min(int(look_back), 30))
    return {
        "requested_as_of": parse_time(as_of).isoformat(),
        "latest_bar": rows.index[-1].isoformat(),
        "rows_used": int(len(rows)),
        "latest": {field_: (None if pd.isna(latest.get(field_)) else float(latest.get(field_)))
                   for field_ in ("open", "high", "low", "close", "volume")},
        "indicators": {name: compute_indicator(rows, name) for name in indicators},
        "recent_closes": [{"time": stamp.isoformat(), "close": float(value)} for stamp, value in rows["close"].tail(window).items()],
    }


# ---------------------------------------------------------------------------------------------------- OA-60
def as_of_snapshot(frame: pd.DataFrame, as_of: datetime, *, bars_out: int = 0) -> dict[str, Any]:
    """The honest "what did it look like at T": the newest actual bar at or before `as_of` with its change against the
    previous close, a compact technical state, and a loud warning when the data ends before the anchor."""
    cut = parse_time(as_of)
    rows = _cut(frame, cut)
    out: dict[str, Any] = {"as_of": cut.isoformat(), "window_bars": int(len(rows)), "warnings": []}
    if rows.empty:
        return {**out, "latest": None, "levels": None, "bars": [], "is_latest_actual": False, "stale_trading_days": None}
    last, closes = rows.iloc[-1], rows["close"].astype(float)
    previous = float(closes.iloc[-2]) if len(closes) >= 2 else None
    # An anchor at 00:00 UTC is the start of its day: the newest bar that can exist is the one of the day before.
    anchor_day = (cut - timedelta(days=1)).date() if (cut.hour, cut.minute, cut.second) == (0, 0, 0) else cut.date()
    gap = trading_days_between(rows.index[-1].date(), anchor_day)
    out["is_latest_actual"] = gap == 0
    out["stale_trading_days"] = gap
    if gap > 0:
        out["warnings"].append(f"data ends {rows.index[-1].date()} but the anchor is {anchor_day}: {gap} trading day(s) behind; "
                               f"this is not the latest print, treat the close as stale")
    high, low = float(rows["high"].max()), float(rows["low"].min())
    close = float(last["close"])
    out["latest"] = {
        "time": rows.index[-1].isoformat(), "close": close, "previous_close": previous,
        "change_pct": None if previous in (None, 0) else (close / previous - 1) * 100,
        "day_high": float(last["high"]), "day_low": float(last["low"]),
        "day_amplitude_pct": None if previous in (None, 0) else (float(last["high"]) - float(last["low"])) / previous * 100,
    }
    out["levels"] = {
        "sma20": compute_indicator(rows, "close_20_sma"), "sma50": compute_indicator(rows, "close_50_sma"),
        "rsi14": compute_indicator(rows, "rsi"), "period_high": high, "period_low": low,
        "dist_from_high_pct": None if high == 0 else (close - high) / high * 100,
        "dist_from_low_pct": None if low == 0 else (close - low) / low * 100,
    }
    shown = rows.tail(max(0, min(bars_out, len(rows))))
    out["bars"] = [{"time": stamp.isoformat(), **{k: float(row[k]) for k in ("open", "high", "low", "close")}} for stamp, row in shown.iterrows()]
    return out


# ---------------------------------------------------------------------------------------------------- AH-46
def _average(values: Sequence[float | None]) -> float | None:
    xs = [v for v in values if v is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _trend(values: Sequence[float | None]) -> float | None:
    """Latest minus oldest; values arrive newest first."""
    xs = [v for v in values if v is not None]
    return round(xs[0] - xs[-1], 4) if len(xs) >= 2 else None


def _cagr(values: Sequence[float | None]) -> float | None:
    """Annualised growth from oldest to latest of trailing periods spaced one quarter apart."""
    xs = [v for v in values if v is not None]
    if len(xs) < 2 or xs[-1] <= 0 or xs[0] <= 0:
        return None
    return round((xs[0] / xs[-1]) ** (1 / ((len(xs) - 1) / 4)) - 1, 4)


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    if abs(value) >= 1e9:
        return f"{value / 1e9:.1f}B"
    if abs(value) >= 1e6:
        return f"{value / 1e6:.1f}M"
    return f"{value:.2f}"


@dataclass(frozen=True)
class FundamentalsSnapshot:
    """What an analyst may know about a company at `as_of`, newest period first. The derived numbers are computed here, not
    by a model. `content_hash` and `render` leave `as_of` out: two dates between filings see identical data and so give an
    identical prompt (a cache hit) instead of two paid model calls."""

    ticker: str
    as_of: str
    sector: str | None
    industry: str | None
    periods: tuple
    roe_avg: float | None = None
    net_margin_avg: float | None = None
    gross_margin_trend: float | None = None
    bvps_cagr: float | None = None
    debt_to_equity_latest: float | None = None
    market_cap_latest: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "as_of": self.as_of, "sector": self.sector, "industry": self.industry,
                "periods": [p.to_dict() for p in self.periods], "roe_avg": self.roe_avg, "net_margin_avg": self.net_margin_avg,
                "gross_margin_trend": self.gross_margin_trend, "bvps_cagr": self.bvps_cagr,
                "debt_to_equity_latest": self.debt_to_equity_latest, "market_cap_latest": self.market_cap_latest}

    @property
    def content_hash(self) -> str:
        data = self.to_dict()
        data.pop("as_of")
        return content_hash(data)[:24]

    def render(self) -> str:
        head = f"Company: {self.ticker}" + (f"  |  Sector: {self.sector}" if self.sector else "") + \
            (f"  |  Industry: {self.industry}" if self.industry else "")
        lines = [head, "All figures below were publicly filed by their filing dates. Treat the most recent filing shown as the present.", "",
                 "Summary:", f"  Market cap (latest filed): {_fmt(self.market_cap_latest)}",
                 f"  ROE avg: {_fmt(self.roe_avg)}  |  Net margin avg: {_fmt(self.net_margin_avg)}",
                 f"  Gross margin trend (latest-oldest): {_fmt(self.gross_margin_trend)}",
                 f"  Book value/share CAGR: {_fmt(self.bvps_cagr)}", f"  Debt/equity (latest): {_fmt(self.debt_to_equity_latest)}", "",
                 "History (trailing periods, newest first):",
                 "period | filed | mktcap | P/E | ROE | gross_m | op_m | net_m | D/E | curr | rev_gr | EPS | BVPS | FCF/sh"]
        for p in self.periods:
            lines.append(" | ".join([p.report_period, p.filing_date or "?", _fmt(p.market_cap), _fmt(p.price_to_earnings_ratio),
                                     _fmt(p.return_on_equity), _fmt(p.gross_margin), _fmt(p.operating_margin), _fmt(p.net_margin),
                                     _fmt(p.debt_to_equity), _fmt(p.current_ratio), _fmt(p.revenue_growth), _fmt(p.earnings_per_share),
                                     _fmt(p.book_value_per_share), _fmt(p.free_cash_flow_per_share)]))
        return "\n".join(lines)


def build_fundamentals_snapshot(ticker: str, as_of: datetime | str, metrics: Sequence[Mapping[str, Any] | Any], *,
                                facts: Mapping[str, Any] | None = None, periods: int = 20, min_periods: int = MIN_PERIODS,
                                source: str = "financial_datasets", clock: Callable[[], datetime] = utcnow) -> DataResult:
    """The point-in-time snapshot of a company, from metrics that carry a filing date.

    Only rows filed on or before `as_of` are used. With fewer than `min_periods` rows the answer is `NO_DATA`, not a
    neutral view. Market cap comes from the newest FILED row, not from a live profile (which would leak the present).
    """
    from hokma.data import pit

    cut = parse_time(as_of)
    rows = [m.to_dict() if hasattr(m, "to_dict") else dict(m) for m in metrics]
    known = pit.filter_financials_by_date(rows, cut)
    known.sort(key=lambda r: str(r.get("filing_date") or ""), reverse=True)
    known = known[:periods]
    if len(known) < min_periods:
        return sentinels.no_data(source, "insufficient_periods", as_of=cut, received_at=clock(),
                                 warnings=(f"{ticker}: {len(known)} filed periods on or before {cut.date()}, need {min_periods}",))
    records = [FinancialMetrics.from_record(r) for r in known]
    compact = tuple(PeriodFundamentals.from_record(r.to_dict()) for r in records)
    snap = FundamentalsSnapshot(
        ticker=ticker, as_of=cut.strftime("%Y-%m-%d"), sector=(facts or {}).get("sector"), industry=(facts or {}).get("industry"),
        periods=compact, roe_avg=_average([r.return_on_equity for r in records]),
        net_margin_avg=_average([r.net_margin for r in records]), gross_margin_trend=_trend([r.gross_margin for r in records]),
        bvps_cagr=_cagr([r.book_value_per_share for r in records]), debt_to_equity_latest=records[0].debt_to_equity,
        market_cap_latest=records[0].market_cap)
    warnings = ("sector_industry_are_latest_not_vintage",) if facts else ()
    return sentinels.ok(snap, source=source, as_of=cut, received_at=clock(), warnings=warnings)


# ---------------------------------------------------------------------------------------------------- Snapshot
@dataclass(frozen=True)
class Snapshot:
    """Verified, hashed picture of a symbol at `as_of`: one section per category, each a `DataResult`."""

    symbol: str
    as_of: datetime
    sections: dict
    content_hash: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(section.ok for section in self.sections.values())

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "as_of": self.as_of.isoformat(), "content_hash": self.content_hash,
                "warnings": list(self.warnings), "sections": {k: v.to_dict() for k, v in self.sections.items()}}


def _section_payload(result: DataResult) -> dict[str, Any]:
    value = result.value.to_dict() if hasattr(result.value, "to_dict") and not isinstance(result.value, pd.DataFrame) else result.value
    return {"status": result.status, "reason": result.reason, "value": value, "source": result.source}


def snapshot(symbol: str, *, as_of: datetime, categories: Sequence[str],
             sources: Mapping[str, Callable[[str, datetime], DataResult]] | None = None) -> Snapshot:
    """Build the snapshot of `symbol` for the `categories` asked (`market`, `fundamentals`, or any key of `sources`).

    The hash covers what the sections say, not when they were received, so the same numbers hash the same on any day.
    """
    cut = parse_time(as_of)
    providers = {"market": _market_section, "fundamentals": _fundamentals_section, **dict(sources or {})}
    unknown = [c for c in categories if c not in providers]
    if unknown:
        raise ValueError(f"unknown snapshot categories {unknown}; expected from {sorted(providers)}")
    sections = {category: providers[category](symbol, cut) for category in categories}
    digest = content_hash({k: _section_payload(v) for k, v in sections.items()})
    warnings = tuple(f"{k}: {w}" for k, v in sections.items() for w in v.warnings)
    return Snapshot(symbol, cut, sections, digest, warnings)


def _market_section(symbol: str, as_of: datetime) -> DataResult:
    from hokma.data import ohlcv

    bars = ohlcv.bars(symbol, "1D", start=as_of - timedelta(days=400), end=as_of, as_of=as_of)
    if not bars.ok:
        return bars
    detail = as_of_snapshot(bars.value, as_of)
    detail["verified"] = verified_market_snapshot(bars.value, as_of)
    return sentinels.ok(detail, source=bars.source, as_of=as_of, received_at=bars.received_at, warnings=tuple(detail.pop("warnings")))


def _fundamentals_section(symbol: str, as_of: datetime) -> DataResult:
    from hokma.data import fundamentals_src

    result = fundamentals_src.fundamentals(symbol, as_of=as_of, statement="metrics", period="ttm")
    if not result.ok:
        return result
    built = build_fundamentals_snapshot(symbol, as_of, result.value, source=result.source)
    return built if not built.ok else sentinels.ok(built.value.to_dict(), source=built.source, as_of=as_of, received_at=built.received_at, warnings=built.warnings)


sentinels.make_module_callable(__name__, "snapshot")
