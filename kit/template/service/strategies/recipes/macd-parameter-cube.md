---
name: macd-parameter-cube
ref: rec-006
rows: [V-26, B-32]
lines: [DT, SWC, LP]
summary: Sweep the three lengths of a MACD rule over several time ranges and keep only the settings that work in all of them.
runnable: true
calls: []
---
# MACD settings as a cube over time

## Idea

A MACD rule has three lengths: the fast average, the slow average and the signal average. Every combination is a trial, and a combination that wins on
one stretch of history usually loses on the next. Put the results in a cube (fast x slow x signal x time range) and read it along the time axis:
the mean over ranges ranks the settings, the number of ranges where a setting is positive says how stable it is. A setting with a high mean and a
single good range is a bet on one episode.

## Steps

1. Rule: long while the MACD line is above zero and above its signal line; flat otherwise. The position is the state, so the fast sweep can score it.
2. Space: three lengths with the constraint `fast < slow`. Cut it before running, never after.
3. Split the history into consecutive ranges of equal size. Each range starts cold: its averages warm up inside the range.
4. Score every combination in every range with `hokma.backtest.sweep` (net of the costs of the spec).
5. Rank by the mean over ranges and keep the settings that are positive in every range. Confirm the survivors with real runs.

## Example

Three ranges of twenty three daily sessions of the real NQ fixture and eighteen settings. The ranges are short, so the numbers show the
mechanics, not an edge.

```python runnable
from decimal import Decimal
from itertools import product

import numpy as np

from hokma.backtest import sweep
from hokma.backtest.spec import CostModel

costs = sweep.sweep_costs(CostModel("schwab"), tick_size=Decimal("0.25"), multiplier=Decimal(20), price=BARS_DAILY[0].close)
settings = [{"fast": f, "slow": s, "signal": g} for f, s, g in product((2, 4, 6), (8, 12, 16), (3, 5)) if f < s]


def ema(values, length):
    out, alpha = np.empty(len(values)), 2.0 / (length + 1)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def macd_state(px, point):
    """1 while the MACD line is above zero and above its signal line, else 0. The first slow + signal bars are flat while the averages warm up."""
    line = ema(px.close, point["fast"]) - ema(px.close, point["slow"])
    signal = ema(line, point["signal"])
    state = ((line > 0) & (line > signal)).astype(int)
    state[: point["slow"] + point["signal"]] = 0
    return state


ranges = [BARS_DAILY[i:i + 23] for i in (0, 23, 46)]
assert [len(r) for r in ranges] == [23, 23, 23] and len(settings) == 18
cube = np.vstack([sweep.signal_sweep(r, macd_state, settings, costs).mtm for r in ranges])        # (ranges, settings), USD

mean = cube.mean(axis=0)
positive = (cube > 0).sum(axis=0)
stable = [i for i in range(len(settings)) if positive[i] == len(ranges)]
best_mean = int(np.argmax(mean))
best_single = int(np.argmax(cube[0]))
print("best by mean", settings[best_mean], round(float(mean[best_mean]), 2), "stable settings", [settings[i] for i in stable],
      "best of the first range", settings[best_single], "its mean over ranges", round(float(mean[best_single]), 2))

# The mean over ranges is never above the best single range, and a stable setting is positive everywhere by construction.
assert cube.shape == (3, 18) and mean[best_mean] <= cube.max(axis=0)[best_mean] + 1e-9
assert all((cube[:, i] > 0).all() for i in stable)
OK = True
```

## Checks

- The ranking by mean and the best of the first range usually name different settings. That gap is the overfit the cube exists to show.
- Costs are in the score: a setting that trades every bar pays two sides each time (tick and commission).
- Every setting that was scored is a trial. When the settings go through `optimize` each one is in the ledger; with a plain sweep count them yourself before deflating a result,
  and expect the warning `many_trials` above 300 settings.
- Ranges of equal size keep the means comparable. Overlapping ranges make the evidence look larger than it is.
