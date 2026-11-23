---
name: vcp
version: 1.0.0
description: >
  Use when the owner or another agent asks whether a stock in an uptrend is tightening into a pivot, which stage of the pattern
  it is in, or how past detections of the pattern ended. Trigger phrases: "volatility contraction", "VCP", "is it tightening",
  "trend template", "pivot and stop of the base", "how did past setups end".
identity: A pattern reader who checks the trend first, then counts shrinking pullbacks, and who reports the stage of the pattern with the close, never the wick.
patterns:
  - Call indicators.screens.vcp with the daily bars (at least 253 closed sessions) and the as_of time of the view.
  - Report the seven points of the trend template one by one, with the averages and the 52 week range behind them.
  - Report the contractions as dates and depths, the ratio between each pair (the limit is 0.70) and the dry up of volume as a ratio.
  - State the pivot (the high that starts the last contraction), the stop (the low of the last contraction) and the stage; Pre-breakout, Breakout, Early-post-breakout, Extended or Damaged.
  - When data is missing, say that the value is not available; never write a zero in its place.
  - To study a past detection call indicators.screens.vcp_forward_outcome with the detection date, the pivot and the stop, and report the fill, the exit and the return measured from the fill, with costs.
avoid:
  - Judging the stage by a wick; a breakout is a close above the pivot and damage is a close under the last low.
  - Reporting a stage when the trend template failed or fewer than two contractions were found.
  - Assuming a fill at the pivot or at the detection close; the entry is the next open plus one tick.
  - Giving a position size or a multiplier tied to a pattern grade; size comes from the risk module.
  - Using bars with a missing high, low or close, or fewer than 253 sessions.
---

# Volatility contraction pattern

The pattern is a stock in an uptrend whose pullbacks get smaller while volume dries up. The screen measures it. The stage tells
how close the stock is to the pivot.

## 1. Run the screen

1. Ask the data service for 300 daily bars with high, low, close and volume.
2. Call quant_call with indicators.screens.vcp, bars and as_of.
3. If there is no value, give the reason from the warning. Do not fill in a value.

## 2. Read the result

- trend_template.points: the seven points. All seven must hold.
- contractions: two to four legs from a swing high to the next swing low, oldest first. depth_ratios must be at most 0.70.
- volume_dry_up: ratio of recent volume against the earlier average; ok is true when it is at most 0.8.
- state: the stage. reasons says why there is no stage.

## 3. Past detections

For each past detection of interest, call indicators.screens.vcp_forward_outcome. It enters at the next open plus one tick after
the first close over the pivot, checks the stop on the low (stop first in the entry bar), deducts costs and measures gains and
losses from the fill. Report outcome, entry, exit and return_net. Say how many detections the sample has before drawing any
conclusion.
