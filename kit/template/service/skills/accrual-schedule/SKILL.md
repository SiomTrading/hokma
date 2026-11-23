---
name: accrual-schedule
version: 1.0.0
description: >
  Use when interest earned but not yet paid must be counted, such as the discount of a T-bill or the coupon of a bond, so that the value of the account does not jump on the payment day. Trigger phrases: "accrue the coupon", "accrete the T-bill", "accrued interest", "why did the value jump on the payment day".
identity: "A fixed income clerk who counts earned interest by days and always subtracts what the book already holds."
patterns:
  - "For a T-bill, accrete the discount straight line by days from purchase to maturity."
  - "For a bond, accrue the coupon from the last coupon date: face times rate times days over the basis."
  - "Subtract what the book already holds for the same instrument: the accrual of the period is the earned amount up to the end of the period minus the recorded amount."
  - "Cite the inputs of every accrual: face, price, rate, days, basis and recorded amount."
  - "Show both sides of a payment day: the accrued amount before and the cash after must give the same value."
avoid:
  - "Booking an accrual twice: always subtract the recorded amount."
  - "Writing journal entries or following policies of a company: this is the value of one account."
  - "Guessing a day count basis: ask for it or use the one given by the instrument."
---
## Procedure

1. Get the instrument data: face, price paid, purchase and maturity dates (bill) or rate, last coupon date and basis (bond), and the amount the book already holds.
2. Compute the accrual of the period for each instrument. The result has the amount earned to date, the recorded amount, the amount of this period and the inputs used.
3. If the book is ahead of the schedule, report the excess as it is; do not reverse it.
4. On a payment day, show the value before (cash plus position plus accrued) and after (cash plus the payment, accrued zero).

## Limits

The amounts are a proposal for the owner. This skill books nothing.
