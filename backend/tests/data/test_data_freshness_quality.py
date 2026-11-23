"""Freshness and quality (SPEC-017: OA-58, TA-62, TA-63, OA-59, ECT-067)."""
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from hokma.data import freshness, quality
from hokma.data.providers import NoDataError

from .support import when


# ------------------------------------------------------------------------------------------------ OA-58
def test_freshness_of_an_instant_series_does_not_claim_a_delay_OA_58() -> None:
    now = when("2024-03-01T15:00:00")
    out = freshness.describe_bar_freshness("2024-03-01T14:30:00Z", False, now=now, earliest="2024-02-01T14:30:00Z")
    assert (out.timestamp_kind, out.record_age_seconds, out.latest_timezone) == ("instant", 1800, "Z")
    assert out.delay_status == "unknown" and out.delay_basis == "insufficient_evidence" and out.delay_estimated_seconds is None
    assert "not feed latency" in out.delay_explanation


def test_freshness_classification_dates_historical_and_empty_OA_58() -> None:
    now = when("2024-03-01T15:00:00")
    delayed = freshness.describe_bar_freshness("2024-03-01T14:30:00+00:00", False, now=now, capability="delayed")
    assert delayed.delay_status == "possible" and delayed.delay_basis == "source_classification"
    daily = freshness.describe_bar_freshness("2024-02-29", False, now=now)
    assert daily.timestamp_kind == "date" and daily.record_age_seconds is None
    history = freshness.describe_bar_freshness("2024-02-29T14:30:00Z", True, now=now)
    assert history.delay_basis == "historical_request" and "not a live delay signal" in history.delay_explanation
    empty = freshness.describe_bar_freshness("", False, now=now)
    assert empty.delay_basis == "no_records" and empty.latest_record_at is None and empty.timestamp_kind == "unknown"
    assert freshness.describe_bar_freshness("2024-03-01T16:00:00Z", False, now=now).record_age_seconds is None   # a future stamp has no age


# ------------------------------------------------------------------------------------------------ TA-62
def _frame(last: str, n: int = 5) -> pd.DataFrame:
    index = pd.date_range(end=last, periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"close": np.arange(1.0, n + 1)}, index=index)


def test_stale_frame_is_rejected_with_the_dates_TA_62() -> None:
    with pytest.raises(NoDataError) as error:
        freshness.reject_stale(_frame("2023-03-01"), "2024-03-01", symbol="ACME")
    assert "2023-03-01" in error.value.detail and "stale" in error.value.detail
    freshness.reject_stale(_frame("2024-02-24"), "2024-03-01", symbol="ACME")                     # 6 days: a long weekend fits
    with pytest.raises(NoDataError):
        freshness.reject_stale(_frame("2024-02-18"), "2024-03-01", symbol="ACME")                 # 12 days
    freshness.reject_stale(_frame("2024-02-18"), "2024-03-01", symbol="ACME", max_stale_days=14)
    freshness.reject_stale(pd.DataFrame(), "2024-03-01", symbol="ACME")                          # empty is the caller's no-data handling


def test_latest_row_date_reads_column_or_index_TA_62() -> None:
    by_column = pd.DataFrame({"Date": ["2024-01-02", "2024-01-05", "bad"], "close": [1, 2, 3]})
    assert freshness.latest_row_date(by_column).date() == date(2024, 1, 5)
    assert freshness.latest_row_date(_frame("2024-02-02")).date() == date(2024, 2, 2)
    assert freshness.latest_row_date(pd.DataFrame({"close": [1]})) is None


# ------------------------------------------------------------------------------------------------ TA-63
def test_cache_serves_only_its_own_day_and_refetches_after_the_ttl_TA_63() -> None:
    now = when("2024-03-01T15:00:00")
    written = when("2024-03-01T14:50:00")
    assert freshness.cache_is_fresh(written, date(2024, 3, 1), now)                               # 10 min old, same day, within 15 min
    assert not freshness.cache_is_fresh(when("2024-03-01T14:30:00"), date(2024, 3, 1), now)       # 30 min old: the partial candle may have closed
    assert freshness.cache_is_fresh(when("2024-03-01T09:00:00"), date(2024, 2, 28), now)          # a past day is final
    assert not freshness.cache_is_fresh(when("2024-02-29T23:59:00"), date(2024, 2, 28), now)       # written on another day
    assert freshness.trading_days_between(date(2024, 3, 1), date(2024, 3, 4)) == 1                # Friday to Monday
    assert freshness.trading_days_between(date(2024, 3, 4), date(2024, 3, 4)) == 0


