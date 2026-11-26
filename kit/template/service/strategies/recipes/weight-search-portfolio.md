---
name: weight-search-portfolio
ref: rec-008
rows: [V-26, B-26]
lines: [LP]
summary: Search portfolio weights at random, read the best one as an optimistic number, deflate it by the number of tries and compare it with the optimizers of the portfolio module.
runnable: true
calls: [hokma.quant.portfolio.optimize]
---
# Portfolio weights: random search, deflated

## Idea

The simplest way to find weights is to draw many at random and keep the one with the best Sharpe ratio. It works as a demonstration and fails as a
method for the usual reason: the best of a thousand draws is the best of a thousand noisy numbers. Keep the search, add two safeguards. Every draw
is a trial, and the Sharpe of the winner is deflated by the number of trials. And the winner is compared with the closed form and clustering
optimizers of the portfolio module, which have no number of tries to pay for.

## Steps

1. Build the returns of each asset from its closes. One column per asset, one row per period, all in the same period.
2. Draw weights that are positive and sum to one (long only, fully invested), with a seeded generator so the search can be repeated.
3. Score each draw by the Sharpe ratio of the portfolio returns (`hokma.quant.metrics`, per period).
4. Deflate the best score with `hokma.quant.validation.dsr`: the number of trials is the number of draws and `sr_std` is the spread of the Sharpe
   ratios of all the draws.
5. Compare with the baselines: equal weights, and the optimizers of `hokma.quant.portfolio.optimize` (see the second block).
6. Rebalance rule: weights are re-estimated on a rolling window and applied from the next period. A weight estimated on the whole sample and used
   on the same sample is look ahead.

## Example

Three real instruments of the multi symbol fixture, ES, MES and CL, on their one minute bars: thirteen returns each. The sample is far too short
to choose weights; the block shows the mechanics and the size of the deflation.

```python runnable
import numpy as np
import pandas as pd

from hokma.quant import metrics, validation

assets = ["ES", "MES", "CL"]
closes = pd.DataFrame({a: [float(b.close) for b in MULTI[a]] for a in assets}, index=pd.DatetimeIndex([b.ts for b in MULTI["ES"]]))
returns = closes.pct_change().dropna()
assert returns.shape == (13, 3)

DRAWS = 300
rng = np.random.default_rng(42)
weights = rng.random((DRAWS, 3))
weights = weights / weights.sum(axis=1, keepdims=True)                       # positive, sum to one


def per_period_sharpe(series):
    return metrics.compute(series, ["sharpe"], periods_per_year=1)["sharpe"].value


sharpes = [per_period_sharpe(returns @ w) for w in weights]
valid = [i for i, s in enumerate(sharpes) if s is not None]
best = max(valid, key=lambda i: sharpes[i])
equal = per_period_sharpe(returns.mean(axis=1))

sr_std = float(np.std([sharpes[i] for i in valid]))
deflated = validation.dsr(returns @ weights[best], n_trials=DRAWS, sr_std=sr_std)
print("best weights", np.round(weights[best], 3), "sharpe", round(sharpes[best], 3), "equal weight", round(equal, 3), "deflated", deflated.value)

assert np.allclose(weights.sum(axis=1), 1.0) and (weights > 0).all()
assert sharpes[best] >= max(sharpes[i] for i in valid)                        # the best draw is the best of the draws, by construction
assert deflated.params["n_trials"] == DRAWS
assert deflated.value is None or 0.0 <= deflated.value <= 1.0
OK = True
```

The optimizers of the portfolio module do the same job without a search. They are built by another block of the plan, so this block only has to parse
here; the call shape follows the interface of the module:

```python illustrative
from hokma.quant import registry

# Through the registry, like every quant function. Names of the module: mean_var, hrp, erc, inverse_vol.
weights = registry.call("portfolio.hrp", returns=returns)
```

## Checks

- The weights sum to one and none is negative (long only). A short leg needs its own rule and its own cost of borrow.
- The deflated Sharpe uses the number of draws. Drawing ten times more does not find ten times more edge: it lowers the deflated value.
- Compare with equal weights before anything else. A search that cannot beat 1/N after costs is not worth its complexity.
- The fixture is thirteen returns. With a sample this short the covariance of three assets is not estimable; a real run needs years of daily data.
