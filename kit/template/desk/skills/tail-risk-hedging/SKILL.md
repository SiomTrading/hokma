---
name: tail-risk-hedging
version: 1.0.0
description: >
  Use when the owner or another agent asks whether a stock or option position, a LEAPS holding or a whole portfolio should be
  protected against a large drop, how much the protection may cost, or when to roll it or cash it in. Trigger phrases: "should we hedge",
  "how much does protection cost", "put spread or collar", "hedge budget", "when do we roll the puts", "take profit on the hedge".
identity: A risk reader who fixes the budget, the roll rule and the cash-in rule of a hedge before it is bought, and prices it from the options module and never from memory.
patterns:
  - Write the budget first, as dollars per year and as a share of the portfolio value from the back office snapshot, and say who set it. The desk limits and the owner set it, not this skill.
  - Price every candidate with the options module, using the contract sheet multiplier of each leg. Candidates are a long put, a put spread, a collar, and for an index overlay a call spread on the volatility index when the desk trades one.
  - Read the payoff and the net greeks of each candidate with the structure profile call. State the loss that remains after the hedge, the premium paid, the breakeven and the move that pays for it.
  - Write the roll rule before entry as a time rule and a moneyness rule, and the cash-in rule as the share of the maximum payoff at which part of the hedge is sold. Say that these thresholds are the desk's choice until a backtest calibrates them.
  - For a LEAPS holding compare the hedge expiry with the holding horizon and name the gap in months.
  - Stress the unhedged and the hedged position for drops of 10, 20, 30 and 50 percent with the risk module, and show both results side by side.
  - Put the carrying cost of the hedge next to the expected benefit and say when the cost is larger than the loss it avoids.
avoid:
  - Quoting an annual cost of protection, a multiple of return that a hedge fund earned or the return of a trend overlay from memory. Those figures come from the options module or they are not said.
  - Writing a multiplier of 100 from memory. The contract sheet decides.
  - Describing a hedge as free, as a source of profit or as certain protection. A hedge can expire worthless and then it was a cost.
  - Choosing the budget, the roll rule or the cash-in rule after the hedge was bought.
  - Hedging a position that the book does not hold. Read the positions from the back office snapshot first.
  - Treating a put spread as protection below its lower strike.
---

# Tail risk hedging

A hedge is insurance. It has a price, a term and a payoff, and its rules must exist before the first premium is paid. This
skill defines the budget and the rules, and prices the choices with the options module.

## 1. What is being protected

1. Read the positions and the portfolio value from the back office snapshot. If the state is not provided, say so and stop: nothing is hedged on a guess.
2. For each underlying list the quantity, the expiration of any option, and the horizon the desk holds it for.
3. Name the loss that the owner cannot accept, as a dollar figure from the desk limits. If the desk has none, ask the arbiter to set one.

## 2. The budget

1. State the budget in dollars per year and as a share of the portfolio value, with its source.
2. Divide it by the number of rolls per year to get the premium that each hedge may cost.
3. A candidate that costs more than that premium is not offered.

## 3. The candidates

1. Call `quant_call options.structure_profile` for each candidate with its strikes, premiums per share, quantity in contracts and the multiplier of the contract sheet.
2. Read the maximum loss after the hedge, the net premium, the breakevens and the net greeks.
3. For a collar read the cap on the upside that was given up, in dollars.
4. Call `quant_call options.payoff` when the legs are not a named structure.

## 4. Roll and cash-in rules, written before entry

| Rule | What to write |
|---|---|
| Roll by time | The number of days to expiry at which the hedge is replaced. |
| Roll by moneyness | The distance of the spot from the long strike at which the hedge is moved. |
| Cash in | The share of the maximum payoff at which part of the hedge is sold, and what is done with the proceeds. |
| Stop paying | The condition under which the hedge is not renewed. |

Say for each number that it is a choice of the desk. A threshold calibrated by a backtest replaces it when one exists.

## 5. Stress

1. Call `quant_call risk.stress` with the portfolio shocks for drops of 10, 20, 30 and 50 percent, once for the unhedged book and once with the hedge payoff added.
2. Show both results and the difference. The difference is what the hedge buys.

## 6. Report

1. The budget and who set it.
2. The candidates with premium, remaining loss and breakeven.
3. The written roll and cash-in rules.
4. The stress table, hedged and unhedged.
5. What could not be priced, as data gaps. Do not recommend an order: hand the figures to the arbiter.
