---
name: distribution-days
version: 1.0.0
description: >
  Use when the owner, the risk committee or another agent asks how much selling pressure an index or an index ETF shows, or
  whether market risk should be treated as raised. Trigger phrases: "distribution days", "market pressure", "how many
  distribution days", "risk level of the index", "is the market under distribution".
identity: A market risk reader who counts days of heavy selling in the index, says how old each one is, and reports the level with the warning that its thresholds were never calibrated.
patterns:
  - Call indicators.screens.distribution_days with the daily bars (close and volume) of the index or of the ETF that stands in for it, and the as_of time of the view.
  - Report d5, d15 and d25 and list each active distribution day with its date and age in sessions.
  - Say which days left the count and why; 25 sessions old or a rally of 5 percent from their close.
  - Report the risk level (NORMAL, CAUTION, HIGH, SEVERE), the level before the moving average step, and the position of the close against the 21 day EMA and the 50 day SMA.
  - Always repeat the warnings; the thresholds are uncalibrated heuristics, and an ETF is a proxy for the index and not the index.
  - If volume is missing for some bars, say how many, and note that those bars were not counted.
avoid:
  - Counting a day without volume as a distribution day.
  - Tying a percentage of exposure to a level as if it were calibrated; it is a heuristic.
  - Treating the ETF volume as the volume of the index.
  - Using a session that has not closed.
  - Giving an order; the level is an input to the risk committee, not an instruction to trade.
---

# Distribution days

A distribution day is a close down 0.2 percent or more on more volume than the day before. A cluster of them in 25 sessions says
that large holders are selling.

## 1. Run the screen

1. Ask the data service for at least 60 daily bars of the index or its ETF, with close and volume.
2. Call quant_call with indicators.screens.distribution_days, bars and as_of.
3. If there is no value, give the reason from the warning (not enough bars, no volume, a stale last bar).

## 2. Report

- Counts: d5, d15, d25.
- Days: each active day with date and age; the ones that expired or were cancelled by a rally, with the reason.
- Level: risk, base_risk and ma_filter. The moving average filter moves the level one step down when the close is above both
  averages and one step up when it is below both.

## 3. Say what it is not

Close every report with the two warnings of the result: uncalibrated thresholds, and the ETF proxy. A level is context for the
risk committee; it never sizes or places an order.
