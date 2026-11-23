"""Accrual schedule for fixed income (SPEC-018: AFS-017). Two rules, both by days:

* a T-bill bought at a discount accretes the discount straight line from the purchase date to the maturity date;
* a bond accrues its coupon from the last coupon date: face * rate * days / basis.

For every accrual the amount of the period is the portion earned up to the end of the period minus what the book already holds for the same
instrument, so running the schedule twice never books a day twice. The value of the account does not jump on the payment day: the accrued
amount that was part of the value the day before becomes the cash that arrives, and `value_with_accrued` shows both sides of that day.
Everything is Decimal. Nothing here writes to the book: the amounts are a proposal that the owner books.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from hokma.exec.money import ZERO, MoneyError, dec, text

CENT = Decimal("0.01")
MAX_DAYS = 36_600           # a hundred years: more is a typing error, not an instrument


class AccrualError(ValueError):
    """The dates or amounts of an accrual cannot be used."""


@dataclass(frozen=True)
class Accrual:
    """One accrual. `earned_to_date` is the portion earned from the start to the end of the period, `recorded` what the book already holds,
    `this_period` the difference (never negative: a book that is ahead is reported, not reversed). `support` names every input used."""
    instrument: str
    period_start: str
    period_end: str
    earned_to_date: Decimal
    recorded: Decimal
    this_period: Decimal
    ahead_of_schedule: Decimal
    support: tuple[str, ...]

    def rounded(self) -> Decimal:
        return self.this_period.quantize(CENT, rounding=ROUND_HALF_UP)


def _day(value: date | str, what: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise AccrualError(f"{what} is not a date") from exc


def days_between(start: date | str, end: date | str) -> int:
    a, b = _day(start, "start"), _day(end, "end")
    if b < a:
        raise AccrualError("the end date is before the start date")
    out = (b - a).days
    if out > MAX_DAYS:
        raise AccrualError("the period is longer than a hundred years")
    return out


def portion(total: Decimal, days: int, basis_days: int) -> Decimal:
    """The part of `total` earned in `days` of a basis of `basis_days`: total * days / basis_days."""
    if basis_days <= 0:
        raise AccrualError("the basis must be a positive number of days")
    if days < 0:
        raise AccrualError("days cannot be negative")
    return total * Decimal(days) / Decimal(basis_days)


def _finish(instrument: str, start: str, end: str, earned: Decimal, recorded: Decimal, support: list[str]) -> Accrual:
    gap = earned - recorded
    return Accrual(instrument, start, end, earned, recorded, max(gap, ZERO), max(-gap, ZERO), tuple(support))


def bill_accretion(*, instrument: str, face: Decimal, price_paid: Decimal, purchase: date | str, maturity: date | str, period_start: date | str,
                   period_end: date | str, recorded: Decimal = ZERO) -> Accrual:
    """Straight line accretion of the discount of a bill. `recorded` is the accretion the book holds up to the end of the previous period."""
    face, price, recorded = dec(face, "face"), dec(price_paid, "price_paid"), dec(recorded, "recorded")
    if face <= 0 or price <= 0 or price > face:
        raise AccrualError("a bill needs a positive face and a price paid that is not above it")
    total_days = days_between(purchase, maturity)
    if total_days == 0:
        raise AccrualError("a bill needs at least one day between purchase and maturity")
    start, end, buy, mat = _day(period_start, "period_start"), _day(period_end, "period_end"), _day(purchase, "purchase"), _day(maturity, "maturity")
    if end < start:
        raise AccrualError("the end of the period is before its start")
    elapsed = days_between(buy, min(max(end, buy), mat))
    discount = face - price
    earned = portion(discount, elapsed, total_days)
    support = [f"face {text(face)}", f"price paid {text(price)}", f"discount {text(discount)}", f"{elapsed} of {total_days} days from {buy} to {mat}",
               f"recorded {text(recorded)}"]
    return _finish(instrument, start.isoformat(), end.isoformat(), earned, recorded, support)


def bill_carrying_value(*, face: Decimal, price_paid: Decimal, purchase: date | str, maturity: date | str, on: date | str) -> Decimal:
    """The value of a bill on a day: the price paid plus the discount accreted to that day. On the maturity day it is the face."""
    first = bill_accretion(instrument="bill", face=face, price_paid=price_paid, purchase=purchase, maturity=maturity, period_start=purchase, period_end=on)
    return dec(price_paid) + first.earned_to_date


def coupon_accrual(*, instrument: str, face: Decimal, annual_rate: Decimal, last_coupon: date | str, period_start: date | str, period_end: date | str,
                   basis_days: int = 365, recorded: Decimal = ZERO) -> Accrual:
    """Coupon earned from the last coupon date to the end of the period: face * rate * days / basis. `recorded` is what the book already holds
    since that coupon date, so the amount of the period is only the new days."""
    face, rate, recorded = dec(face, "face"), dec(annual_rate, "annual_rate"), dec(recorded, "recorded")
    if face <= 0 or rate < 0:
        raise AccrualError("a bond needs a positive face and a coupon rate that is not negative")
    start, end, last = _day(period_start, "period_start"), _day(period_end, "period_end"), _day(last_coupon, "last_coupon")
    if end < start or start < last:
        raise AccrualError("the period must start on or after the last coupon date and end after it starts")
    days = days_between(last, end)
    earned = portion(face * rate, days, basis_days)
    support = [f"face {text(face)}", f"rate {text(rate)}", f"{days} days from {last} to {end} over a basis of {basis_days}", f"recorded {text(recorded)}"]
    return _finish(instrument, start.isoformat(), end.isoformat(), earned, recorded, support)


def coupon_amount(face: Decimal, annual_rate: Decimal, payments_per_year: int) -> Decimal:
    if payments_per_year <= 0:
        raise AccrualError("payments per year must be positive")
    return dec(face, "face") * dec(annual_rate, "annual_rate") / Decimal(payments_per_year)


def value_with_accrued(*, cash: Decimal, position_value: Decimal, accrued: Decimal) -> Decimal:
    """Value of the account counting the accrued interest that the position carries."""
    return dec(cash, "cash") + dec(position_value, "position_value") + dec(accrued, "accrued")


def payment_day(*, cash_before: Decimal, position_value: Decimal, accrued_before: Decimal, payment: Decimal) -> dict[str, Decimal]:
    """The two sides of a payment day. Before: cash + position + accrued. After: the payment is cash and the accrued is zero. The two values
    must be the same; `jump` is their difference (zero when the accrual had earned exactly the payment, any other number is reported as it is)."""
    before = value_with_accrued(cash=cash_before, position_value=position_value, accrued=accrued_before)
    after = value_with_accrued(cash=dec(cash_before) + dec(payment, "payment"), position_value=position_value, accrued=ZERO)
    return {"before": before, "after": after, "jump": after - before}


__all__ = ["Accrual", "AccrualError", "MoneyError", "bill_accretion", "bill_carrying_value", "coupon_accrual", "coupon_amount", "days_between", "payment_day",
           "portion", "value_with_accrued"]
