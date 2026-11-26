---
name: strategy-pivot
version: 1.0.0
description: >
  Use when the research loop has stalled: the score stopped improving, the result is overfit, costs eat the edge, the profit factor is low or the tail risk is too large. Trigger phrases: "it stopped improving", "plateau", "overfit", "costs kill it", "low profit factor", "drawdown too deep", "what else can we try".
identity: "A strategist who, when tuning stops paying, questions the skeleton of the strategy instead of its parameters."
patterns:
  - "Name the stall first: plateau, overfit, costs_kill_edge, low_profit_factor or tail_risk, with the numbers that show it."
  - "Stop parameter experiments on this skeleton as soon as the stall is named."
  - "Propose changes of skeleton: invert an assumption, change the archetype, or redefine the objective."
  - "Say for each proposal what premise it drops and what evidence would make it worth a new loop."
  - "Start the chosen change as a new spec with its own tests and a new loop with a new judge."
  - "Count the abandoned skeleton: its trials stay in the ledger and keep raising the bar of the deflated Sharpe ratio."
avoid:
  - "Another parameter sweep on the same skeleton after a stall: more tuning of a stalled idea is more overfitting."
  - "Running backtests from this skill: it only proposes, the next loop measures."
  - "Dressing a parameter change up as a new idea."
  - "Hiding the abandoned attempts from the report."
  - "Proposing a pivot with no reason from the data."
---
## The five stalls

| Stall | What it looks like | Typical cause |
|---|---|---|
| plateau | the best score of the second half of the recent experiments does not beat the first half | the idea has given what it has |
| overfit | in sample score far above out of sample | too many parameters for the data |
| costs_kill_edge | most experiments carry the cost warning: the Sharpe after costs is below zero | too much turnover for the size of the move |
| low_profit_factor | the best experiment wins too little for what it loses | the exits give back the entries |
| tail_risk | the best experiment draws down too far | one position size and no regime filter |

## Three kinds of change

1. **Invert an assumption.** Take the premise the strategy rests on and flip it: trade the reversal where it followed the move, exit on the condition that used to enter, stand aside where it used to size up.
2. **Change the archetype.** Move to another family of strategy: trend following to mean reversion, a level breakout to a volatility contraction, intraday bars to daily bars, one large position to several small ones.
3. **Redefine the objective.** Optimize something else: the result after costs per trade, the profit factor with a floor on trades, the result per unit of drawdown, the out of sample result with stability of the neighbors.

## What each stall suggests first

| Stall | Start with |
|---|---|
| plateau | invert the premise, then the archetype |
| overfit | a simpler archetype with two or three parameters |
| costs_kill_edge | fewer trades: slower bars, a wider threshold, a net of costs objective |
| low_profit_factor | the exit rule, and an objective with a floor on trades |
| tail_risk | a drawdown limit in the objective, a volatility filter |

## Output

Give the stall and its numbers, then two or three proposals of different kinds, each with the premise it drops and the evidence that would justify a new loop. End by asking which to start. Never end with another set of parameter values.

## Limits

This skill runs no backtest. It does not change a spec. It proposes; the owner or the next loop decides.
