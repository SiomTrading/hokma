---
name: ledger-pnl
ref: rec-016
rows: [FT-102, FT-103, FT-104]
lines: [LP, SWC, RF, DT]
summary: Turn a list of fills into positions and profit and loss with average cost and with first in first out, and see why the two disagree.
runnable: true
calls: []
---
# From transactions to positions and profit and loss

## Idea

A portfolio is a list of transactions. The position is what is left, the cost is what it was paid, and the profit of a sale depends on a rule: against the
average cost of the whole position, or against the oldest lots first. The rule changes the realized figure and the cost of what remains, never the total
when everything is closed. A report names its rule.

## Steps

1. One row per fill: contract, signed quantity (buy positive, sell negative), price, multiplier and commission, in the order of the book.
2. Average cost: `hokma.backoffice.cost_basis.reconstruct` keeps one average per contract; a sale books profit against that average and leaves the
   average alone; a cross through zero opens the other side at the fill price.
3. First in first out: `hokma.factory.orders.reduce` closes the lots in order at their own prices and returns what is left.
4. Costs are kept apart so the profit shows gross and net.
5. Period views of the book (a year, a quarter) and the benchmark comparison come from `hokma.backoffice`; a recipe never rebuilds them.

## Example

Three fills of one NQ contract at real opens of the daily fixture: buy 2, buy 1, then sell 2. The expected values are written by hand in the comments.

```python runnable
from decimal import Decimal

from hokma.backoffice import cost_basis
from hokma.factory.orders import Lot, reduce

open_ = [b.open for b in BARS_DAILY]
M = Decimal(20)
fills = [
    {"contract_id": "NQ", "qty": Decimal(2), "price": open_[2], "multiplier": M, "commission": Decimal("4.50")},
    {"contract_id": "NQ", "qty": Decimal(1), "price": open_[5], "multiplier": M, "commission": Decimal("2.25")},
    {"contract_id": "NQ", "qty": Decimal(-2), "price": open_[9], "multiplier": M, "commission": Decimal("4.50")},
]
near = lambda a, b: abs(a - b) < Decimal("0.000001")                                   # noqa: E731

# Average cost: the average of 2 lots at open_[2] and 1 lot at open_[5] is (2 x open_[2] + open_[5]) / 3. The sale of 2 books (open_[9] - average) x 2 x 20.
average = (2 * open_[2] + open_[5]) / 3
position = cost_basis.reconstruct(fills)["NQ"]
assert position.qty == 1 and near(position.avg_price, average) and near(position.realized, (open_[9] - average) * 2 * M)
assert position.commissions == Decimal("11.25")                                         # 4.50 + 2.25 + 4.50

# First in first out: the two lots of the first buy close first at their own price: (open_[9] - open_[2]) x 2 x 20, and the lot of open_[5] is what remains.
fifo = reduce((Lot(Decimal(2), open_[2]), Lot(Decimal(1), open_[5])), 2, exit_price=open_[9], side_held="buy", multiplier=M)
assert fifo.realized_pnl == (open_[9] - open_[2]) * 2 * M and fifo.remaining == (Lot(Decimal(1), open_[5]),)

# The rules disagree on the sale and agree on the whole: close the last lot at the same price and the two totals are equal.
last = open_[12]
average_total = position.realized + (last - position.avg_price) * 1 * M
fifo_total = fifo.realized_pnl + (last - open_[5]) * 1 * M
assert near(average_total, fifo_total)
print("average cost realized", position.realized, "fifo realized", fifo.realized_pnl)
OK = True
```

## Checks

- Both totals are equal once the position is closed; only the timing of the profit differs. A tax report needs the rule of the jurisdiction.
- The multiplier is part of every figure. A future without it is off by a factor of the contract size.
- Commissions are positive costs. Gross minus costs is the net; show both.
- Reconcile the result with the broker statement before trusting a report built from fills.
