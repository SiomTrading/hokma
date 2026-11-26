---
name: risk-adjusted-performance
ref: rec-011
rows: [FT-102]
lines: [LP, SWC, SWO]
summary: Judge a return series against a benchmark with Sharpe, Sortino, Treynor, alpha, beta and rolling ratios, and add factor exposures through the statistics module.
runnable: true
calls: [hokma.quant.stats.regression]
---
# Risk adjusted performance against a benchmark

## Idea

A return means little without the risk taken to get it and the benchmark it should beat. The standard set is small: Sharpe (return per unit of total
volatility), Sortino (only the downside counts), Treynor (return per unit of market beta), Jensen's alpha (what is left after the market part), and the
same figures on a rolling window to see whether the quality is stable or lives in one year. A factor regression goes one step further and says what the
series is made of: market, size, value, profitability, investment.

## Steps

1. Two series on the same dates: the strategy and the benchmark. Periodic returns, net of costs.
2. `metrics.sharpe`, `metrics.sortino`, `metrics.treynor_ratio`, `metrics.alpha_beta`, `metrics.capm`, `metrics.information_ratio`.
3. Rolling view: `metrics.rolling_sharpe` with a window of a quarter or a half year; a Sharpe that swings between very good and very bad is one episode.
4. Period view: the same ratios by month or by year through the `*_by_period` functions of `hokma.quant.metrics`.
5. Factors: the regression of the returns on the factor returns is a function of `hokma.quant.stats.regression` (second block); the factor data comes
   from the data module, never from a file typed by hand.

## Example

```python runnable
from hokma.quant import registry

table = {
    "sharpe": registry.call("metrics.sharpe", returns=RETURNS).value,
    "sortino": registry.call("metrics.sortino", returns=RETURNS).value,
    "treynor": registry.call("metrics.treynor_ratio", returns=RETURNS, benchmark=BENCHMARK).value,
    "information": registry.call("metrics.information_ratio", returns=RETURNS, benchmark=BENCHMARK).value,
}
fit = registry.call("metrics.alpha_beta", returns=RETURNS, benchmark=BENCHMARK).value
capm = registry.call("metrics.capm", returns=RETURNS, benchmark=BENCHMARK).value
rolling = registry.call("metrics.rolling_sharpe", returns=RETURNS, window=120).value

# The beta of the CAPM view and the beta of the regression view are the same number, computed twice by the module.
assert abs(fit["beta"] - capm["beta"]) < 1e-9
assert len(rolling) == len(RETURNS) - 120 + 1
print(table, fit, "rolling sharpe range", round(min(rolling.values()), 2), round(max(rolling.values()), 2))
OK = True
```

The factor regression. It is built by another block of the plan (H3), so this block only has to parse here:

```python illustrative
from hokma.quant import registry

# Factor returns come from the data module on the same dates as the strategy. The module name is hokma.quant.stats.regression.
exposure = registry.call("stats.regression", y=RETURNS, x=factors)
```

## Checks

- Sharpe, Sortino and Treynor answer different questions: total risk, downside risk and market risk. A series that ranks well on all three is the one to keep.
- The rolling Sharpe has a range. If its minimum is below zero for long stretches, the average hides how the strategy feels to hold.
- Alpha without a standard error is an opinion. Look at the t statistic of the regression before calling any intercept an edge.
- A benchmark of another asset class makes the beta meaningless. Match the benchmark to the instrument.
