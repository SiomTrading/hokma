"""Accrual schedule of T-bills and bonds (SPEC-018: AFS-017). Values by hand, base by days."""
from decimal import Decimal

import pytest

from hokma.backoffice import accruals
from hokma.backoffice.accruals import AccrualError

D = Decimal


def test_a_bill_accretes_its_discount_straight_line_by_days():
    # face 10000 bought at 9900 on 2026-01-01, matures 2026-04-01: 90 days, discount 100. At 2026-02-15 (45 days) half of it is earned.
    a = accruals.bill_accretion(instrument="BIL:T90", face=D(10000), price_paid=D(9900), purchase="2026-01-01", maturity="2026-04-01",
                                period_start="2026-02-01", period_end="2026-02-15", recorded=D(20))
    assert a.earned_to_date == D(50) and a.recorded == D(20) and a.this_period == D(30) and a.ahead_of_schedule == 0
    assert any("45 of 90 days" in s for s in a.support) and any("discount 100" in s for s in a.support)


def test_the_period_subtracts_what_the_book_already_holds_and_running_twice_books_nothing_new():
    first = accruals.bill_accretion(instrument="b", face=D(10000), price_paid=D(9900), purchase="2026-01-01", maturity="2026-04-01", period_start="2026-02-01",
                                    period_end="2026-02-15", recorded=D(20))
    again = accruals.bill_accretion(instrument="b", face=D(10000), price_paid=D(9900), purchase="2026-01-01", maturity="2026-04-01", period_start="2026-02-01",
                                    period_end="2026-02-15", recorded=D(20) + first.this_period)
    assert again.this_period == 0
    ahead = accruals.bill_accretion(instrument="b", face=D(10000), price_paid=D(9900), purchase="2026-01-01", maturity="2026-04-01", period_start="2026-02-01",
                                    period_end="2026-02-15", recorded=D(60))
    assert ahead.this_period == 0 and ahead.ahead_of_schedule == D(10)


def test_the_value_of_a_bill_reaches_the_face_on_maturity():
    kw = dict(face=D(10000), price_paid=D(9900), purchase="2026-01-01", maturity="2026-04-01")
    assert accruals.bill_carrying_value(on="2026-01-01", **kw) == D(9900)
    assert accruals.bill_carrying_value(on="2026-02-15", **kw) == D(9950)
    assert accruals.bill_carrying_value(on="2026-04-01", **kw) == D(10000)
    assert accruals.bill_carrying_value(on="2026-05-01", **kw) == D(10000)         # after maturity nothing more accretes


def test_a_bond_accrues_its_coupon_by_days_over_the_basis():
    # face 100000 at 5 percent, basis 360: from 2026-01-15 to 2026-04-15 is 90 days, so 100000 * 0.05 * 90 / 360 = 1250.
    a = accruals.coupon_accrual(instrument="BND:X", face=D(100000), annual_rate=D("0.05"), last_coupon="2026-01-15", period_start="2026-03-15",
                                period_end="2026-04-15", basis_days=360, recorded=D(1000))
    assert a.earned_to_date == D(1250) and a.this_period == D(250) and a.rounded() == D("250.00")
    assert any("90 days" in s and "basis of 360" in s for s in a.support)


def test_the_coupon_of_a_period_and_the_basis_are_checked():
    assert accruals.coupon_amount(D(100000), D("0.05"), 2) == D(2500)
    with pytest.raises(AccrualError):
        accruals.coupon_accrual(instrument="x", face=D(100), annual_rate=D("0.05"), last_coupon="2026-04-01", period_start="2026-03-01", period_end="2026-05-01")
    with pytest.raises(AccrualError):
        accruals.portion(D(1), 1, 0)
    with pytest.raises(AccrualError):
        accruals.bill_accretion(instrument="x", face=D(100), price_paid=D(101), purchase="2026-01-01", maturity="2026-02-01", period_start="2026-01-01", period_end="2026-01-02")


def test_the_value_of_the_account_does_not_jump_on_the_payment_day():
    # semiannual coupon of 2500 on 100000 at 5 percent, basis 360 with a 180 day period: on the payment day the accrued amount is the whole coupon.
    accrued = accruals.coupon_accrual(instrument="b", face=D(100000), annual_rate=D("0.05"), last_coupon="2026-01-15", period_start="2026-07-01",
                                      period_end="2026-07-14", basis_days=360).earned_to_date               # 180 days: 100000 * 0.05 * 180 / 360
    assert accrued == D(2500)
    day = accruals.payment_day(cash_before=D(5000), position_value=D(99000), accrued_before=accrued, payment=D(2500))
    assert day["before"] == D(106500) and day["after"] == D(106500) and day["jump"] == 0
    off = accruals.payment_day(cash_before=D(5000), position_value=D(99000), accrued_before=D(2400), payment=D(2500))
    assert off["jump"] == D(100)                                       # a mismatch is reported as it is, never hidden
