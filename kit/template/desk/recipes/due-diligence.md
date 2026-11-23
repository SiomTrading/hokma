---
name: due-diligence
lines: [LP]
summary: Full review of a company before the desk holds it for a long time.
---
# Recipe: due-diligence

Follow the steps in order. Cite each result as it arrives, as the citation rules require. Every call carries the cut-off
time of the view as its as_of. If a call returns no data, unavailable or stale, write that in the report and go on with
the remaining steps.

1. Call `data_snapshot` with the symbol and the categories profile, valuation and price to freeze the facts and get the snapshot hash.
2. Call `data_fundamentals` for the income statement, the balance sheet and the cash flow statement, annual period, as many years as the source returns.
3. Call `data_filings` with form 10-K and read the business description, the risk factors and the notes on debt and leases.
4. Call `data_filings` with form 10-Q for the latest quarter and compare it with the same quarter of the year before.
5. Call `quant_call` for the quality and growth criteria the line uses (returns on capital, margins, growth, leverage) from the fundamentals of step 2.
6. Call `data_filings` with form 8-K and list the events of the last twelve months that change the story (management, financing, legal matters).
7. List what you could not get and which conclusion depends on it.

## Synthesis

Write one report with five parts: business, financial quality, balance sheet and cash, open risks and what is missing. Every figure is cited. End with the criteria table and a plain statement of what would change the view.
