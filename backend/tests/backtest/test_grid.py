"""The date grid and the rebalance grid (SPEC-020: TA-80, AH-35), on the real daily sessions of NQ 12-25 (fixtures/nq_session.json: 70 sessions
from Monday 2025-09-15 to Friday 2025-12-19)."""
from datetime import date, datetime, timezone

import pytest
from backtest.session_support import session_bars

from hokma.backtest import grid
from hokma.backtest.spec import SpecError
from hokma.econ_calendar.events import EconEvent

UTC = timezone.utc
FIRST, LAST = date(2025, 9, 15), date(2025, 12, 19)


def sessions_of_fixture() -> list[date]:
    return [b.ts.date() for b in session_bars("daily")]


def test_dt_and_swc_get_a_dense_grid_of_the_sessions_of_the_calendar_TA_80():
    calendar = sessions_of_fixture()
    assert len(calendar) == 70
    for line in ("DT", "SWC"):
        found = grid.date_grid(line, start=FIRST, end=LAST, as_of=date(2026, 1, 31), calendar=calendar, today=date(2026, 2, 1))
        assert found == tuple(calendar)                                    # every session, in order, none made up
    # without a calendar the sessions are the weekdays that the exchange table does not close: for the equity index that is the same 70 days
    # as the real data, Columbus Day, Veterans Day and Thanksgiving (2025-10-13, 2025-11-11, 2025-11-27) included
    table = grid.date_grid("DT", start=FIRST, end=LAST, as_of=LAST, root="NQ", today=date(2026, 2, 1))
    assert table == tuple(calendar) and len(table) == 70


def test_swo_gets_only_the_dates_of_the_events_that_are_sessions_TA_80():
    calendar = sessions_of_fixture()
    events = [EconEvent("e1", "forexfactory", "CPI", "US", "high", datetime(2025, 10, 24, 12, 30, tzinfo=UTC), None, None, None, None, datetime(2025, 10, 1, tzinfo=UTC)),
              EconEvent("e2", "forexfactory", "FOMC", "US", "high", datetime(2025, 10, 29, 18, 0, tzinfo=UTC), None, None, None, None, datetime(2025, 10, 1, tzinfo=UTC))]
    found = grid.date_grid("SWO", start=FIRST, end=LAST, as_of=LAST, calendar=calendar, events=[*events, date(2025, 11, 8), date(2025, 12, 3)], today=date(2026, 2, 1))
    assert found == (date(2025, 10, 24), date(2025, 10, 29), date(2025, 12, 3))     # 2025-11-08 is a Saturday: not a session
    assert grid.date_grid("SWO", start=FIRST, end=LAST, as_of=LAST, calendar=calendar, today=date(2026, 2, 1)) == ()


def test_no_date_goes_past_as_of_or_past_today_TA_80():
    calendar = sessions_of_fixture()
    cut = grid.date_grid("DT", start=FIRST, end=LAST, as_of=datetime(2025, 10, 3, 21, 0, tzinfo=UTC), calendar=calendar, today=date(2026, 2, 1))
    assert cut[0] == FIRST and cut[-1] == date(2025, 10, 3) and len(cut) == 15           # 3 weeks of 5 sessions
    early = grid.date_grid("DT", start=FIRST, end=LAST, as_of=LAST, calendar=calendar, today=date(2025, 9, 19))
    assert early[-1] == date(2025, 9, 19) and len(early) == 5
    assert grid.date_grid("DT", start=FIRST, end=LAST, as_of=date(2025, 9, 1), calendar=calendar, today=date(2026, 2, 1)) == ()
    swo = grid.date_grid("SWO", start=FIRST, end=LAST, as_of=date(2025, 10, 28), calendar=calendar, events=[date(2025, 10, 24), date(2025, 10, 29)],
                         today=date(2026, 2, 1))
    assert swo == (date(2025, 10, 24),)


def test_two_data_providers_give_the_same_grid_for_the_same_symbol_TA_80():
    # the grid reads the calendar of the exchange, never the rows that a provider returned: a provider that prints a Sunday bar and one that
    # misses a day (2025-10-08) get the same grid; what the second one lacks is reported as a gap and not filled
    provider_a = sessions_of_fixture()
    provider_b = [d for d in provider_a if d != date(2025, 10, 8)] + [date(2025, 10, 12)]
    one = grid.date_grid("DT", start=FIRST, end=LAST, as_of=LAST, root="NQ", today=date(2026, 2, 1))
    two = grid.date_grid("DT", start=FIRST, end=LAST, as_of=LAST, root="NQ", today=date(2026, 2, 1))
    assert one == two and 70 == len(one)
    assert grid.gaps(one, provider_a) == ()
    assert grid.gaps(two, provider_b) == (date(2025, 10, 8),)
    assert date(2025, 10, 12) not in two


