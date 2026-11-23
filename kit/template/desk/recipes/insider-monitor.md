---
name: insider-monitor
lines: [SWO, LP]
summary: Review of what insiders did with their own shares and what it may mean.
---
# Recipe: insider-monitor

Follow the steps in order. Cite each result as it arrives, as the citation rules require. Every call carries the cut-off
time of the view as its as_of. If a call returns no data, unavailable or stale, write that in the report and go on with
the remaining steps.

1. Call `data_filings` with form 4 for the symbol, covering the window the line uses (for example the last ninety days).
2. Separate open market purchases and sales from grants, exercises and tax withholding. Only the first group counts as a signal.
3. Group the transactions by person and role and note who is an officer, who is a director and who is a large holder.
4. Compare the pattern with the earlier twelve months for the same people.
5. Call `data_filings_watch` to see whether new filings of the symbol appeared since the last check.
6. Call `data_snapshot` with the categories price and valuation to place the transactions against the price at the time.

## Synthesis

Write what was bought and sold, by whom and when, what is routine and what is unusual, and how sure you are. A pattern of one person is an observation, not a conclusion.
