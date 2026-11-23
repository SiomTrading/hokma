"""Quality of bars and of the data pipeline (SPEC-017: OA-59, ECT-067).

`inspect_bar_quality` and `drop_invalid_bars` exclude rows whose OHLC is missing, not finite or inconsistent and say what
was excluded. The rest is the checklist of a data pipeline: the biases that invalidate a test, reconciliation between two
providers, price and fundamental reasonableness checks, and the daily, weekly and quarterly lists.
These checks do not handle futures rolls, session calendars or 23-hour gaps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

OHLC_FIELDS = ("open", "high", "low", "close")


# ---------------------------------------------------------------------------------------------------- OA-59
def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def invalid_ohlc_fields(row: Mapping[str, Any]) -> list[str]:
    """Fields of a bar that are missing, not finite, not positive, or inconsistent with the others (a high below the open,
    the close or the low; a low above them)."""
    values = {name: _number(row.get(name)) for name in OHLC_FIELDS}
    bad = [name for name, value in values.items() if value is None or value <= 0]
    if bad:
        return bad
    o, h, l, c = (values[name] for name in OHLC_FIELDS)
    if h < max(o, c, l):
        bad.append("high")
    if l > min(o, c, h):
        bad.append("low")
    return bad


@dataclass(frozen=True)
class BarQuality:
    scope: str
    inspected_rows: int
    excluded_rows: int
    latest_excluded_record_at: str | None
    latest_excluded_fields: tuple[str, ...]
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"scope": self.scope, "inspected_rows": self.inspected_rows, "excluded_rows": self.excluded_rows,
                "latest_excluded_record_at": self.latest_excluded_record_at,
                "latest_excluded_fields": list(self.latest_excluded_fields), "reason": self.reason}


def inspect_bar_quality(rows: Sequence[Mapping[str, Any]]) -> BarQuality:
    """What would be excluded from a fetched window, before any count is applied."""
    flagged = [(row, invalid_ohlc_fields(row)) for row in rows]
    excluded = [(row, fields) for row, fields in flagged if fields]
    dated = sorted((pair for pair in excluded if isinstance(pair[0].get("date"), str)), key=lambda pair: pair[0]["date"])
    latest = dated[-1] if dated else None
    return BarQuality("fetched_window_before_count", len(rows), len(excluded),
                      str(latest[0]["date"]) if latest else None, tuple(latest[1]) if latest else (),
                      "missing_or_invalid_ohlc" if excluded else None)


def drop_invalid_bars(frame: pd.DataFrame) -> tuple[pd.DataFrame, BarQuality]:
    """The frame without the rows that have a bad OHLC, and the quality report of the original rows. Rows are never
    repaired or filled."""
    rows = []
    for stamp, row in frame.iterrows():
        record = {name: row.get(name) for name in OHLC_FIELDS}
        record["date"] = stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp)
        rows.append(record)
    report = inspect_bar_quality(rows)
    keep = np.array([not invalid_ohlc_fields(r) for r in rows], dtype=bool)
    return frame.loc[keep].copy(), report


# ---------------------------------------------------------------------------------------------------- ECT-067
@dataclass(frozen=True)
class Bias:
    name: str
    what: str
    fix: str


BIASES: tuple[Bias, ...] = (
    Bias("survivorship", "The universe holds only securities that survived to today; bankruptcies, delistings and "
         "acquisitions are missing.", "Use point-in-time membership and keep delisted securities with their final prices."),
    Bias("look_ahead", "Information used before it was available: statements dated by fiscal period end instead of "
         "filing date, revised data on its first release date, today's index members in the past.",
         "Date every fact by when it became public; use data as first reported."),
    Bias("time_period", "Results depend on the start and end dates chosen.",
         "Test across several regimes and report by sub-period."),
    Bias("selection", "Many variants were tried and the best one is reported.",
         "Count the trials and correct for multiple comparisons."),
    Bias("backfill", "A database added entries retroactively with their good history.",
         "Use data collected with its retrieval timestamp."),
    Bias("reporting", "Companies choose what to disclose (adjusted versus reported earnings).",
         "Use one consistent definition, preferably the reported one."),
    Bias("restatement", "Figures were restated months or years later.", "Use as-reported figures, not restated ones."),
    Bias("timestamp", "Exchange time, UTC and local time are mixed or a bar is labelled with the wrong boundary.",
         "Keep one time zone and state the bar boundary."),
)


@dataclass(frozen=True)
class QualityFinding:
    code: str
    severity: str          # warning | alert | critical
    message: str
    where: str = ""


@dataclass(frozen=True)
class Reconciliation:
    compared: int
    max_discrepancy_bps: float
    discrepancies: pd.DataFrame
    only_in_primary: tuple[Any, ...]
    only_in_secondary: tuple[Any, ...]
    status: str            # ok | alert


def reconcile_providers(primary: pd.Series, secondary: pd.Series, *, threshold_bps: float = 50.0) -> Reconciliation:
    """Compare the same series from two providers on their common dates. A discrepancy above `threshold_bps` basis
    points is listed. Dates held by only one provider are reported and never filled from the other."""
    both = primary.index.intersection(secondary.index)
    a, b = primary.loc[both].astype(float), secondary.loc[both].astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        bps = ((a - b).abs() / b.abs().replace(0, np.nan)) * 10_000.0
    flagged = pd.DataFrame({"primary": a, "secondary": b, "discrepancy_bps": bps})
    flagged = flagged[flagged["discrepancy_bps"] > threshold_bps]
    maximum = float(bps.max()) if len(bps) and bps.notna().any() else 0.0
    return Reconciliation(len(both), maximum, flagged, tuple(primary.index.difference(secondary.index)),
                          tuple(secondary.index.difference(primary.index)), "alert" if len(flagged) else "ok")


def check_price_series(frame: pd.DataFrame, *, max_move: float = 0.30) -> list[QualityFinding]:
    """Daily checks of a price frame: non-positive prices, duplicated timestamps, a close-to-close move above `max_move`
    (30 percent by default, to be verified against corporate actions), zero volume."""
    findings: list[QualityFinding] = []
    close = frame["close"].astype(float) if "close" in frame else pd.Series(dtype=float)
    if (close <= 0).any():
        findings.append(QualityFinding("non_positive_price", "critical", "zero or negative price", str(close[close <= 0].index[0])))
    if frame.index.has_duplicates:
        findings.append(QualityFinding("duplicate_timestamp", "alert", "duplicated timestamps", str(frame.index[frame.index.duplicated()][0])))
    moves = close.pct_change().abs()
    for stamp in moves[moves > max_move].index:
        findings.append(QualityFinding("large_move", "alert", f"close moved {moves[stamp]:.1%} without a verified corporate action", str(stamp)))
    if "volume" in frame:
        zero = frame.index[frame["volume"].astype(float) == 0]
        for stamp in zero[:1]:
            findings.append(QualityFinding("zero_volume", "warning", "zero volume on a session: halt or data gap", str(stamp)))
    return findings


def check_universe_coverage(expected: Iterable[str], received: Iterable[str], *, critical_share: float = 0.05) -> list[QualityFinding]:
    """Securities expected but not received. More than `critical_share` of the universe missing is critical."""
    expected_set, received_set = set(expected), set(received)
    missing = sorted(expected_set - received_set)
    if not missing:
        return []
    share = len(missing) / max(len(expected_set), 1)
    return [QualityFinding("universe_missing", "critical" if share > critical_share else "warning",
                           f"{len(missing)} of {len(expected_set)} securities missing ({share:.1%})", ",".join(missing[:10]))]


def check_fundamental_record(record: Mapping[str, Any], *, previous_market_cap: float | None = None) -> list[QualityFinding]:
    """Reasonableness checks of one fundamentals record."""
    findings: list[QualityFinding] = []
    pe = _number(record.get("price_to_earnings_ratio"))
    if pe is not None and (pe < 0 or pe > 100 or pe == 0):
        findings.append(QualityFinding("pe_out_of_range", "warning", f"P/E {pe:.2f} is negative, zero or above 100", "price_to_earnings_ratio"))
    revenue = _number(record.get("revenue"))
    if revenue is not None and revenue < 0:
        findings.append(QualityFinding("negative_revenue", "alert", "negative revenue is usually a data error", "revenue"))
    equity = _number(record.get("shareholders_equity"))
    if equity is not None and equity < 0:
        findings.append(QualityFinding("negative_equity", "warning", "negative equity: possible, verify", "shareholders_equity"))
    cap, before = _number(record.get("market_cap")), previous_market_cap
    if cap is not None and before and abs(cap / before - 1) > 0.5:
        findings.append(QualityFinding("market_cap_jump", "alert", "market cap changed by more than 50 percent", "market_cap"))
    return findings


CHECKLISTS: dict[str, tuple[str, ...]] = {
    "daily": (
        "expected number of securities received compared with the prior day",
        "no security with a zero or negative price",
        "volume is plausible: not zero for liquid securities, not a hundred times its average",
        "timestamps are for today, not stale",
        "corporate actions applied against the announcement calendar",
    ),
    "weekly": (
        "cross-provider price reconciliation, discrepancies above 50 basis points listed",
        "new and delisted symbols handled",
        "fundamentals updated for recent filings",
        "universe membership changes applied",
    ),
    "quarterly": (
        "full universe integrity: every expected security present",
        "survivorship check: delisted securities kept in history",
        "point-in-time integrity: no fundamental backdated",
        "total return reconciled against a known index",
    ),
}


def checklist(period: str) -> tuple[str, ...]:
    """The checks of a period: `daily`, `weekly` or `quarterly`."""
    if period not in CHECKLISTS:
        raise ValueError(f"unknown period {period!r}; expected one of {sorted(CHECKLISTS)}")
    return CHECKLISTS[period]
