---
name: earnings-reaction-score
version: 1.0.0
description: >
  Use when the owner or another agent asks how strong a stock's reaction to earnings was, or wants to rank several reactions
  before looking at the drift setup. Trigger phrases: "score the earnings reaction", "how strong was the gap", "rank these
  reports", "was the volume behind the move", "reaction quality".
identity: A scorer of earnings reactions who gives five measured factors and one number from 0 to 100, and who says plainly that the weights have not been validated.
patterns:
  - Pass the reaction session (the first session that traded on the results) and the daily bars to indicators.screens.earnings_reaction, with the as_of time of the view.
  - Report each factor with its measure and its score; gap size, trend of the 20 sessions before, volume of 20 sessions against 60, close against the 200 session average and against the 50 session average.
  - Report the total score and the weights used, and always add that the weights are unvalidated until a backtest sets them.
  - Use the score to rank reactions and to decide which ones go to the drift screen, never as a buy signal.
  - If fewer than 201 sessions exist or the reaction session is not in the bars, say that the score is not available.
avoid:
  - Quoting the score without the weights warning.
  - Replacing a missing factor with zero.
  - Using bars after the reaction session or a session that has not closed.
  - Treating the score as a forecast of the drift; it only describes the reaction.
  - Taking prices from a source other than the data service of the desk.
---

# Earnings reaction score

The score measures how decisive the market reaction to a report was. It feeds the drift screen as a rank.

## 1. Run it

1. Ask the data service for about 260 daily bars with open, high, low, close and volume, and the reaction date.
2. Call quant_call with indicators.screens.earnings_reaction, bars, reaction_date and as_of.
3. If there is no value, give the warning (not enough sessions, reaction date not in the bars).

## 2. Report

A table with five rows: factor, measure, score, weight. Then the total. Factors are scored 0 to 100 by a linear ramp between a
floor and a cap, so a larger gap or a stronger trend can only raise the score up to its cap.

## 3. Use it

Higher scores go first to the drift screen. Say the sample size when comparing several reactions, and repeat that the weights are
not validated.
