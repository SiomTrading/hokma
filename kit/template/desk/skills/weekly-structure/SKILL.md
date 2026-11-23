---
name: weekly-structure
version: 1.0.0
description: >
  Use when the owner or another agent asks for the weekly picture of a stock or index: trend, support and resistance, where the
  idea is wrong, or whether a turn is real. Trigger phrases: "weekly chart read", "what is the trend", "where are the levels",
  "is this reversal confirmed", "failed breakout on the weekly", "what invalidates this view".
identity: A weekly chart reader who writes two to four scenarios with the level that proves each one wrong, and who never gives a probability that nothing measured.
patterns:
  - Call indicators.screens.weekly_structure with the daily bars and the as_of time of the view; the screen cuts the bars at as_of first and then builds the weeks, so the week in progress never counts.
  - Report the trend with the closing price against the 30 week average and the direction of that average, then the swing structure of the last pivots.
  - List the nearest two supports and two resistances from the fractal pivots and say that a pivot is known only two weeks after it formed.
  - Give each scenario with its trigger level and its invalidation level, both taken from the result, and leave the probability empty.
  - For a possible reversal read the confirmation block; the 13 week direction change, the failed 52 week extreme, the failed breakout and the continuation veto, and quote the verdict with its reasons.
  - If a visual reading of the chart exists, pass it as visual_read; a disagreement with the reading by code is reported as not confirmed.
avoid:
  - Giving a probability for a scenario; the screen returns none because none was calibrated.
  - Reading a week that has not closed, or mixing a partial week into the averages.
  - Calling a reversal confirmed when the verdict says not confirmed, or ignoring the continuation veto.
  - Treating a tie as a break; levels and extremes need a strictly higher high or a strictly lower low.
  - Reading intraday moves or order flow; this skill is weekly only.
---

# Weekly structure

The goal is a plain statement of where the stock is and what would change that. Every level comes from the screen.

## 1. Get the numbers

1. Ask the data service for daily bars (open, high, low, close, volume), at least 54 weeks.
2. Call quant_call with indicators.screens.weekly_structure and the as_of time of the view.
3. If there is no value, say why (fewer than 54 closed weeks, a missing value) and stop.

## 2. Write the read

- Trend: up, down or sideways, with the close, the 30 week average and its slope.
- Structure: higher highs and higher lows, lower highs and lower lows, expanding or contracting.
- Levels: supports and resistances, each with its price.
- Volume: the ratio of the last week against the previous ten.

## 3. Write the scenarios

Use the scenarios of the result (two to four). For each one write the name, the trigger, the invalidation and the sentence "wrong
if the weekly close is beyond the invalidation level". Say once that the probabilities are not calibrated.

## 4. Confirm a turn

Read confirmation.verdict. A turn is confirmed when there is a reversal signal, no continuation veto and no disagreement with the
visual reading. Quote confirmation.reasons when it is not confirmed.
