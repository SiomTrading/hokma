---
name: period-statistics
ref: rec-012
rows: [FT-102]
lines: [LP, SWC, SWO, RF, DT]
summary: Turn one daily return series into period views (week, month, quarter), growth against a lag, trailing and rolling figures and a standardized score.
runnable: true
calls: []
---
# Period views, growth, trailing, rolling and standardized

## Idea

One return series answers many questions once it is cut by period. Volatility by month tells how risk moves. The change against three months ago tells
whether it is rising. A trailing average smooths a noisy month. A rolling window gives the same figure every day. A standardized score (how many
standard deviations from its own past) puts figures of different scales on one axis. Four options, one rule for all of them: a value at date t may
only use dates up to t.

## Steps

1. Period: `metrics.volatility_by_period` and `metrics.variance_by_period` take `weekly`, `monthly`, `quarterly` or `yearly` and answer one figure per period.
2. Growth: the figure over its own value `lag` periods ago, minus one. A lag of 1 is the last period; lags of 1, 2 and 3 show the trend.
3. Trailing: the mean of the last `n` periods (4 months of volatility, 4 quarters of earnings).
4. Rolling: `metrics.rolling_volatility` and `metrics.rolling_sharpe` with a window in days. One figure per day, no period boundaries.
5. Standardize: `hokma.factory.features.expanding_zscore`: each value against the mean and spread of the values before it and itself, never the whole sample.

## Example

```python runnable
import numpy as np
import pandas as pd

from hokma.factory import features
from hokma.quant import registry

monthly = pd.Series(registry.call("metrics.volatility_by_period", returns=RETURNS, period="monthly").value)
quarterly = pd.Series(registry.call("metrics.volatility_by_period", returns=RETURNS, period="quarterly").value)

growth = {lag: monthly / monthly.shift(lag) - 1 for lag in (1, 2, 3)}            # the change against `lag` months ago
trailing = monthly.rolling(4).mean()                                              # the mean of the last four months
score = features.expanding_zscore(monthly.to_numpy(), 6)                          # needs six months before it speaks
rolling = registry.call("metrics.rolling_volatility", returns=RETURNS, window=60).value

assert len(monthly) > 30 and len(quarterly) >= 10 and len(rolling) == len(RETURNS) - 60 + 1
assert growth[1].iloc[1] == monthly.iloc[1] / monthly.iloc[0] - 1 and growth[3].iloc[:3].isna().all()
assert np.isnan(trailing.iloc[2]) and not np.isnan(trailing.iloc[3])             # four months are needed for a four month mean
assert np.isnan(score[:5]).all() and not np.isnan(score[5])
# The score of the first month with a score does not change when later months are added: nothing in it comes from the future.
assert features.expanding_zscore(monthly.to_numpy()[:12], 6)[5] == score[5]
OK = True
```

## Checks

- A period figure needs enough days: a month with five sessions is not comparable with a month of twenty one. Look at the count before comparing.
- Growth of a figure that crosses zero is meaningless; use the difference instead.
- Do not standardize on the whole sample and then decide on the same sample: the score uses the future mean. The expanding score above does not.
- Weekly figures start on Monday. A period that is still open is not closed: drop it before comparing.
