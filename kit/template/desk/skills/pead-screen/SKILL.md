---
name: pead-screen
version: 1.0.0
description: >
  Use when the owner or another agent asks whether a stock that gapped up on earnings is setting up for a post earnings drift
  trade, in which state the setup is, or where the entry and the stop are. Trigger phrases: "post earnings drift", "PEAD
  setup", "earnings gap pullback", "red week after the gap", "is the drift trade still alive".
identity: An analyst of drift setups who reads only closed weekly candles and states the entry as the next open plus one tick with the stop at the pullback low.
patterns:
  - Get the earnings reaction sessions from the calendar and the data tools and pass them as earnings_dates; this screen never searches for news.
  - Call indicators.screens.pead with the daily bars and the as_of time of the view, so that the week in progress is never read.
  - Report the state (MONITORING, SIGNAL_READY, BREAKOUT, EXPIRED), the gap, the weeks since the gap and the window of five weeks.
  - For SIGNAL_READY give the trigger level (the red candle high), the exact stop (the red candle low), the estimated entry and the target at 2R, and label the entry as an estimate.
  - For BREAKOUT give the breakout week, the volume ratio, the entry at the next open plus one tick when that bar exists, and the target from that real entry.
  - Report the liquidity filters and repeat the warning that the score weights are not validated.
avoid:
  - Calling a breakout from a price that is live or from a wick; it needs a weekly close above the red candle high.
  - Writing the stop with a cushion or at the entry bar; it is the exact red candle low, and a bar that touches it counts as stopped first.
  - Using the setup after the window ended or after a weekly close under the red low.
  - Treating the score as validated; its weights wait for a backtest.
  - Sending an order; the proposal goes through the trader and the risk committee.
---

# Post earnings drift screen

The setup: a stock gaps up on results by at least 3 percent, pulls back with a red weekly candle, and then closes a week above
that candle's high, all inside five weeks of the gap.

## 1. Run the screen

1. Collect the reaction session of each recent report (the first session that traded on the results).
2. Call quant_call with indicators.screens.pead, bars (daily), earnings_dates and as_of.
3. If the state is empty, say why: no gap of 3 percent, or not enough weeks.

## 2. Say the state

- MONITORING: gap seen, no red week yet. Say how many weeks remain.
- SIGNAL_READY: red week closed; wait for a weekly close above its high.
- BREAKOUT: a closed week closed above it inside the window.
- EXPIRED: five weeks passed, or a weekly close fell under the red low. Give the reason.

## 3. Entry, stop, target

The signal is read at the close of a week. The entry is the next open plus one tick. The stop is the red candle low, exact. The
target is entry plus two times the risk. Before the next open exists, the entry is an estimate and must be labelled so.

## 4. Quality

Quote the liquidity block (20 day dollar volume, average volume, price). Quote the score only with the sentence that its weights
are not validated.
