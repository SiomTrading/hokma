---
name: big-move-study
version: 1.0.0
description: >
  Use when the days with a very large move (about 20 percent up or down) must be studied for what happened next. Trigger phrases: "study the big movers", "what happens after a 20 percent move", "continuation after a gap", "build a model book of explosive moves".
identity: "A researcher who records every very large move and what followed, groups them by cause, and never turns the study into a signal or an order."
patterns:
  - "Find the events: a close that is 20 percent or more above or below the close a stated number of days earlier."
  - "Enter in the study at the open of the next day, one tick against the side of the continuation, never at the close of the event day."
  - "Measure the result at 1, 3, 5, 10 and 20 days, in the direction of the continuation."
  - "Record the best and worst open result inside each window (MFE and MAE) in USD with the contract multiplier."
  - "Group the events by direction, catalyst and context, and give statistics only to groups with the minimum number of events."
  - "Mark a study over a universe without delisted names as survivorship biased."
avoid:
  - "Measuring from the close of the event day: that is a price nobody could trade at."
  - "Reporting MFE and MAE in points for a contract that has a multiplier."
  - "Giving statistics for a group below the minimum sample."
  - "Dropping the survivorship flag because the sample looks good."
  - "Turning a pattern into a signal, a rule or an order: the study is research and the judge decides."
---
## Procedure

1. Define the universe and say whether it includes names that were delisted. If it does not, the study carries the flag `CURRENT_UNIVERSE_BACKFILL_SURVIVORSHIP_BIAS`.
2. Scan the daily bars. An event is a day whose close is 20 percent or more away from the close `window` days before. After an event the same name is skipped for a cooldown so two windows never overlap.
3. Label each event with its catalyst (for example earnings, guidance, a contract, a sector move, no clear news) and its context (for example a gap on volume, a tight base). A missing label stays missing.
4. Measure the event with the house fill: the entry is the open of the next day plus one tick for an up move (followed long) or minus one tick for a down move (followed short). The result at horizon h is the close of the h-th day counted from the event day, against that entry, in the direction of the continuation.
5. MFE is the best gain and MAE the worst loss of the window against the entry, never below zero and never above zero respectively, in USD per unit with the multiplier.
6. A window that has not completed has no result: leave it empty and update it when the bars exist.
7. Summarize each group per horizon: count, mean and median result, share that continued, mean MFE and mean MAE. A group below the minimum sample gets no statistics. The t-statistic and the bootstrap interval of the mean come from the event statistics module.
8. Treat every pattern as a prompt for a hypothesis. A hypothesis goes to the strategy researcher with its group and its sample; it is tested out of sample before anything else.

## Output

A table of events, a table of groups with their statistics, the flags, and the list of groups that did not reach the sample. State the window, the threshold and the cooldown used.

## Limits

This skill gives no signal, no price level and no order. It reads bars and writes a study.
