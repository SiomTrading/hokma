---
name: momentum-burst
version: 1.0.0
description: >
  Use when the owner or another agent asks whether a stock just made a one day burst of momentum on daily bars, or wants the
  entry and stop of that burst. Trigger phrases: "momentum burst", "did it break out today on volume", "4 percent day on rising
  volume", "range expansion day", "is this a clean burst or an extended one".
identity: A reader of daily candles who calls a burst only when the closed bar, the volume and the base before it agree, and who states the entry and the stop as rules, not as prices already filled.
patterns:
  - Get the daily bars from the data service and call indicators.screens.momentum_burst with the as_of time of the view, so that only closed bars are read and the bar in progress is ignored.
  - Report which trigger fired (percent, dollar or range), the gain, the dollar gain and the position of the close inside the bar, each with its number.
  - Report the four filters one by one; close near the high, contracted base before the burst, no run of up days just before, no recent drop of 4 percent or more.
  - State the entry as the next open plus one tick and the stop as the low of the trigger bar, exact, and say that a bar that touches both is counted as stopped first.
  - Show the risk per share only as an estimate until the next open exists, and label it as an estimate.
  - When the warning about the dollar threshold appears, say that the dollar trigger depends on the price scale of this stock and lean on the percent trigger.
avoid:
  - Reading the bar of a session that is still open, or a stale last bar, as a signal.
  - Treating a trigger as a signal when one of the filters failed.
  - Writing an entry at the close of the trigger bar, or a stop with a cushion that the screen did not give.
  - Calling a burst good because of the news around it; this skill reads price and volume only.
  - Sizing a position; size comes from the risk module.
---

# Momentum burst

A burst is one closed daily bar that moves far, on more volume than the day before, out of a quiet base. The screen only measures.
The decision to act belongs to the trader and to the risk committee.

## 1. Read the bar

1. Ask the data service for the daily bars of the symbol with open, high, low, close and volume. Ask for at least 40 sessions.
2. Call quant_call with indicators.screens.momentum_burst, passing bars and as_of (the time of the view in New York time).
3. If the result has no value, read the warning and say why: not enough bars, a bar with a missing value, or a last bar that is
   too old. Do not estimate a value by hand.

## 2. Say what fired

- triggers.percent: close at least 4 percent over the previous close with more volume than the day before.
- triggers.dollar: a gain of at least the dollar threshold with more volume. Quote dollar_threshold_share_of_close.
- triggers.range: a range larger than the three previous ranges, an up close, and a day before that was not already a 4 percent move.

## 3. Check the filters

A signal needs a trigger and all of these: close near the high, contracted base, no run of three up days before, no drop of
4 percent in the last three bars. trigger_dates lists every past trigger and signal_dates the ones that passed the filters; use
them to show how often the pattern appears on this stock.

## 4. Entry and stop

The signal is read at the close, so the entry cannot be the close. Entry is the next open plus one tick. The stop is the low of
the trigger bar. If the next bar trades through the stop, the stop counts first. Pass these rules to the trader as they are.
