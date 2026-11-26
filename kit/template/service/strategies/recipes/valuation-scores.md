---
name: valuation-scores
ref: rec-021
rows: [FT-102]
lines: [SWO, LP]
summary: Read a company with the classic scores and models, the DuPont split of return on equity, bankruptcy and quality scores, enterprise value and a discounted cash flow with its checks.
runnable: true
calls: [hokma.quant.fundamentals.dupont, hokma.quant.fundamentals.scores, hokma.quant.fundamentals.value, hokma.quant.fundamentals.cost_of_capital]
---
# Valuation models and scores

## Idea

Four questions, four tools. Where does the return on equity come from? The DuPont split says profitability, asset efficiency or leverage. Is the company in
trouble or in good health? The Altman score for distress, the Piotroski score for improving quality (nine yes or no tests) and the Beneish score for earnings that
look managed. What is the business worth? The enterprise value breakdown and a discounted cash flow, with a cost of capital. And how fast can it grow without outside money? The
sustainable and the internal growth rates. A score is a screen: it ranks, it does not decide.

## Steps

1. DuPont: return on equity = net margin x asset turnover x equity multiplier (`fundamentals.dupont`; the `extended` form splits the margin into tax burden, interest burden and
   operating margin). Check that the product returns the reported return on equity.
2. Scores (`fundamentals.scores`): `altman_z`, `piotroski_f`, `beneish_m`. Each reports its components and the zone, so the reader sees which of the nine tests failed.
3. Value (`fundamentals.value`): the enterprise value breakdown (`ev_breakdown`: market value, debt, preferred, minority interest, cash) and `intrinsic`, which values by a method (the
   Gordon model, a two stage dividend model, free cash flow to the firm or to equity, a discounted cash flow). A rate at or below the growth rate gives no value, and the answer says why.
4. The cost of capital is an input with a source, not a constant (`fundamentals.wacc`, `cost_of_equity`). Show the value for three discount rates; a value that flips with a point of rate has no margin of safety.
5. Compare the model value with the price, and keep the margin of safety rule of the line.

## Example

`STATEMENTS` holds the three companies of the fundamentals fixture. The scores use the periods filed by the cut (2024-06-30); the one filed on 2024-08-05 is left out.

```python runnable
from hokma.quant import registry

alfa, as_of = STATEMENTS["ALFA"], "2024-06-30"
split = registry.call("fundamentals.dupont", statements=alfa, as_of=as_of).value["latest"]
health = registry.call("fundamentals.piotroski_f", statements=alfa, as_of=as_of).value["latest"]["piotroski_f"]
distress = registry.call("fundamentals.altman_z", statements=alfa, as_of=as_of).value
capital = registry.call("fundamentals.wacc", statements=alfa, risk_free_rate=0.04, beta=1.1, market_return=0.09, as_of=as_of).value
gordon = registry.call("fundamentals.intrinsic", method="gordon", params={"dividends_per_share": 2.0, "rate_of_return": 0.09, "growth_rate": 0.04})
no_value = registry.call("fundamentals.intrinsic", method="gordon", params={"dividends_per_share": 2.0, "rate_of_return": 0.04, "growth_rate": 0.05})

# DuPont of 2023: margin 237.6 / 1300 x turnover 1300 / 1825 (average assets) x multiplier 1825 / 1095 (average equity) = 237.6 / 1095.
assert abs(split["Net Profit Margin"] * split["Asset Turnover"] * split["Equity Multiplier"] - 237.6 / 1095) < 1e-9
assert abs(split["Return on Equity"] - 237.6 / 1095) < 1e-9
assert health == 7.0 and distress["zone"] == "safe"
# Cost of equity by CAPM: 0.04 + 1.1 x (0.09 - 0.04) = 0.095.
assert abs(capital["cost_of_equity"] - 0.095) < 1e-12
# Gordon model: 2 x 1.04 / (0.09 - 0.04) = 41.6. A rate below the growth rate has no value, and says so.
assert abs(gordon.value["value"] - 41.6) < 1e-9 and no_value.value is None and no_value.warnings
OK = True
```

## Checks

- A score from a stale statement is stale. Show the date of the statement with the score.
- The DuPont product must equal the reported return on equity within rounding; if it does not, a line is mapped wrongly.
- A perpetual growth rate close to the discount rate makes the value explode. The check refuses it; do not work around the check.
- Scores built for one kind of company mislead on another (banks, insurers). Name the universe where the score was validated.
