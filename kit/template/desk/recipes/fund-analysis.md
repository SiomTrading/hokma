---
name: fund-analysis
lines: [LP, RF]
summary: Review of a fund or an ETF, what it holds, what it costs and how it behaved.
---
# Recipe: fund-analysis

Follow the steps in order. Cite each result as it arrives, as the citation rules require. Every call carries the cut-off
time of the view as its as_of. If a call returns no data, unavailable or stale, write that in the report and go on with
the remaining steps.

1. Call `data_snapshot` with the symbol and the categories profile and price to get the type, the expense ratio and the assets.
2. Call `data_filings` with the latest holdings report of the fund and list the top positions and the weights by sector or by issuer.
3. Call `data_bars` with a daily interval for the longest window available and the same window for the benchmark.
4. Call `quant_call` for return, volatility, drawdown and the capture ratios against the benchmark, from the series of step 3.
5. For a fixed income fund, call `data_macro` for the yield curve series and note the duration exposure in the holdings.
6. Compare the fund with two alternatives of the same type on the same dates.

## Synthesis

Write a report with what the fund holds, what it costs, how it behaved against its benchmark and the alternatives. Declare any period or figure the source did not provide.
