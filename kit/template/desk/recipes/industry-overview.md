---
name: industry-overview
lines: [LP]
summary: Map of an industry and of where one company sits in it.
---
# Recipe: industry-overview

Follow the steps in order. Cite each result as it arrives, as the citation rules require. Every call carries the cut-off
time of the view as its as_of. If a call returns no data, unavailable or stale, write that in the report and go on with
the remaining steps.

1. Call `data_universe` with the sector and industry filters to list the companies of the industry.
2. Call `data_snapshot` for the three or four largest names with the categories profile and valuation.
3. Call `data_fundamentals` for each of them, annual income statement, and compare growth and margins on the same dates.
4. Call `data_macro` for the macro series that drive the industry (rates, prices of inputs, demand indicators) and note the vintage.
5. Call `quant_call` for the dispersion of growth, margins and valuation across the names.
6. Place the company under review inside that dispersion and say what explains the gap, if there is one.

## Synthesis

Write a one page map: the shape of the industry, the forces that move it, a table of the peers on the same dates and the position of the company. State which names or data you left out.
