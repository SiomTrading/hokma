"""Schedule expressions and next run times."""
from datetime import datetime, timezone

import pytest

from hokma.scheduler import schedule as sc

UTC = timezone.utc


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_daily_in_a_timezone():
    # 07:30 in Bogota (UTC-5) is 12:30 UTC
    assert sc.next_run("daily", "07:30", "America/Bogota", at("2026-11-05T10:00")) == at("2026-11-05T12:30")
    assert sc.next_run("daily", "07:30", "America/Bogota", at("2026-11-05T12:30")) == at("2026-11-06T12:30")


def test_weekly_range_and_list():
    # 2026-11-06 is a Friday
    assert sc.next_run("weekly", "mon-fri 07:30", "UTC", at("2026-11-06T08:00")) == at("2026-11-09T07:30")
    assert sc.next_run("weekly", "sat,sun 09:00", "UTC", at("2026-11-06T08:00")) == at("2026-11-07T09:00")
    assert sc.normalize("weekly", "Fri, mon 7:05", "UTC") == "mon,fri 07:05"


def test_once_and_interval():
    assert sc.next_run("once", "2026-11-06T07:30", "UTC", at("2026-11-05T00:00")) == at("2026-11-06T07:30")
    assert sc.next_run("once", "2026-11-06T07:30", "UTC", at("2026-11-07T00:00")) is None
    assert sc.next_run("interval", "30m", "UTC", at("2026-11-05T00:00")) == at("2026-11-05T00:30")
    assert sc.normalize("interval", "120m", "UTC") == "2h"
    assert sc.normalize("interval", "1440m", "UTC") == "1d"


@pytest.mark.parametrize("kind,expr,tz,code", [
    ("daily", "25:00", "UTC", "schedule_invalid"), ("daily", "7", "UTC", "schedule_invalid"),
    ("weekly", "funday 07:00", "UTC", "schedule_invalid"), ("interval", "1m", "UTC", "interval_out_of_range"),
    ("interval", "99d", "UTC", "interval_out_of_range"), ("interval", "abc", "UTC", "schedule_invalid"),
    ("once", "tomorrow", "UTC", "schedule_invalid"), ("daily", "07:00", "Mars/Base", "timezone_invalid"),
    ("cron", "* * * * *", "UTC", "kind_invalid")])
def test_invalid(kind, expr, tz, code):
    with pytest.raises(sc.ScheduleError) as err:
        sc.normalize(kind, expr, tz)
    assert err.value.code == code
