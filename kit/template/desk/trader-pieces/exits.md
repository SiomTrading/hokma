---
name: exits
lines: [SWO]
summary: How the stop and the objective are set
---
# Stop and objective

The exits are computed by code from the plan, and the order carries them. You choose only the basis of the stop.

- Basis `premium`: a debit structure leaves when a share of the premium paid is lost; a credit structure leaves when its loss reaches a multiple of the credit received.
- Basis `underlying`: the stop is a level of the share. The options module prices the structure at that level and the order leaves at that premium. Use it only when the thesis is about a level that, once lost, ends the idea.

The objective is a multiple of the distance from the entry to the stop for a debit structure, and a share of the credit for a credit structure. Prices are rounded to the tick of the contract sheet against the trader: the stop away from the entry, the objective toward it.

The shares, multiples and objectives are defaults until a backtest calibrates them. Say so when you mention them. Never write a stop or an objective yourself.
