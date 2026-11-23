"""Schedule expressions: at, every and cron with an IANA zone, and daylight saving (SPEC-018 B7: OA-33). Values computed by hand."""
from datetime import datetime, timezone

import pytest

from hokma.scheduler import schedule as sc

UTC = timezone.utc
NY = "America/New_York"


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def nxt(kind, expr, tz, after):
    return sc.next_run(kind, expr, tz, at(after))


# ---------------------------------------------------------------------------------------------- at and every

def test_at_is_a_one_time_instant_with_its_own_offset_or_the_zone_of_the_job():
    assert nxt("at", "2026-11-06T07:30:00-05:00", "UTC", "2026-11-05T00:00") == at("2026-11-06T12:30")     # the offset wins
    assert nxt("at", "2026-11-06T12:30:00Z", NY, "2026-11-05T00:00") == at("2026-11-06T12:30")
    assert nxt("at", "2026-11-06T07:30", NY, "2026-11-05T00:00") == at("2026-11-06T12:30")                # EST is UTC-5
    assert nxt("at", "2026-07-06T07:30", NY, "2026-07-05T00:00") == at("2026-07-06T11:30")                # EDT is UTC-4
    assert nxt("at", "2026-11-06T12:30:00Z", NY, "2026-11-06T12:30") is None                              # not in the past, not now
    assert sc.stored_kind("at") == "once" and sc.normalize("at", " 2026-11-06T07:30 ", "UTC") == "2026-11-06T07:30"


@pytest.mark.parametrize("text", ["tomorrow", "2026-11-06", "2026-11-06T7:30", "2026-13-06T07:30", "2026-11-06T07:30:00+5"])
def test_at_refuses_what_is_not_a_complete_timestamp(text):
    with pytest.raises(sc.ScheduleError) as err:
        sc.normalize("at", text, "UTC")
    assert err.value.code == "schedule_invalid"


def test_every_counts_from_the_last_run_and_takes_compound_durations():
    assert nxt("every", "30m", "UTC", "2026-11-05T00:00") == at("2026-11-05T00:30")
    assert nxt("every", "1h30m", "UTC", "2026-11-05T00:00") == at("2026-11-05T01:30")
    assert nxt("every", "5m30s", "UTC", "2026-11-05T00:00") == at("2026-11-05T00:05:30")                  # 330 seconds
    assert sc.normalize("every", "1h30m", "UTC") == "90m" and sc.normalize("every", "5M30S", "UTC") == "5m30s"
    assert sc.normalize("every", "24h", "UTC") == "1d" and sc.stored_kind("every") == "interval"
    for text, code in (("4m", "interval_out_of_range"), ("4m59s", "interval_out_of_range"), ("32d", "interval_out_of_range"),
                       ("1h-5m", "schedule_invalid"), ("m", "schedule_invalid")):
        with pytest.raises(sc.ScheduleError) as err:
            sc.normalize("every", text, "UTC")
        assert err.value.code == code


# ---------------------------------------------------------------------------------------------- cron

def test_cron_in_an_iana_zone():
    # 09:00 Monday to Friday in Bogota (UTC-5, no daylight saving); Friday 2026-11-06 15:00 UTC is 10:00 there: next is Monday the 9th
    assert nxt("cron", "0 9 * * mon-fri", "America/Bogota", "2026-11-06T15:00") == at("2026-11-09T14:00")
    assert nxt("cron", "0 9 * * 1-5", "America/Bogota", "2026-11-06T13:00") == at("2026-11-06T14:00")   # 08:00 there: today at 09:00
    assert nxt("cron", "*/15 * * * *", "UTC", "2026-11-06T10:07") == at("2026-11-06T10:15")
    assert nxt("cron", "*/15 * * * *", "UTC", "2026-11-06T10:15") == at("2026-11-06T10:30")                # strictly after
    assert nxt("cron", "5,35 8-9 * * *", "UTC", "2026-11-06T08:36") == at("2026-11-06T09:05")
    assert nxt("cron", "0 12 * * 7", "UTC", "2026-11-06T00:00") == at("2026-11-08T12:00")                   # 7 is Sunday: the 8th
    assert nxt("cron", "0 12 * * sun", "UTC", "2026-11-06T00:00") == at("2026-11-08T12:00")
    assert nxt("cron", "0 0 1 jan *", "UTC", "2026-11-22T00:00") == at("2027-01-01T00:00")
    assert nxt("cron", "0 0 29 2 *", "UTC", "2026-11-22T00:00") == at("2028-02-29T00:00")                   # the next leap day
    assert sc.stored_kind("cron") == "cron" and sc.normalize("cron", "  0   9 * *  MON-FRI ", "UTC") == "0 9 * * mon-fri"


def test_when_day_and_weekday_are_both_restricted_either_one_fires():
    # the 13th OR a Friday: from Monday 2026-11-02 the first is Friday the 6th
    assert nxt("cron", "0 0 13 * fri", "UTC", "2026-11-02T00:00") == at("2026-11-06T00:00")
    # a day field that starts with a star is AND-ed with the weekday: odd days (1, 3, 5...) that are Fridays (6, 13, 20, 27): the 13th
    assert nxt("cron", "0 0 */2 * fri", "UTC", "2026-11-02T00:00") == at("2026-11-13T00:00")


