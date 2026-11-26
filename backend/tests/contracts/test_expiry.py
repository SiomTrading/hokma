"""Expiration cycles, rules and calendar utilities (SPEC-020: L-04, L-05, L-06, L-55). Expected dates are written by hand."""
from datetime import date, time

import pytest

from hokma.contracts import expiry
from hokma.contracts.identity import NoContract


def test_cycles_of_each_root():
    assert expiry.cycles("NQ") == expiry.cycles("ES") == (3, 6, 9, 12)
    assert expiry.cycles("GC") == (2, 4, 6, 8, 10, 12)
    assert expiry.cycles("SI") == (3, 5, 7, 9, 12)
    assert expiry.cycles("CL") == tuple(range(1, 13))
    assert expiry.listed_months("NQ", date(2025, 10, 15), date(2026, 4, 1)) == (date(2025, 12, 1), date(2026, 3, 1))


def test_nq_december_2025_expires_on_the_third_friday_at_the_open():
    out = expiry.rules("NQ").expiry(2025, 12)
    assert (out.day, out.time_et) == (date(2025, 12, 19), time(9, 30))
    assert expiry.rules("ES").expiry(2026, 3).day == date(2026, 3, 20)
    assert expiry.rules("MNQ").expiry(2025, 12).day == expiry.rules("NQ").expiry(2025, 12).day


def test_gold_expires_on_the_third_last_business_day():
    # December 2025 ends on Wednesday the 31st: 31 is the last, 30 the second last, 29 the third last
    assert expiry.rules("GC").expiry(2025, 12).day == date(2025, 12, 29)
    # November 2025 ends on Sunday: Friday 28 is the last, Wednesday 26 the third last (Thursday 27 is a holiday)
    assert expiry.rules("GC").expiry(2025, 11).day == date(2025, 11, 25)


def test_crude_oil_moves_with_the_christmas_closure():
    # January 2026: the 25th of December 2025 is a closure, so one more business day back from it: 24, 23, 22, 19
    assert expiry.rules("CL").expiry(2026, 1).day == date(2025, 12, 19)
    # February 2026: the 25th of January 2026 is a Sunday; counting 3 business days back from it: Fri 23, Thu 22, Wed 21 ... one more
    assert expiry.rules("CL").expiry(2026, 2).day == date(2026, 1, 20)
    assert expiry.rules("MCL").expiry(2026, 1).day == date(2025, 12, 18)


def test_a_month_outside_the_cycle_is_no_contract_of_a_quarterly_root_and_moves_forward_for_mgc():
    with pytest.raises(NoContract):
        expiry.rules("NQ").expiry(2025, 11)
    assert expiry.rules("MGC").expiry(2025, 11).day == expiry.rules("MGC").expiry(2025, 12).day
    assert expiry.rules("GC").expiry(2025, 11).day == date(2025, 11, 25)        # gold lists every month near the front


def test_an_unknown_root_and_a_bad_month_are_refused():
    with pytest.raises(NoContract):
        expiry.rules("XYZ")
    with pytest.raises(ValueError):
        expiry.rules("NQ").expiry(2025, 13)


@pytest.mark.parametrize("year,month,expected", [(2025, 1, 17), (2025, 2, 21), (2025, 3, 21), (2025, 4, 18), (2025, 5, 16)])
def test_third_friday_of_five_months_written_by_hand(year, month, expected):
    assert expiry.third_friday(year, month) == date(2025, month, expected)


def test_a_holiday_moves_the_third_friday_back_one_business_day():
    assert expiry.third_friday(2025, 12, {date(2025, 12, 19)}) == date(2025, 12, 18)
    assert expiry.third_friday(2025, 12, {date(2025, 12, 18)}) == date(2025, 12, 19)


def test_business_days_skip_weekends_and_holidays():
    thanksgiving = {date(2025, 11, 27)}
    assert expiry.business_days(date(2025, 11, 24), date(2025, 11, 30), thanksgiving) == (
        date(2025, 11, 24), date(2025, 11, 25), date(2025, 11, 26), date(2025, 11, 28))
    assert expiry.business_days(date(2025, 11, 24), date(2025, 11, 30)) == (
        date(2025, 11, 24), date(2025, 11, 25), date(2025, 11, 26), date(2025, 11, 27), date(2025, 11, 28))
    assert expiry.business_days(date(2025, 11, 30), date(2025, 11, 1)) == ()


def test_a_holiday_moves_the_business_day():
    assert expiry.add_business_days(date(2025, 11, 26), 1) == date(2025, 11, 27)
    assert expiry.add_business_days(date(2025, 11, 26), 1, {date(2025, 11, 27)}) == date(2025, 11, 28)
    assert expiry.add_business_days(date(2025, 11, 28), -1, {date(2025, 11, 27)}) == date(2025, 11, 26)
    assert expiry.add_business_days(date(2025, 11, 21), 1) == date(2025, 11, 24)
    assert expiry.nth_last_business_day(2025, 11, 1) == date(2025, 11, 28)
    assert expiry.nth_last_business_day(2025, 11, 2, {date(2025, 11, 28)}) == date(2025, 11, 26)


def test_front_by_open_interest_changes_the_day_the_next_contract_exceeds():
    a, b = "FUT:NQ:2025-12", "FUT:NQ:2026-03"
    d = [date(2025, 12, n) for n in (1, 2, 3, 4, 5)]
    interest = {a: dict(zip(d, (100, 90, 70, 50, 200))), b: dict(zip(d, (20, 60, 80, 120, 100)))}
    assert expiry.front_by_open_interest(interest, [a, b]) == {d[0]: a, d[1]: a, d[2]: b, d[3]: b, d[4]: b}    # never moves back
    with pytest.raises(ValueError):
        expiry.front_by_open_interest(interest, [])
