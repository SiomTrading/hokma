---
name: regime-filter
ref: rec-014
rows: [FT-102]
lines: [DT, SWC, SWO, LP]
summary: Split history into a calm and a stormy regime with only the information known at the time, and measure what each regime pays.
runnable: true
calls: [hokma.quant.stats.regime, hokma.quant.stats.hurst]
---
# Volatility regimes without look ahead

## Idea

Many rules work in one kind of market and fail in the other. A regime filter says which market it is today and switches the rule on or off. The simplest
regime is volatility: calm when the recent volatility is under its own usual level, stormy when it is above. The two tests that matter are timing
and size. Timing: the regime known at the close of day t judges day t+1 and nothing earlier. Size: both regimes need enough days to say anything.

## Steps

1. Volatility of the last `window` days (`metrics.rolling_volatility`), one value per day.
2. Its usual level: the median of the values up to that day, never the whole sample (an expanding median).
3. Regime of day t: stormy when the volatility is above the median known at t; calm otherwise. Days before there is any level are neither.
4. Judge the next day: group the return of day t+1 by the regime of day t and compute the same metrics in each group.
5. Model based regimes (hidden Markov, Markov switching) and the Hurst exponent (trending against mean reverting) come from `hokma.quant.stats`
   (`regime`, `hurst`): see the second block. Treat them like this one: filter on information known at t.

## Example

The 756 real daily returns of the quant fixture, window of 20 days.

```python runnable
import numpy as np
import pandas as pd

from hokma.quant import metrics, registry

WINDOW = 20


def regime_of(returns):
    """+1 stormy, -1 calm, 0 unknown, from the volatility of the last WINDOW days and the median of the volatilities known so far."""
    vol = pd.Series(registry.call("metrics.rolling_volatility", returns=returns, window=WINDOW).value)
    vol.index = pd.DatetimeIndex(vol.index)
    usual = vol.expanding().median()
    out = pd.Series(np.where(vol > usual, 1, -1), index=vol.index)
    return out.reindex(returns.index, fill_value=0)


regime = regime_of(RETURNS)
tomorrow = RETURNS.shift(-1)                                                    # the return of the next day, known only afterwards
calm, stormy = tomorrow[regime == -1].dropna(), tomorrow[regime == 1].dropna()

# Nothing comes from the future: cutting the history anywhere leaves the earlier regimes unchanged.
for cut in (300, 500, 700):
    assert (regime_of(RETURNS.iloc[:cut]) == regime.iloc[:cut]).all()
assert (regime.iloc[:WINDOW - 1] == 0).all() and len(calm) > 100 and len(stormy) > 100

by_regime = {name: metrics.compute(group, ["sharpe", "volatility"])["sharpe"].value for name, group in (("calm", calm), ("stormy", stormy))}
print({k: round(v, 2) for k, v in by_regime.items()}, "days", len(calm), len(stormy))
OK = True
```

The model based regimes. They are built by another block of the plan (H3), so this block only has to parse here:

```python illustrative
from hokma.quant import registry

# Module names: hokma.quant.stats.regime (states of a series) and hokma.quant.stats.hurst (trending or mean reverting).
states = registry.call("stats.regime", returns=RETURNS)
hurst = registry.call("stats.hurst", values=prices)
```

## Checks

- The cut test is the guard against look ahead. It must pass again every time the window or the level changes.
- Both regimes have more than a hundred days here. With a few dozen days a regime Sharpe is noise.
- A regime that changes every day is a coin. Count the switches; add a minimum stay if it flips too often.
- The filter reduces the number of trades. Compare the filtered rule with the plain one after costs, over several periods, before keeping it.
