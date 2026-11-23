"""Point-in-time rules (SPEC-017: TA-27, TA-64, TA-65, TA-66, TA-67)."""
from datetime import datetime, timezone

import pandas as pd
import pytest

from hokma.data import pit, sentinels

from .support import fixture_json, when

UTC = timezone.utc


@pytest.mark.parametrize("bad", ["2026-9-10", "2026-09-10 00:00", "Sept 10", None, "2026-13-01"])
def test_run_date_rejects_malformed_TA_27(bad) -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        pit.validate_run_date(bad, today="2026-09-10")


def test_run_date_rejects_future_and_accepts_today_TA_27() -> None:
    assert pit.validate_run_date("2026-09-10", today="2026-09-10") == "2026-09-10"
    with pytest.raises(ValueError, match="future"):
        pit.validate_run_date("2026-09-11", today="2026-09-10")


def test_filing_after_as_of_is_hidden_and_before_is_shown_TA_64() -> None:
    rows = fixture_json("fd_financial_metrics.json")["financial_metrics"]
    visible = pit.filter_financials_by_date(rows, when("2024-04-30"))
    filed = [r["filing_date"] for r in visible]
    assert "2024-05-02" not in filed                    # the 10-Q filed 2024-05-02 is not known on 2024-04-30
    assert "2024-02-01" in filed and len(visible) == 5
    assert all(r.get("filing_date") for r in visible)   # the row with no filing date cannot be proven public: excluded
    assert "2024-05-02" in [r["filing_date"] for r in pit.filter_financials_by_date(rows, when("2024-05-02"))]


def test_filter_financials_works_on_a_frame_TA_64() -> None:
    frame = pd.DataFrame(fixture_json("fd_financial_metrics.json")["financial_metrics"])
    out = pit.filter_financials_by_date(frame, when("2023-06-01"))
    assert list(out["filing_date"]) == ["2023-02-02", "2023-05-04"]
    assert pit.filter_financials_by_date(frame.drop(columns=["filing_date"]), when("2023-06-01")).empty


def test_in_window_is_half_open_and_undated_needs_the_present_TA_65() -> None:
    start, end = when("2024-03-01"), when("2024-03-05")
    assert pit.in_window(when("2024-03-01T00:00:00"), start, end)
    assert pit.in_window(when("2024-03-05T23:59:00"), start, end)         # the end day is inside
    assert not pit.in_window(when("2024-03-06T00:00:00"), start, end)     # but not the next midnight
    now = when("2024-06-01T12:00:00")
    assert not pit.in_window(None, start, end, now=now)                   # a historical window cannot prove an undated item
    assert pit.in_window(None, when("2024-05-30"), when("2024-06-01"), now=now)


def test_coverage_gap_says_not_available_when_the_feed_never_observed_the_window_TA_65() -> None:
    now = when("2024-06-01T12:00:00")
    gap = pit.coverage_gap([when("2024-05-30")], "2024-03-01", "2024-03-05", "News feed", "news about ACME", now=now)
    assert gap is not None and gap.reason == sentinels.REASON_NOT_OBSERVED and "not an absence" in gap.message
    assert pit.coverage_gap([when("2024-02-01")], "2024-03-01", "2024-03-05", "News feed", "news", now=now) is None   # coverage reaches the window
    future = pit.coverage_gap([when("2024-02-01")], "2024-05-30", "2024-06-05", "News feed", "news", now=now)
    assert future is not None and "past today" in future.message


def test_as_of_is_fixed_by_the_run_not_the_model_TA_66() -> None:
    assert pit.as_of("2024-03-01", "2024-03-10") == "2024-03-01"
    assert pit.as_of("2024-03-20", "2024-03-10") == "2024-03-10"       # later than the run: clamped
    assert pit.as_of(None, "2024-03-10") == "2024-03-10"
    assert pit.as_of("Sept 10", "2024-03-10") == "2024-03-10"
    assert pit.as_of("2024-03-20", "") == "2024-03-20"                 # outside a run the request passes through


def test_as_of_window_clamps_the_end_and_keeps_the_length_TA_66() -> None:
    assert pit.as_of_window("2024-03-01", "2024-03-05", "2024-03-10") == ("2024-03-01", "2024-03-05")
    assert pit.as_of_window("2024-03-01", "2024-03-20", "2024-03-10") == ("2024-03-01", "2024-03-10")
    assert pit.as_of_window("2024-03-15", "2024-03-20", "2024-03-10") == ("2024-03-05", "2024-03-10")   # wholly after: moved back, 5 days


def test_live_profile_is_withheld_for_a_past_as_of_and_the_result_says_so_TA_67() -> None:
    profile = {"name": "Acme", "sector": "Technology", "market_cap": 2e11}
    past = pit.guard_live_profile(profile, as_of=when("2024-03-01"), label="ACME", source="prov", today="2026-11-22")
    assert past.value is None and past.status == sentinels.NO_DATA and past.reason == sentinels.REASON_LIVE_PROFILE
    assert past.warnings and "withheld" in past.warnings[0] and "2024-03-01" in past.warnings[0]
    live = pit.guard_live_profile(profile, as_of=when("2026-11-22"), label="ACME", source="prov", today="2026-11-22")
    assert live.ok and live.value == profile and live.warnings == ()
    assert pit.withhold_live_profile(None, "ACME") is None
