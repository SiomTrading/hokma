---
name: fundamental-ratios
ref: rec-020
rows: [FT-102]
lines: [SWO, LP]
summary: Compute the ratio families of a company from its statements through the fundamentals module, with growth, trailing and a custom ratio written as data.
runnable: true
calls: [hokma.quant.fundamentals.ratios, hokma.quant.fundamentals.formulas]
---
# Fundamental ratios

## Idea

Ratios compress a company into numbers that can be compared across firms and across time: how efficiently assets are used, whether bills can be paid, how much of a
sale becomes profit, how much debt weighs on equity, how expensive the price is. The five families are efficiency, liquidity, profitability, solvency and valuation.
Each ratio is a small formula over lines of the statements, so the value of a recipe is in the order of work: clean statements first, one family at a time,
growth and trailing after.

## Steps

1. Statements come from the data module with their reporting dates and the date they became public (`hokma.data.pit`). A ratio uses the statements that were
   public at the decision date, not the ones that exist today: every function takes `as_of` and leaves out a period filed after it.
2. One family per call (`hokma.quant.fundamentals.ratios`: `fundamentals.efficiency`, `liquidity`, `profitability`, `solvency`, `valuation`). The answer has the series of every
   period and the `latest` value; a ratio that cannot be built is listed in `skipped` with the missing lines.
3. Growth: the ratio against its value `lag` periods ago. Trailing: four quarters summed (`fundamentals.ttm`, which derives the fourth quarter from the year and the nine months and
   says so). Never mix a quarterly figure with an annual one.
4. The formulas that are read are written down: `fundamentals.read_formulas` gives the convention of each one (signs, averages of the opening and closing balance) and why it
   differs from the library figure. A ratio that arrives as free text from an agent is refused.
5. Shareholder yield (dividends plus buybacks over market value) and debt over capital (bounded between zero and one) are the two ratios that correct the usual
   dividend yield and debt over equity when a company buys back shares or has negative equity.

## Example

`STATEMENTS` holds the three companies of the fundamentals fixture (one row per period, with the date each was filed) and `QUARTERLY` the quarterly revenue facts of the first one.
The period of 2024-06 is filed on 2024-08-05, after the cut, so the figures below are those of 2023.

```python runnable
from hokma.quant import registry

alfa, as_of = STATEMENTS["ALFA"], "2024-06-30"
profit = registry.call("fundamentals.profitability", statements=alfa, as_of=as_of).value["latest"]
value = registry.call("fundamentals.valuation", statements=alfa, as_of=as_of).value["latest"]
trailing = registry.call("fundamentals.ttm", facts=QUARTERLY, as_of=as_of).value

# Return on equity: net income 237.6 over the average of the closing equity of 2022 (1030) and of 2023 (1160).
assert abs(profit["return_on_equity"] - 237.6 / ((1030 + 1160) / 2)) < 1e-9
# Earnings yield: earnings per share (237.6 over 98 shares) over the price of the period (60).
assert abs(value["earnings_yield"] - 237.6 / 98 / 60) < 1e-9
# Trailing four quarters: Q2 320 + Q3 335 + Q4 (the year 1300 less the nine months 965 = 335) + Q1 of 2024 340.
assert trailing["value"] == 320 + 335 + (1300 - 965) + 340 and trailing["periods"] == ["2023-Q2", "2023-Q3", "2023-Q4", "2024-Q1"]
# A company with a loss has no price to earnings ratio, and one with no capex line has no free cash flow ratio: empty, never zero.
loss = registry.call("fundamentals.valuation", statements=STATEMENTS["BRAVO"], as_of=as_of).value["latest"]
assert loss["price_to_earnings_ratio"] is None
OK = True
```

## Checks

- Use the figures that were public on the decision date. A restated statement used on an earlier date is look ahead.
- A ratio with a negative or zero denominator is empty with a warning, never infinity.
- Compare within a sector: a ratio that is normal for a utility is a warning for a software company.
- A ratio that needs both statements and prices states the price date next to the statement date.
