"""Trading hours of the history (SPEC-020: L-03). Every expected value is a time written by hand in New York time."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hokma.contracts import sessions
from hokma.contracts.identity import NoContract

NY = ZoneInfo("America/New_York")


def at(year, month, day, hour, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=NY)


def test_a_normal_day_regular_and_extended():
    assert sessions.state("NQ", at(2025, 11, 10, 10)) == "regular"
    assert sessions.state("NQ", at(2025, 11, 10, 3)) == "extended"
    assert sessions.is_open("NQ", at(2025, 11, 10, 10), session="RTH")
    assert sessions.is_open("NQ", at(2025, 11, 10, 3), session="ETH") and sessions.is_open("NQ", at(2025, 11, 10, 3))
    assert not sessions.is_open("NQ", at(2025, 11, 10, 3), session="RTH")


def test_the_daily_cut_from_17_to_18_is_closed_and_the_edges_belong_to_the_next_state():
    assert sessions.state("NQ", at(2025, 11, 12, 16, 59, 59)) == "regular"
    for hour, minute in ((17, 0), (17, 30), (17, 59)):
        assert sessions.state("NQ", at(2025, 11, 12, hour, minute)) == "closed"
    assert sessions.state("NQ", at(2025, 11, 12, 18)) == "extended"


def test_the_week_starts_sunday_at_18_and_ends_friday_at_17():
    assert sessions.state("ES", at(2025, 11, 16, 17, 59)) == "closed"
    assert sessions.state("ES", at(2025, 11, 16, 18)) == "extended"
    assert sessions.state("ES", at(2025, 11, 14, 16, 59)) == "regular"
    assert sessions.state("ES", at(2025, 11, 14, 17)) == "closed" and sessions.state("ES", at(2025, 11, 14, 19)) == "closed"
    assert not sessions.is_open("ES", at(2025, 11, 15, 12))


def test_a_holiday_with_an_early_close_ends_the_day_at_its_time():
    assert sessions.state("ES", at(2025, 11, 28, 13, 14)) == "regular"
    assert sessions.state("ES", at(2025, 11, 28, 13, 15)) == "closed"
    assert not sessions.is_open("ES", at(2025, 11, 28, 15))
    assert sessions.state("ES", at(2025, 11, 27, 12, 59)) == "regular" and sessions.state("ES", at(2025, 11, 27, 13)) == "closed"
    assert sessions.state("ES", at(2025, 11, 27, 18)) == "extended"             # Thanksgiving evening: the next trading day opens


def test_the_early_close_belongs_to_each_product():
    assert sessions.state("GC", at(2025, 11, 28, 14, 44)) == "regular"
    assert sessions.state("GC", at(2025, 11, 28, 14, 45)) == "closed"
    assert sessions.state("ES", at(2025, 11, 28, 14, 44)) == "closed"


def test_christmas_has_no_regular_session_but_reopens_in_the_evening():
    assert sessions.state("NQ", at(2025, 12, 25, 10)) == "closed"
    assert sessions.state("NQ", at(2025, 12, 25, 18)) == "extended"


def test_a_full_closure_closes_the_whole_date():
    assert sessions.state("NQ", at(2026, 12, 25, 10)) == "closed"
    assert sessions.state("NQ", at(2026, 12, 25, 20)) == "closed"


def test_any_time_zone_is_converted_to_the_exchange_zone():
    utc = ZoneInfo("UTC")
    assert sessions.state("NQ", datetime(2025, 11, 10, 15, 0, tzinfo=utc)) == "regular"        # 10:00 EST
    assert sessions.state("NQ", datetime(2025, 7, 1, 14, 0, tzinfo=utc)) == "regular"          # 10:00 EDT
    assert sessions.state("NQ", datetime(2025, 7, 1, 21, 30, tzinfo=utc)) == "closed"          # 17:30 EDT


def test_business_days_calendar_counts_cut_days_and_the_bank_holidays_that_close_the_product():
    holidays = sessions.holidays("NQ")
    assert datetime(2025, 12, 25).date() in holidays          # no regular session
    assert datetime(2025, 11, 28).date() not in holidays      # an early close is still a business day
    assert datetime(2025, 1, 20).date() in holidays           # a full closure of the product
    # a bank holiday on which the product trades is not a holiday of it: the exchange table of the other families keeps theirs
    for day in (datetime(2025, 10, 13).date(), datetime(2025, 11, 11).date(), datetime(2025, 11, 27).date()):
        assert day not in holidays and day not in sessions.holidays("ES") and day not in sessions.holidays("YM")
        assert day in sessions.holidays("CL") and day in sessions.holidays("ZN") and day in sessions.holidays("GC") and day in sessions.holidays("6E")


def test_the_equity_index_trades_on_columbus_day_veterans_day_and_thanksgiving_morning():
    assert sessions.state("NQ", at(2025, 10, 13, 10)) == "regular" and sessions.state("NQ", at(2025, 11, 11, 10)) == "regular"
    assert sessions.state("NQ", at(2025, 11, 27, 12, 59)) == "regular" and sessions.state("NQ", at(2025, 11, 27, 13)) == "closed"
    assert sessions.state("NQ", at(2025, 11, 27, 18)) == "extended"


def test_the_calendar_of_nq_is_the_real_daily_sessions_of_trading_core_from_september_to_december_2025():
    """fixtures/nq_session.json: 70 real daily sessions of NQ 12-25 read from Trading Core. Every weekday of the window is a session of the
    calendar exactly when the data has one, and the sessions of the three bank holidays are full or partial in the data itself."""
    import json
    from datetime import timedelta
    from pathlib import Path

    fixture = json.loads((Path(__file__).resolve().parents[1] / "backtest" / "fixtures" / "nq_session.json").read_text(encoding="utf-8"))
    real = {b["ts"][:10]: int(b["volume"]) for b in fixture["daily"]["bars"]}
    first, last = datetime(2025, 9, 15).date(), datetime(2025, 12, 19).date()
    weekdays = [first + timedelta(days=n) for n in range((last - first).days + 1) if (first + timedelta(days=n)).weekday() < 5]
    closed = sessions.holidays("NQ")
    assert [d for d in weekdays if (d not in closed) != (d.isoformat() in real)] == []
    assert real["2025-10-13"] > 100_000 and real["2025-11-11"] > 100_000        # full sessions
    assert real["2025-11-27"] < 100_000 < real["2025-11-28"] + 100_000            # Thanksgiving ends at 13:00: a fraction of a normal day
    assert real["2025-11-27"] < real["2025-11-26"] / 5 and real["2025-11-28"] < real["2025-11-26"] / 2


def test_a_naive_time_a_bad_session_and_an_unknown_root_are_refused():
    with pytest.raises(ValueError):
        sessions.state("NQ", datetime(2025, 11, 10, 10))
    with pytest.raises(ValueError):
        sessions.is_open("NQ", at(2025, 11, 10, 10), session="XTH")
    with pytest.raises(NoContract):
        sessions.state("XYZ", at(2025, 11, 10, 10))
