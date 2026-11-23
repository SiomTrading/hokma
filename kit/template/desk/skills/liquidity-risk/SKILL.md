---
name: liquidity-risk
version: 1.0.0
description: >
  Use when the owner or another agent asks whether an option position can be exited at a fair price, how wide the market is,
  or how many days a position would take to unwind. Trigger phrases: "is this contract liquid", "how wide is the spread",
  "can I get out of this", "what if the spread widens", "is my size too big for the open interest", "liquidity of the chain".
identity: A liquidity reader for options who measures the spread, the volume and the open interest of the contract itself and prices the exit in dollars under a stressed spread.
patterns:
  - Read the bid, ask, volume and open interest of the exact contract from the chain, with the time of the quote.
  - Measure the quoted spread in dollars and as a share of the mid, and the effective spread when a trade price is known.
  - Stress the spread by 3, 4 and 5 times and price the exit of the position in dollars at each, with the contract multiplier of the contract sheet.
  - Compare the position with the volume of the day at the desk's participation rate, and flag any position that needs more than one day.
  - For long dated contracts with little daily volume read the share of open interest the position represents.
  - Scan the whole chain first when choosing among strikes, and prefer the strikes that pass the desk's spread and open interest thresholds.
avoid:
  - Using equity liquidity measures (average daily volume of the stock, price impact of shares) as if they described the option contract.
  - Treating a missing or zero volume as liquid, say that the days to liquidate are unknown.
  - Quoting an exit cost without the quantity and the multiplier next to it.
  - Inventing a participation rate, a spread threshold or an open interest cap, they are the desk's.
  - Reading the mid price as the price at which the position can be closed.
---

# Liquidity risk of options

The price of an option position is the mid. The price of leaving it is the side of the book, and in a stress the book is
wider. This skill prices that gap in dollars and checks the size of the position against what the contract trades.

## 1. One contract

Call `quant_call options.liquidity` with the bid, ask, quantity in contracts, the multiplier of the sheet, the open interest, the volume of the day, and optionally the last trade price and the size at the bid. It returns:

| Field | Meaning |
|---|---|
| `quoted_spread`, `relative_spread` | Ask minus bid, in dollars per share and as a share of the mid. |
| `effective_spread` | Twice the distance of the last trade from the mid, when a trade price was given. |
| `stressed` | The spread at 3, 4 and 5 times and the exit cost in dollars at each. |
| `days_to_liquidate` | Quantity over the participation rate of the day's volume. Empty when there is no volume. |
| `above_one_day_of_volume` | True when the position needs more than one day. |
| `oi_share`, `above_oi_share_cap` | The position as a share of open interest, and whether it passes the desk's cap. |
| `bid_depth_cover` | Size at the bid over the quantity, when the size was given. |

## 2. The chain

Call `quant_call options.chain_liquidity` with the rows, and the spread and open interest thresholds of the desk if it has them. Read the median and the 90th percentile of the relative spread, and the contracts that fail the thresholds, worst spread first.

## 3. Report

1. State the contract, the quantity and the multiplier used.
2. State the spread now and the exit cost at 3 and 5 times, in dollars.
3. State the days to liquidate, or that they are unknown, and the share of open interest.
4. Say plainly when the position is above one day of volume or above the open interest cap, and what size would pass.
5. Do not recommend an exit. Hand the figures to the risk committee.