@pytest.mark.parametrize("expr", ["", "* * * *", "0 9 * * * *", "61 * * * *", "0 24 * * *", "0 9 32 * *", "0 9 * 13 *", "0 9 * * 8",
                                  "a b c d e", "0 9 * * funday", "5-1 * * * *", "*/0 * * * *", "0 9 31 2 *", "0 0 30 2 *"])
def test_an_invalid_cron_is_schedule_invalid(expr):
    with pytest.raises(sc.ScheduleError) as err:
        sc.normalize("cron", expr, "UTC")
    assert err.value.code == "schedule_invalid"


def test_a_cron_that_fires_more_often_than_every_five_minutes_is_refused():
    for expr in ("* * * * *", "*/2 * * * *", "0,3 * * * *", "0-4 9 * * *"):
        with pytest.raises(sc.ScheduleError) as err:
            sc.normalize("cron", expr, "UTC")
        assert err.value.code == "interval_out_of_range", expr
    assert sc.normalize("cron", "*/5 * * * *", "UTC") == "*/5 * * * *"
    with pytest.raises(sc.ScheduleError) as err:
        sc.normalize("cron", "0 9 * * *", "Mars/Base")
    assert err.value.code == "timezone_invalid"


# ---------------------------------------------------------------------------------------------- daylight saving

def test_the_time_the_clock_skips_runs_once_at_the_shifted_instant():
    # 2026-03-08: clocks go from 02:00 to 03:00 in New York. 02:30 does not exist that day: 02:30 at the offset before the change (UTC-5)
    # is 07:30 UTC, which is 03:30 on the clock. It runs once; the day after it is 02:30 at UTC-4 = 06:30 UTC.
    first = nxt("cron", "30 2 * * *", NY, "2026-03-07T12:00")
    assert first == at("2026-03-08T07:30")
    assert sc.next_run("cron", "30 2 * * *", NY, first) == at("2026-03-09T06:30")
    assert nxt("daily", "02:30", NY, "2026-03-07T12:00") == at("2026-03-08T07:30")
    assert nxt("at", "2026-03-08T02:30", NY, "2026-03-07T12:00") == at("2026-03-08T07:30")
    # a cron for 02:30 and 03:30 gives one instant that day, not two runs
    both = nxt("cron", "30 2,3 * * *", NY, "2026-03-07T12:00")
    assert both == at("2026-03-08T07:30") and sc.next_run("cron", "30 2,3 * * *", NY, both) == at("2026-03-09T06:30")


def test_the_time_the_clock_repeats_runs_once_the_first_time():
    # 2026-11-01: clocks go from 02:00 back to 01:00. 01:30 happens twice: 05:30 UTC (UTC-4) and 06:30 UTC (UTC-5). Only the first runs.
    first = nxt("cron", "30 1 * * *", NY, "2026-10-31T12:00")
    assert first == at("2026-11-01T05:30")
    assert sc.next_run("cron", "30 1 * * *", NY, first) == at("2026-11-02T06:30")        # not the second 01:30 of the 1st
    assert nxt("daily", "01:30", NY, "2026-10-31T12:00") == at("2026-11-01T05:30")
    assert sc.next_run("daily", "01:30", NY, first) == at("2026-11-02T06:30")
    # 09:00 keeps its local hour across the change: 13:00 UTC before it, 14:00 UTC after
    assert nxt("cron", "0 9 * * *", NY, "2026-10-31T14:00") == at("2026-11-01T14:00")
    assert nxt("cron", "0 9 * * *", NY, "2026-10-31T12:00") == at("2026-10-31T13:00")
    assert nxt("cron", "0 9 * * *", NY, "2026-10-31T13:00") == at("2026-11-01T14:00")


def test_a_weekly_cron_follows_the_local_clock_across_the_change():
    # Monday-Friday 08:30 New York: Friday 2026-03-06 (UTC-5) is 13:30 UTC; Monday 2026-03-09 (UTC-4) is 12:30 UTC
    assert nxt("cron", "30 8 * * mon-fri", NY, "2026-03-06T13:30") == at("2026-03-09T12:30")


# ---------------------------------------------------------------------------------------------- one line

def test_parse_expression_reads_kind_expression_and_zone():
    assert sc.parse_expression("cron 30 9 * * mon-fri tz=America/New_York") == sc.Parsed("cron", "30 9 * * mon-fri", NY)
    assert sc.parse_expression("every 2h", "UTC") == sc.Parsed("interval", "2h", "UTC")
    assert sc.parse_expression("at 2026-11-06T07:30:00Z tz=Europe/Madrid") == sc.Parsed("once", "2026-11-06T07:30:00Z", "Europe/Madrid")
    assert sc.parse_expression("weekly mon-fri 07:30", NY) == sc.Parsed("weekly", "mon,tue,wed,thu,fri 07:30", NY)
    for text, code in (("bogus 1", "kind_invalid"), ("cron 61 * * * * tz=UTC", "schedule_invalid"), ("cron", "schedule_invalid"),
                       ("daily 09:00 tz=Mars/Base", "timezone_invalid"), ("", "schedule_invalid"), ("cron " + "1 " * 100, "schedule_invalid")):
        with pytest.raises(sc.ScheduleError) as err:
            sc.parse_expression(text)
        assert err.value.code == code, text
