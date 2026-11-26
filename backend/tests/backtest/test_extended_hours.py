"""Orders in the extended session of futures (SPEC-020: L-26). The session gate that the simulator asks before it sends a stop or a limit:
a stop of NQ at 03:00 New York time is sent in the extended session, and with `session="RTH"` it is not sent outside the regular one."""
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest
from backtest.session_support import run, session_bars
from backtest.support import requires_engine

from hokma.backtest.fills import Decision
from hokma.backtest.spec import SpecError
from hokma.contracts import sessions

NY = ZoneInfo("America/New_York")
MONDAY_03 = datetime(2025, 11, 10, 3, 0, tzinfo=NY)


def test_a_stop_of_nq_at_three_in_the_morning_goes_out_in_the_extended_session():
    assert sessions.state("NQ", MONDAY_03) == "extended"
    assert sessions.is_open("NQ", MONDAY_03, session="ETH") and sessions.is_open("NQ", MONDAY_03, session=None)


def test_with_rth_it_is_not_sent_outside_the_regular_session():
    assert not sessions.is_open("NQ", MONDAY_03, session="RTH")
    assert not sessions.is_open("NQ", datetime(2025, 11, 10, 9, 29, 59, tzinfo=NY), session="RTH")
    assert sessions.is_open("NQ", datetime(2025, 11, 10, 9, 30, tzinfo=NY), session="RTH")
    assert not sessions.is_open("NQ", datetime(2025, 11, 10, 17, 0, tzinfo=NY), session="RTH")


def test_an_order_in_the_daily_break_is_sent_in_neither_session():
    at = datetime(2025, 11, 10, 17, 30, tzinfo=NY)
    assert not sessions.is_open("NQ", at, session="ETH") and not sessions.is_open("NQ", at, session="RTH")


@requires_engine
def test_a_stop_of_nq_at_three_in_the_morning_fills_in_the_extended_session_L_26():
    # Real bars of 2025-11-10 from 02:58 to 03:07 New York time. Decided after the close of 02:59 (07:59 UTC) the long fills at the open of
    # 03:00 (25490.00) plus a tick = 25490.25; the stop is 5 points under the open, 25485.00, and the 03:00 bar reaches 25485.00 (low),
    # so the stop fills in the same bar at 25485.00 less a tick = 25484.75: (25484.75 - 25490.25) * 20 = -110.00, less 4.50 = -114.50.
    bars = session_bars("night")
    plan = {"2025-11-10T07:59:00Z": [Decision("enter", "long", stop_points=D(5))]}
    out = run("", plan, bars=bars, protective=True, session="ETH", root="NQ")
    assert [(f.kind, f.price) for f in out.fills] == [("entry", D("25490.25")), ("stop", D("25484.75"))]
    assert out.realized_pnl_usd == D("-114.50") and out.rejected == {"reject_prob": 0}
    # with no session set the order goes the same way
    assert run("", plan, bars=bars, protective=True, root="NQ").realized_pnl_usd == D("-114.50")


@requires_engine
def test_with_rth_an_order_is_not_sent_outside_the_regular_session_L_26():
    bars = session_bars("night")
    plan = {"2025-11-10T07:59:00Z": [Decision("enter", "long", stop_points=D(5))]}
    out = run("", plan, bars=bars, protective=True, session="RTH", root="NQ")
    assert out.fills == () and out.trades == () and out.rejected == {"reject_prob": 0, "outside_session": 1}
    with pytest.raises(SpecError, match="root"):
        run("", plan, bars=bars, protective=True, session="RTH")


@requires_engine
def test_the_real_overnight_stop_of_the_scenario_t2_fills_in_the_extended_session_L_26():
    # T2 is 00:03 to 00:07 New York time: -114.50 on the long, and nothing is sent with session RTH
    plan = {"2025-11-05T05:04:00Z": [Decision("enter", "long", stop_points=D(5), target_points=D(5))]}
    eth = run("T2", plan, protective=True, session="ETH", root="NQ")
    assert eth.realized_pnl_usd == D("-114.50")
    assert run("T2", plan, protective=True, session="RTH", root="NQ").fills == ()
    assert len(eth.priced) == 2
