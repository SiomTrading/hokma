---
name: time-series-forecast
ref: rec-019
rows: [AS-015, B-37]
lines: [SWC, SWO, LP]
summary: Evaluate any forecast of the next return against simple baselines in a walk forward, with a test of equal accuracy, before it is allowed near a strategy.
runnable: true
calls: [hokma.quant.stats.timeseries]
---
# Time series forecasts in a walk forward

## Idea

A forecast model (an ARIMA, an exponential smoothing, a volatility model) is judged against the cheapest forecasts there are: zero, the last value, the mean of the
training window. If a model does not beat them out of sample, the model is decoration. The evaluation is always the same: fit on a window, forecast the next
step, store the error, move the window, and compare the error series with a test of equal predictive accuracy. The model itself is a function of
`hokma.quant.stats.timeseries`; the evaluation is the same code for every model.

## Steps

1. The target is the return of the next day, so a forecast made at the close of day t is compared with day t+1. Rows with no next day are dropped.
2. Walk forward with `hokma.backtest.walkforward.refit` (rolling windows, a purge of one bar). A forecast of row t may only use rows up to t.
3. Baselines: zero, and the mean of the training window. Add the last value for a level series.
4. Keep the error of every test row. `validation.forecast_tests` gives RMSE and MAE (`accuracy`, with an out of sample split) and the Diebold-Mariano test of two
   error series: a positive statistic favours the second model.
5. A model enters the second block the same way: its forecast replaces the baseline in the same loop. The statistics come from the module, never from a
   library imported by the agent.

## Example

The 756 real daily returns of the quant fixture, five rolling windows. The two baselines are compared; the comparison is the template for any model.

```python runnable
import numpy as np

from hokma.backtest import walkforward
from hokma.quant import registry

values = RETURNS.to_numpy()
data = {"ret": values, "next": np.append(values[1:], np.nan)}
errors = {"zero": [], "mean": []}


def fit(view):
    target = view.get("next")
    return float(np.mean(target[np.isfinite(target)]))                     # the mean of the next day returns seen in training


def score(mean, view):
    target = view.get("next")
    ok = np.isfinite(target)
    errors["zero"].extend((target[ok] - 0.0).tolist())
    errors["mean"].extend((target[ok] - mean).tolist())
    return float(np.sqrt(np.mean((target[ok] - mean) ** 2)))


result = walkforward.refit(data, fit=fit, score=score, scheme="rolling", n_folds=5, purge=1, embargo=0)
assert result.leakage_free and len(result.windows) >= 4 and len(errors["zero"]) == len(errors["mean"]) > 300

test = registry.call("validation.forecast_tests", test="diebold_mariano", errors_a=errors["zero"], errors_b=errors["mean"]).value
accuracy = registry.call("validation.forecast_tests", test="accuracy", actual=errors["zero"], forecast=[0.0] * len(errors["zero"])).value
assert test["p_value"] is None or 0.0 <= test["p_value"] <= 1.0
assert abs(accuracy["rmse"] - float(np.sqrt(np.mean(np.square(errors["zero"]))))) < 1e-12      # the module and the arithmetic agree
print("window rmse", [round(w.score, 5) for w in result.windows], "dm statistic", None if test["statistic"] is None else round(test["statistic"], 3))
OK = True
```

A model from the statistics module goes into the same loop. It is built by another block of the plan (H3), so this block only has to parse here:

```python illustrative
from hokma.quant import registry


def fit(view):
    # Module: hokma.quant.stats.timeseries (models). List the registered names with registry.list("stats") and read the inputs of the one you pick
    # with registry.describe(name); the name and the arguments below are the shape, not a promise of the module.
    return registry.call("stats.arima", values=view.get("ret"), order=[1, 0, 0])
```

## Checks

- Both baselines are in the table. A model that loses to zero on a return series is the usual case, and a useful result.
- The error series are collected row by row across windows; `diebold_mariano` needs at least ten paired errors.
- The target of a row is the next day. A model that is given the same day return as input and the same day return as target looks perfect and is worthless.
- Forecasting prices (levels) needs a unit root check first; forecast returns unless there is a reason not to.