def test_a_line_without_a_date_grid_and_bad_input_are_refused_TA_80():
    for line in ("LP", "RF"):
        with pytest.raises(SpecError):
            grid.date_grid(line, start=FIRST, end=LAST, as_of=LAST, today=date(2026, 2, 1))
    with pytest.raises(SpecError):
        grid.date_grid("XX", start=FIRST, end=LAST, as_of=LAST)
    with pytest.raises(SpecError):
        grid.date_grid("DT", start=FIRST, end=LAST, as_of=LAST, events=[1], calendar=["x"])


# ---------------------------------------------------------------------------------------------------------------- AH-35
def test_swc_rebalances_weekly_on_the_monday_bar_AH_35():
    days = sessions_of_fixture()
    weekly = grid.rebalance_grid(days, line="SWC")
    assert len(weekly) == 14 and weekly[0] == date(2025, 9, 15) and weekly[-1] == date(2025, 12, 15)    # Mondays from 09-15 to 12-15
    assert all(d.weekday() == 0 and d in days for d in weekly)


def test_a_day_without_a_bar_moves_the_rebalance_to_the_next_bar_AH_35():
    days = sessions_of_fixture()
    without_monday = [d for d in days if d != date(2025, 10, 13)]
    weekly = grid.rebalance_grid(without_monday, "weekly")
    assert date(2025, 10, 13) not in weekly and date(2025, 10, 14) in weekly and len(weekly) == 14      # Tuesday 2025-10-14 takes its place
    assert set(weekly) <= set(without_monday)
    # the week after the data ends is not invented
    assert grid.rebalance_grid(days[:3], "weekly") == (date(2025, 9, 15),)


def test_lp_rebalances_monthly_on_the_first_bar_of_the_month_AH_35():
    days = sessions_of_fixture()
    # 2025-10-01 is a Wednesday with a bar; 2025-11-01 is a Saturday: the next bar is Monday 11-03; 2025-12-01 is a Monday. September 1 is before the data.
    assert grid.rebalance_grid(days, line="LP") == (date(2025, 10, 1), date(2025, 11, 3), date(2025, 12, 1))


def test_swo_rebalances_on_the_day_of_each_event_or_the_next_bar_AH_35():
    days = sessions_of_fixture()
    found = grid.rebalance_grid(days, line="SWO", events=[date(2025, 10, 24), date(2025, 11, 8), date(2025, 11, 9), date(2026, 3, 1)])
    assert found == (date(2025, 10, 24), date(2025, 11, 10))              # the weekend of 11-08 and 11-09 both land on Monday: one rebalance
    assert grid.rebalance_grid([], "weekly") == ()
    with pytest.raises(SpecError):
        grid.rebalance_grid(days)
    with pytest.raises(SpecError):
        grid.rebalance_grid(days, "yearly")  # type: ignore[arg-type]


def test_every_rebalance_date_is_a_date_of_the_real_bars_AH_35():
    bars = session_bars("daily")
    assert all(b.close > 0 for b in bars)
    dates = {b.ts.date() for b in bars}
    for cadence in ("weekly", "monthly"):
        assert set(grid.rebalance_grid(dates, cadence)) <= dates


def test_the_swo_grid_comes_from_the_answer_of_the_economic_calendar_TA_80():
    """The recorded week of Forex Factory (Sunday 2026-10-11 to Saturday 2026-10-17, at 08:30 New York time = 12:30 UTC) gives the event days; the grid of a
    SWO desk holds only those that are sessions and do not pass `as_of`. A calendar that did not answer is refused, and one with no events gives an empty grid."""
    import json
    from pathlib import Path

    from hokma.econ_calendar import events as econ

    body = (Path(__file__).resolve().parents[1] / "econ" / "fixtures" / "forexfactory_week.json").read_text(encoding="utf-8")
    parsed = econ.parse_forex_factory(body, datetime(2026, 10, 15, 6, 0, tzinfo=UTC))
    answer = econ.CalendarResult("ok", tuple(parsed), "forexfactory")
    event_days = sorted({e.scheduled_at.date() for e in parsed})
    sessions = [date(2026, 10, d) for d in range(12, 17)]
    found = grid.date_grid("SWO", start=date(2026, 10, 12), end=date(2026, 10, 16), as_of=date(2026, 10, 16), calendar=sessions, today=date(2026, 10, 20),
                           events=grid.calendar_events(answer))
    assert found == tuple(d for d in sessions if d in event_days) and found
    cut = grid.date_grid("SWO", start=date(2026, 10, 12), end=date(2026, 10, 16), as_of=event_days[0], calendar=sessions, today=date(2026, 10, 20),
                         events=grid.calendar_events(answer))
    assert cut == (event_days[0],) and all(d <= event_days[0] for d in cut)             # nothing after as_of
    assert grid.calendar_events(econ.CalendarResult("NO_EVENTS", (), "forexfactory")) == ()
    with pytest.raises(SpecError, match="events_unverified"):
        grid.calendar_events(econ.CalendarResult("UNVERIFIED_EVENT_RISK", (), None, ("feed_unreachable",)))
    assert json.loads(body)                                                              # the fixture is the recorded answer, not an empty one
