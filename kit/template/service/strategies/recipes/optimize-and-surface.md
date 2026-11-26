---
name: optimize-and-surface
ref: rec-002
rows: [B-39, B-26, B-28, L-42]
lines: [DT, SWC, SWO, LP]
summary: Search the parameters of a strategy, look at the whole surface and not only at the best point, and report the deflated Sharpe ratio with the number of trials.
runnable: true
calls: []
---
# Optimize, read the surface, deflate the result

## Idea

The best point of a search is the maximum of many noisy numbers, so it is optimistic by construction. Three habits make the number honest. Keep every
point that was tried in the ledger of trials. Look at the surface around the winner: a broad hill is an edge, a single tall cell is luck. And
deflate the Sharpe ratio of the winner with the count of the ledger, which the search fills and the agent never types.

## Steps

1. Write the space as data: one `Axis` per parameter and `Constraint` rules between them (`fast < slow`). A rule cuts a point before it runs.
2. Score all points in one pass with the fast sweep (`hokma.backtest.sweep`) and let the search record each point as a trial. `hokma.backtest.optimize`
   does both for a registered strategy (fast pre-sweep, then the best points confirmed by the engine); `optimize.search` is the same search over any
   scoring function, which is what the example below uses. Confirm the winner with a real run before believing it.
3. Read the surface: `surface.heatmap` for the map, `surface.isolated_peaks` for cells that stand alone.
4. Deflate: `deflated(result, returns, sr_std=...)`. The number of trials comes from the ledger. `sr_std` is the spread of the per period Sharpe of
   all the points that were tried.
5. A winner on the edge of its range (`edge_of_space` in the warnings) says the range was too short, not that the optimum was found.

## Example

A moving average crossover on the real daily NQ sessions of the fixture: five fast lengths by eight slow lengths, forty points. The fixture is
seventy sessions long, so the numbers show how the tools fit together; they are not a result.

```python runnable
from decimal import Decimal

import numpy as np
import pandas as pd

from hokma.backtest import optimize, surface, sweep
from hokma.backtest.optimize import Axis, Constraint, ParamSpace, deflated
from hokma.backtest.spec import BacktestSpec, CostModel
from hokma.quant import metrics

CAPITAL = Decimal("1000000")
spec = BacktestSpec(strategy_id="ma_cross", strategy_version=1, params={}, line="SWC", instruments=(CONTRACT,), bar="1d", start=BARS_DAILY[0].ts,
                    end=BARS_DAILY[-1].ts, as_of=BARS_DAILY[-1].ts, capital_usd=CAPITAL, costs=CostModel("schwab"))
costs = sweep.sweep_costs(spec.costs, tick_size=Decimal("0.25"), multiplier=Decimal(20), price=BARS_DAILY[0].close)


def sma(values, length):
    out = np.full(len(values), np.nan)
    total = np.cumsum(np.insert(values, 0, 0.0))
    out[length - 1:] = (total[length:] - total[:-length]) / length
    return out


def ma_cross(px, point):
    """Long while the fast average of the closes is above the slow one, flat otherwise and before both exist."""
    return np.where(np.nan_to_num(sma(px.close, point["fast"]) - sma(px.close, point["slow"]), nan=-1.0) > 0, 1, 0)


space = ParamSpace((Axis("fast", "int", 2, 10, 2), Axis("slow", "int", 12, 40, 4)), (Constraint("fast", "lt", "slow"),))
points = space.combinations()
swept = sweep.signal_sweep(BARS_DAILY, ma_cross, points, costs, per_bar=True)       # every point in one pass
score = {space.key(p): float(v) for p, v in zip(points, swept.mtm)}
result = optimize.search(spec, space, method="grid", n_trials=len(points), objective="mtm_pnl", presweep=False,
                           evaluate=lambda p: score[space.key(p)], ledger=LEDGER)    # in production the ledger is the default one

# The ledger counts every point that was tried: this is the number of trials the deflated Sharpe uses.
assert len(points) == 40 and result.n_trials == 40 and LEDGER.count("ma_cross") == 40

# The surface: a map of the objective over the two parameters, and the cells that stand alone.
heat = surface.heatmap(result)
assert (heat.x, heat.y, len(heat.cells)) == ("fast", "slow", 40) and heat.svg().startswith("<svg")
peaks = surface.isolated_peaks(result)
assert all(p.label == "isolated_peak" for p in peaks)

# The deflated Sharpe of the winner. sr_std is the spread of the per period Sharpe of every point that was tried.
index = points.index(dict(result.best.params))
dates = pd.DatetimeIndex([b.ts for b in BARS_DAILY])
returns = pd.Series(swept.pnl[:, index] / float(CAPITAL), index=dates)
tried = [metrics.compute(pd.Series(swept.pnl[:, i] / float(CAPITAL), index=dates), ["sharpe"], periods_per_year=1)["sharpe"].value
         for i in range(len(points))]
sr_std = float(np.std([v for v in tried if v is not None]))
dsr = deflated(result, returns, sr_std=sr_std)
assert dsr.params["n_trials"] == result.n_trials == 40
assert dsr.value is None or 0.0 <= dsr.value <= 1.0
print(f"best {dict(result.best.params)}  trials {result.n_trials}  deflated Sharpe {dsr.value}  isolated peaks {len(peaks)}  warnings {result.warnings}")
OK = True
```

## Checks

- The ledger holds forty rows, one per point, and the deflated Sharpe reports `n_trials = 40`. Searching twice doubles the count: the second search
  pays for the first one.
- A value close to 1 for the deflated Sharpe means the winner survives the number of tries; a value near or below 0.5 means a chance result.
- More than 300 points raises `many_trials`. It does not stop the search; the deflation is the brake.
- `edge_of_space:<name>` in the warnings: widen that range and search again before anything else.
- Never read the best cell alone. A plateau around it, with similar values on every side, is the evidence.
