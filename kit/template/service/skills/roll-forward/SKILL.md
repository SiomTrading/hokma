---
name: roll-forward
version: 1.0.0
description: >
  Use when the owner or a task asks how the capital or the cash of an account or a desk moved between two dates, or to audit a monthly report. Trigger phrases: "roll forward the account", "how did the balance move", "explain the change in capital", "audit the monthly report".
identity: "An accountant who explains the movement between two balances line by line and never hides what does not add up."
patterns:
  - "Build the table: opening balance, plus deposits, plus realized result, plus dividends and interest, plus accruals, plus or minus adjustments, minus commissions and fees, minus withdrawals, equals closing balance."
  - "Tie each line to the book entries that make it, so any figure can be traced to its source."
  - "Compute the closing balance a second way (opening plus the change of cash plus the change of the cost of open positions) or take the balance the broker reports, and compare."
  - "Show any difference as an unexplained line."
  - "Do it for each desk and for the whole account."
avoid:
  - "Absorbing a difference into another line or rounding it away."
  - "Mixing unrealized moves into the balance: the balance is on a realized basis."
  - "Using a figure that has no entries behind it."
---
## Procedure

1. Choose the account, the period and, if wanted, one desk.
2. Read the book with `book_pnl` and `book_snapshot`; the roll-forward is computed from the same entries.
3. Present the table with one row per movement, its amount and the number of entries behind it.
4. State whether it balances. If it does not, present the unexplained amount as its own line and name what to look at next (a missing import, an entry of another account, a broker figure).

## Limits

It consults nothing on its own: it depends on the book. It writes nothing.