# ------------------------------------------------------------------------------------------------ OA-59
def test_bad_ohlc_rows_are_excluded_and_reported_OA_59() -> None:
    index = pd.date_range("2024-01-02", periods=5, freq="D", tz="UTC")
    frame = pd.DataFrame({"open": [10, 10, 10, np.nan, 10], "high": [11, 9, 11, 11, 11], "low": [9, 10, 9, 9, 9], "close": [10.5, 10, 10, 10, np.inf]}, index=index)
    clean, report = quality.drop_invalid_bars(frame)
    assert list(clean.index) == [index[0], index[2]]
    assert report.inspected_rows == 5 and report.excluded_rows == 3 and report.reason == "missing_or_invalid_ohlc"
    assert report.latest_excluded_record_at == index[4].isoformat() and report.latest_excluded_fields == ("close",)


def test_valid_rows_have_no_report_reason_OA_59() -> None:
    rows = [{"date": "2024-01-02", "open": 1, "high": 2, "low": 1, "close": 2}]
    report = quality.inspect_bar_quality(rows)
    assert (report.excluded_rows, report.reason, report.latest_excluded_record_at) == (0, None, None)
    assert quality.invalid_ohlc_fields({"open": 1, "high": 2, "low": 1, "close": "x"}) == ["close"]
    assert quality.invalid_ohlc_fields({"open": 1, "high": 1.5, "low": 1, "close": 2}) == ["high"]     # high below the close
    assert quality.invalid_ohlc_fields({"open": 1, "high": 2, "low": 1.5, "close": 1.2}) == ["low"]    # low above the open


# ------------------------------------------------------------------------------------------------ ECT-067
def test_bias_taxonomy_lists_the_eight_biases_with_a_fix_ECT_067() -> None:
    assert [b.name for b in quality.BIASES] == ["survivorship", "look_ahead", "time_period", "selection", "backfill", "reporting", "restatement", "timestamp"]
    assert all(b.what and b.fix for b in quality.BIASES)


def test_reconciliation_flags_discrepancies_and_never_fills_a_missing_date_ECT_067() -> None:
    days = pd.date_range("2024-01-02", periods=4, freq="D")
    primary = pd.Series([100.0, 101.0, 102.0, 103.0], index=days)
    secondary = pd.Series([100.0, 101.2, 102.0], index=days[[0, 1, 2]])                            # 20 bps apart on day 2; day 4 missing
    out = quality.reconcile_providers(primary, secondary, threshold_bps=50)
    assert out.status == "ok" and out.compared == 3 and out.only_in_primary == (days[3],) and out.only_in_secondary == ()
    assert quality.reconcile_providers(primary, secondary, threshold_bps=10).status == "alert"
    assert abs(out.max_discrepancy_bps - 19.8) < 0.1


def test_price_checks_find_the_known_problems_ECT_067() -> None:
    index = pd.date_range("2024-01-02", periods=5, freq="D", tz="UTC")
    frame = pd.DataFrame({"close": [100.0, 140.0, 141.0, -1.0, 142.0], "volume": [10, 10, 0, 10, 10]}, index=index)
    codes = {f.code for f in quality.check_price_series(frame)}
    assert {"non_positive_price", "large_move", "zero_volume"} <= codes
    assert quality.check_price_series(pd.DataFrame({"close": [1.0, 1.01]}, index=index[:2])) == []


def test_universe_and_fundamental_checks_and_checklists_ECT_067() -> None:
    critical = quality.check_universe_coverage([f"S{i}" for i in range(100)], [f"S{i}" for i in range(90)])
    assert critical[0].severity == "critical" and "10 of 100" in critical[0].message
    assert quality.check_universe_coverage(["A", "B"], ["A", "B"]) == []
    assert quality.check_universe_coverage([f"S{i}" for i in range(100)], [f"S{i}" for i in range(98)])[0].severity == "warning"
    codes = {f.code for f in quality.check_fundamental_record({"price_to_earnings_ratio": 250, "revenue": -5, "shareholders_equity": -1, "market_cap": 300}, previous_market_cap=100)}
    assert codes == {"pe_out_of_range", "negative_revenue", "negative_equity", "market_cap_jump"}
    assert [len(quality.checklist(p)) for p in ("daily", "weekly", "quarterly")] == [5, 4, 4]
    with pytest.raises(ValueError):
        quality.checklist("hourly")
