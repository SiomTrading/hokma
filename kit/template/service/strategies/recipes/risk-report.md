---
name: risk-report
ref: rec-010
rows: [FT-102, FT-048]
lines: [LP, SWC, SWO, RF]
summary: A risk report of a return series with value at risk, expected shortfall, drawdown measures and volatility, and a backtest of the value at risk model itself.
runnable: true
calls: []
---
# Risk report with a validated value at risk

## Idea

A risk number is a claim that can be tested. The report below asks for the usual figures (value at risk, expected shortfall, drawdown measures,
volatility that reacts to recent returns) and then tests the value at risk like any other model: count the days when the loss was bigger than the
number and compare with what the confidence level promises. A 95 percent value at risk should be broken on about 5 percent of the days, no more and no less.

## Steps

1. Work on periodic returns, one series, dates as the index. Risk figures of the module are positive losses.
2. Report: `risk.var` and `risk.cvar` (historical by default; `gaussian`, `cornish_fisher` and `evt` are the alternatives), `risk.drawdown_risk` with
   the measures `max`, `cdar` and `ulcer`, `metrics.tail_ratio`, `metrics.drawdown_duration` and `risk.ewma_volatility`.
3. Roll the value at risk on a window (`risk.rolling_var`) and use it the next day: the number known at the close of day t judges the return of day t+1.
4. Test it: `risk.var_backtest` with `kupiec` (how often the number is broken) and `christoffersen` (are the breaches clustered).
5. Period views: `metrics.volatility_by_period` with `weekly`, `monthly`, `quarterly` or `yearly`.

## Example

The fixture is 756 real daily returns. Every figure comes from the registry; nothing is computed by hand here.

```python runnable
import pandas as pd

from hokma.quant import registry

report = {
    "var_95": registry.call("risk.var", returns=RETURNS, level=0.95).value,
    "cvar_95": registry.call("risk.cvar", returns=RETURNS, level=0.95).value,
    "var_95_cornish_fisher": registry.call("risk.var", returns=RETURNS, level=0.95, method="cornish_fisher").value,
    "ulcer": registry.call("risk.drawdown_risk", returns=RETURNS, measure="ulcer").value,
    "cdar_95": registry.call("risk.drawdown_risk", returns=RETURNS, measure="cdar", level=0.95).value,
    "tail_ratio": registry.call("metrics.tail_ratio", returns=RETURNS).value,
    "ewma_vol": registry.call("risk.ewma_volatility", returns=RETURNS).value,
}
monthly = registry.call("metrics.volatility_by_period", returns=RETURNS, period="monthly").value

# The expected shortfall is the average loss beyond the value at risk, so it cannot be smaller.
assert report["cvar_95"] >= report["var_95"] > 0 and len(monthly) >= 30

# The number known at the close of day t is the one that judges day t + 1.
rolling = registry.call("risk.rolling_var", returns=RETURNS, window=60, level=0.95).value
known = pd.Series(rolling["values"], index=pd.DatetimeIndex(rolling["index"])).shift(1)
test = registry.call("risk.var_backtest", returns=RETURNS, var_series=known, test="kupiec", level=0.95).value
assert test["observations"] > 600 and 0.0 <= test["p_value"] <= 1.0
assert abs(test["expected_breaches"] - 0.05 * test["observations"]) < 1e-9
print(report, "breaches", test["breaches"], "expected", round(test["expected_breaches"], 1), "kupiec p", round(test["p_value"], 3))
OK = True
```

## Checks

- Breaches far above the expected count mean the model understates risk; far below, that it is too cautious and costs capital. Kupiec says which one is
  statistically different from the promise.
- A value at risk is a loss that is exceeded on the stated share of days; it says nothing about how bad the other days are. The expected shortfall
  and the drawdown measures do.
- The window of the rolling number is a choice that changes the breach count. Test two or three windows and keep the one that passes both tests.
- Not enough observations is an empty result with a warning, never a silent zero.
