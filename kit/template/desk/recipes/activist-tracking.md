---
name: activist-tracking
lines: [SWO, LP]
summary: Follow of large holders who ask a company for change.
---
# Recipe: activist-tracking

Follow the steps in order. Cite each result as it arrives, as the citation rules require. Every call carries the cut-off
time of the view as its as_of. If a call returns no data, unavailable or stale, write that in the report and go on with
the remaining steps.

1. Call `data_filings` with forms 13D and 13G for the symbol and list the holders, the size of the stake and the date of each filing.
2. Read the purpose section of each 13D and write down what the holder asks for.
3. Call `data_filings_watch` to see whether amendments or new filings appeared since the last check.
4. Call `data_filings` with form 8-K and look for the answers of the company: board changes, agreements, plans announced.
5. Call `data_snapshot` with the categories price and valuation to place the filings against the price path.

## Synthesis

Write who holds how much, what they ask for, how the company answered and what is still open. Keep the facts of the filings apart from your reading of what the holder intends.
