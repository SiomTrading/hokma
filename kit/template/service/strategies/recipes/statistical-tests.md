---
name: statistical-tests
ref: rec-015
rows: [FT-102]
lines: [SWC, LP, SWO]
summary: Test a relationship before trading it with regression, residual checks and a multiple testing correction, and leave unit roots, causality and cointegration to the statistics module.
runnable: true
calls: [hokma.quant.stats.regression, hokma.quant.stats.timeseries, hokma.quant.stats.cointegration]
---
# Statistical tests before a trade

## Idea

A correlation on a chart is a hypothesis, not a finding. The routine has four questions. Does the relationship survive a regression with the other drivers
included? Are the residuals well behaved, so the p values can be trusted? Is it a same period move or does one series lead the other? And, when many pairs or
settings were tried, how many survive the price of trying them? The answers decide whether a study is worth a backtest.

## Steps

1. Regression: the strategy returns on the benchmark (or on the other series) with a constant. `validation.specification_tests` reports heteroscedasticity
   (`breusch_pagan`, `white`), autocorrelation (`durbin_watson`) and a misspecification check (`reset`).
2. Residuals: `validation.residual_diagnostics` with `jarque_bera` (normal tails?), `ljung_box` (memory?), `arch_lm` (volatility clusters?) and
   `variance_ratio` (random walk?). A rejected normality check weakens the t statistics, so use the robust versions.
3. Lead and lag, unit roots and cointegration live in `hokma.quant.stats` (`timeseries`: Granger, unit root, models; `cointegration`; `regression`: panel).
   Prices are not stationary and returns are; a long run link between two prices is a different claim from a same week co-movement.
4. Many tests: `validation.multiple_testing` with `bh` (false discovery rate), `holm` or `bonferroni`. Test twenty pairs and one will pass by luck.

## Example

The strategy and benchmark returns of the quant fixture. The multiple testing line is checked by hand: five p values sorted, each one times five over its rank, then
made monotone from the end.

```python runnable
from hokma.quant import registry

bench = BENCHMARK.to_numpy().reshape(-1, 1)
spec = registry.call("validation.specification_tests", y=RETURNS.to_numpy(), x=bench).value
resid = registry.call("validation.residual_diagnostics", residuals=RETURNS, tests=["jarque_bera", "ljung_box", "arch_lm", "variance_ratio"]).value

assert set(spec) >= {"breusch_pagan", "white", "durbin_watson", "reset"} and set(resid) == {"jarque_bera", "ljung_box", "arch_lm", "variance_ratio"}
assert all(0.0 <= resid[name]["p_value"] <= 1.0 for name in resid)
assert 1.0 < spec["durbin_watson"]["statistic"] < 3.0               # no strong first order autocorrelation

# By hand: p values 0.001 0.02 0.04 0.3 0.7 -> times 5 over the rank: 0.005 0.05 0.0667 0.375 0.7; the first two stay under 0.05 after the correction.
fixed = registry.call("validation.multiple_testing", p_values=[0.001, 0.02, 0.04, 0.3, 0.7], method="bh").value
assert [round(v, 4) for v in fixed["adjusted"]] == [0.005, 0.05, 0.0667, 0.375, 0.7] and fixed["reject"] == [True, True, False, False, False]
OK = True
```

The lead lag, unit root and cointegration checks. They are built by another block of the plan (H3), so this block only has to parse here:

```python illustrative
from hokma.quant import registry

# Modules: hokma.quant.stats.timeseries (granger, unit_root), hokma.quant.stats.cointegration, hokma.quant.stats.regression (panel).
lead = registry.call("stats.granger", y=aapl_returns, x=supplier_returns, lags=2)
root = registry.call("stats.unit_root", values=prices)
link = registry.call("stats.cointegration", a=prices_a, b=prices_b)
```

## Checks

- Non rejection is not proof: a test with 756 points can miss a small effect. Say what the test could not see.
- Return series are used for regressions and Granger checks; price levels are used only for cointegration, after a unit root test.
- A relationship found on the same week and not on the next one is not a trade. Granger asks whether the past of one helps predict the other.
- Every pair tried counts. Keep the number, correct the p values, and then run the strategy through the walk forward.
