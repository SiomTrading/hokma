---
name: earnings-analysis
lines: [SWO]
summary: Reading of a results release against what was expected, around the event.
---
# Recipe: earnings-analysis

Follow the steps in order. Cite each result as it arrives, as the citation rules require. Every call carries the cut-off
time of the view as its as_of. If a call returns no data, unavailable or stale, write that in the report and go on with
the remaining steps.

1. Call `data_snapshot` with the symbol and the categories calendar and price to get the date of the release and the price before it.
2. Call `data_filings` with form 8-K for the release of the quarter and take the reported revenue, margins and earnings per share.
3. Call `data_fundamentals` for the quarterly income statement of the same quarter one year earlier and the quarter before.
4. Take the fiscal quarter from the name of the report, never from the calendar date. Label every valuation ratio as trailing or forward.
5. Call `data_bars` with a daily interval from ten days before the release until the cut-off and compute the move of each day.
6. Call `quant_call` for the surprise and the drift criteria the line uses, with the figures of steps 2 and 3.
7. Write down what the market could not know before the release and what it knew.

## Synthesis

Write a short report: what was reported, what was expected, the size of the surprise, how the price moved and whether the move continued. Separate facts from your inference, and cap any inference at the ceiling of the source policy.
