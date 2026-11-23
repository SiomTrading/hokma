# Execution in Hokma

This page describes what the code does. The single door from an order idea to a broker is `hokma.exec.submit`.

## The road of an order

1. A desk agent calls `exec_propose` (or the owner asks in the chat). The proposal is typed (`OrderProposal`): legs with canonical contract ids, a
   quantity as a Decimal, an order type, optional stop and target. The agent never sends anything.
2. `submit` takes the origin from who is asking, never from the proposal: only the owner is `chat`.
3. Every contract needs a sheet (multiplier, tick, expiration, strike) from `hokma.contracts`. No sheet, no order (`no_contract`).
4. The filter (`hokma.exec.prefilter`) runs at every level, in a fixed order: contract discipline, symbol list, event windows, cooldown, position
   size with the multiplier, emergency monitor, account (daily loss, drawdown, capital floor, funded account rules), limits of the committee with
   clamping, and last the rules of SPEC-015. The effect of a leg (open or close) is read from the real positions, not from what was declared.
5. The autonomy level of the desk decides what happens next.

| Level | What the desk does |
|---|---|
| 1 | Only analyzes: the proposal is recorded as `analysis_only`, nothing is sent. |
| 2 | Proposes and asks the owner yes or no for each order (app and Telegram buttons). |
| 3 | Sends alone under rules the owner approved (symbols, quantity, computed risk). An order outside the rules asks the owner. |

Raising the level, and approving the rules of level 3, is the owner's. Lowering it is free.

## What is approved is what is sent

The proposal has a fingerprint (the hash of its canonical form without the thesis). The owner approves that fingerprint. `send_approved` claims the
row atomically, recomputes the fingerprint of the saved proposal, compares it with the approved one (`hash_mismatch` stops everything), runs the
filter again with the prices of that moment, and only then calls `submit.dispatch`. Two sends of the same fingerprint let one out.

## Money

Quantities and amounts are Decimal from the proposal to the broker. A `reduce_only` order is cut to the position the book really holds and never
opens the opposite side. The risk of an order for the rules of level 3 is computed from the legs, the multiplier and the entry price; a stop lowers
it only when the broker places the stop with the order and the stop is at a real distance.

## Brokers

The simulated broker trades a real market: a market order fills at the ask (buy) or the bid (sell) worse by the slippage, with commission, and a
limit order waits for the stream. Without a fresh quote it refuses (`no_market_price`). A desk moves to a real account (Schwab, Alpaca, IBKR, crypto)
by changing only its assigned account. Every fill of every broker is written once in the book (`book_entries`).

## Emergency switch

A task of the scheduler reads the desks that have emergency thresholds, and pauses a desk that passes them (percent or USD). If the task stops, a
callback restarts it, and a desk whose monitor has not read it for two intervals does not open new positions.
