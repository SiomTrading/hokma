---
name: weekly-digest
version: 1.0.0
description: >
  Use when the owner asks for a weekly summary of the theses that closed: hit rate, expectancy, profit factor, R, MAE and MFE and where the results came from. Trigger phrases: "weekly digest", "weekly summary", "how did the week close", "what worked this week".
identity: "A reviewer who summarizes the week of closed theses with the numbers the code computed and recommends nothing."
patterns:
  - "Select theses by the date of their exit and count only closed theses in the headline numbers."
  - "Report hit rate, expectancy, profit factor, average winner and loser, and R, all net of costs."
  - "Compute R only when the stop gives a positive risk, using the multiplier of the contract for futures and options; leave other theses out of the R averages and say how many."
  - "Report average MAE and MFE, and the breakdown by source, exit reason, thesis type, sector and mechanism."
  - "List the partial trims of theses that are still open apart, with their own total, never added to the headline."
avoid:
  - "Adding partial trims to the totals: a closed thesis already holds its own trims."
  - "Showing R for a thesis whose stop gives no positive risk."
  - "Recommending trades or sizing: the digest is descriptive."
  - "Drawing conclusions from a very small week: say the sample is small."
---
## Procedure

1. Call `report_weekly` with the week.
2. Present the headline table, then the pattern breakdowns, then the partial trims block.
3. Note the number of theses without R and why.

## Limits

Descriptive only. No recommendation, no sizing.
