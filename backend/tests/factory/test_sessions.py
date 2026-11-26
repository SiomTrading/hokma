"""Session filters (SPEC-020: V-25). The schedule is a parameter: this one is written by hand for the test."""
from datetime import date, datetime, time

import pandas as pd
import pytest

from hokma.factory.orders import buy, reduce_intent
from hokma.factory.sessions import BadSession, SessionSchedule, filter_session, in_session, session_gate, session_mask

NY = "America/New_York"
SCHEDULE = SessionSchedule(NY, time(9, 30), time(16, 0), time(17, 0), time(18, 0), holidays=frozenset({date(2025, 11, 27)}),
                           early_closes={date(2025, 11, 28): time(13, 0)})


def at(text: str) -> datetime:
    return pd.Timestamp(text, tz=NY).to_pydatetime()


@pytest.mark.parametrize("moment, rth, eth", [
    ("2025-03-05 09:29", False, True),            # Wednesday, one minute before the open
    ("2025-03-05 09:30", True, True),
    ("2025-03-05 15:59", True, True),
    ("2025-03-05 16:00", False, True),
    ("2025-03-05 17:00", False, False),           # daily break
    ("2025-03-05 17:59", False, False),
    ("2025-03-05 18:00", False, True),            # the evening session reopens
    ("2025-03-05 03:00", False, True),            # overnight
    ("2025-03-08 12:00", False, False),           # Saturday
    ("2025-03-09 17:59", False, False),           # Sunday before the reopen
    ("2025-03-09 18:00", False, True),
    ("2025-03-07 16:59", False, True),            # Friday before the break
    ("2025-03-07 17:00", False, False),           # Friday: closed until Sunday evening
    ("2025-11-27 10:00", False, False),           # holiday
    ("2025-11-28 12:59", True, True),             # reduced day
    ("2025-11-28 13:00", False, False),           # closes at 13:00 and stays closed until the evening
    ("2025-11-28 14:00", False, False),
])
def test_rth_and_eth_by_hand(moment, rth, eth):
    assert in_session(at(moment), SCHEDULE, "RTH") is rth
    assert in_session(at(moment), SCHEDULE, "ETH") is eth


def test_a_utc_index_is_read_on_the_exchange_clock():
    # 14:30 UTC on 5 March 2025 is 09:30 in New York (standard time). On 12 March daylight time is on: 13:30 UTC is 09:30.
    index = pd.DatetimeIndex(["2025-03-05 14:29", "2025-03-05 14:30", "2025-03-12 13:29", "2025-03-12 13:30"], tz="UTC")
    assert list(session_mask(index, SCHEDULE, "RTH")) == [False, True, False, True]


def test_filter_session_keeps_only_regular_hours():
    index = pd.date_range("2025-03-05 09:28", periods=5, freq="min", tz=NY)
    frame = pd.DataFrame({"close": range(5)}, index=index)
    assert list(filter_session(frame, SCHEDULE, "RTH")["close"]) == [2, 3, 4]


def test_a_strategy_with_rth_does_not_open_outside_but_may_exit():
    entry, leave = buy("A", 1), reduce_intent("A", "buy", 1)
    kept, notes = session_gate([entry, leave], at("2025-03-05 09:29"), SCHEDULE, "RTH")
    assert kept == (leave,) and notes == ("entry_outside_rth:1",)
    kept, notes = session_gate([entry, leave], at("2025-03-05 09:30"), SCHEDULE, "RTH")
    assert kept == (entry, leave) and notes == ()
    assert session_gate([entry], at("2025-03-05 03:00"), SCHEDULE, None) == ((entry,), ())


def test_bad_schedule_and_naive_times():
    with pytest.raises(BadSession):
        SessionSchedule("Nowhere/City", time(9, 30), time(16, 0))
    with pytest.raises(BadSession):
        SessionSchedule(NY, time(16, 0), time(9, 30))
    with pytest.raises(BadSession):
        SessionSchedule(NY, time(9, 30), time(16, 0), early_closes={date(2025, 11, 28): time(17, 0)})
    with pytest.raises(BadSession, match="time zone"):
        session_mask(pd.DatetimeIndex(["2025-03-05 10:00"]), SCHEDULE, "RTH")
    with pytest.raises(BadSession):
        session_mask(pd.DatetimeIndex(["2025-03-05 10:00"], tz=NY), SCHEDULE, "NIGHT")  # type: ignore[arg-type]
