---
name: filing-comparison
lines: [LP]
summary: Comparison of two filings of the same type to find what changed.
---
# Recipe: filing-comparison

Follow the steps in order. Cite each result as it arrives, as the citation rules require. Every call carries the cut-off
time of the view as its as_of. If a call returns no data, unavailable or stale, write that in the report and go on with
the remaining steps.

1. Call `data_filings` with the form (annual or quarterly) for the two periods to compare and take the same sections of each.
2. Compare the risk factors section: new items, removed items and items that changed wording.
3. Compare the management discussion: new explanations, dropped metrics and changes of tone.
4. Compare the notes on debt, leases, segments and accounting policies.
5. Call `data_fundamentals` for the two periods and tie each textual change to a number that moved, when there is one.

## Synthesis

Write a list of changes ordered by importance. Each change shows the old text, the new text, the date of each filing and the number that moved. Mark changes that have no number behind them as unverified.
