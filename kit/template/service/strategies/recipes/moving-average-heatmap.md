---
name: moving-average-heatmap
ref: rec-009
rows: [V-26, B-39, L-17]
lines: [SWC, LP]
summary: Test a dual moving average rule against holding and against random trading with the same number of trades, on a grid of lengths, for any market including crypto.
runnable: true
calls: []
---
# Dual moving average: grid, hold and random

## Idea

A dual moving average rule goes long while a fast average is above a slow one. It always looks good on a chart. Two controls turn it into a test:
holding the instrument for the whole period, and trading at random moments with exactly the same number of trades and the same holding time as the rule.
If the rule does not beat most of the random runs, its timing adds nothing. Run it on a grid of lengths and the same controls say whether the whole region
works or only one cell.

## Steps

1. Long while `fast average > slow average`, flat otherwise. The state of each bar is the signal; fills happen at the next open plus a tick.
2. One pass of the fast sweep over the grid of lengths (`hokma.backtest.sweep`), costs from the fee table of the market.
3. Hold: buy at the first open and sell at the last close with the same costs (`hokma.backtest.placebo.buy_and_hold`).
4. Random: `placebo.random_signals` draws signals with the trades and holding time of the rule at random moments; the share of random runs that the rule beats
   is its rank among its controls.
5. Crypto: the same code on 24 hour daily or hourly bars, with a percentage fee table (`coinbase`, `kraken`, `alpaca` in `backend/hokma/broker/fees/`)
   and the engine run for the final numbers, because a percentage fee follows the price and the sweep takes one fee for the whole run.

## Example

The fixture is the real NQ daily sessions, seventy bars. One rule (6 and 20 bars) is ranked against two hundred random runs with its own number of trades.

```python runnable
from decimal import Decimal

import numpy as np

from hokma.backtest import placebo, sweep
from hokma.backtest.spec import CostModel

model = CostModel("schwab")
costs = sweep.sweep_costs(model, tick_size=Decimal("0.25"), multiplier=Decimal(20), price=BARS_DAILY[0].close)
px = sweep.arrays(BARS_DAILY)


def sma(values, length):
    out = np.full(len(values), np.nan)
    total = np.cumsum(np.insert(values, 0, 0.0))
    out[length - 1:] = (total[length:] - total[:-length]) / length
    return out


def dual_average(arrays, point):
    return np.where(np.nan_to_num(sma(arrays.close, point["fast"]) - sma(arrays.close, point["slow"]), nan=-1.0) > 0, 1, 0)


rule = sweep.signal_sweep(BARS_DAILY, dual_average, [{"fast": 6, "slow": 20}], costs)
signal = dual_average(px, {"fast": 6, "slow": 20})
trades = int(rule.trades[0])
assert trades >= 1

hold = placebo.buy_and_hold(BARS_DAILY, tick_size=Decimal("0.25"), multiplier=Decimal(20), model=model)

# Random trading with the number of trades and the holding time of the rule.
mean_hold = max(1, round(int(signal[:-1].sum()) / trades))
random_runs = placebo.random_signals(len(BARS_DAILY), n_trades=trades, mean_hold=mean_hold, seed=7, count=200)
random_result = sweep.sweep_signals(px, random_runs, costs)
rank = float((random_result.mtm < rule.mtm[0]).mean())
print("rule", float(rule.mtm[0]), "trades", trades, "mean hold", mean_hold, "hold price return", float(hold.price_return), "beats random", rank)

assert random_runs.shape == (200, len(BARS_DAILY)) and (np.abs(random_runs).sum(axis=1) > 0).all()
assert 0.0 <= rank <= 1.0 and hold.bars == len(BARS_DAILY)

# The same rule on a grid: the whole region is judged by its mean, not by its best cell.
grid = [{"fast": f, "slow": s} for f in (2, 4, 6, 8, 10) for s in (12, 16, 20, 24, 28, 32, 36, 40)]
swept = sweep.signal_sweep(BARS_DAILY, dual_average, grid, costs)
assert len(swept.mtm) == 40 and swept.order()[0] == int(np.argmax(swept.mtm))
OK = True
```

## Checks

- The random controls have the trade count of the rule: more trades pay more costs, and a control with a different count is not a control.
- A rank near 1 means the rule beats nearly all random timing; near 0.5 it is indistinguishable from a coin. Seventy sessions cannot tell the two apart, so read
  the rank on years of data and many seeds.
- The hold reference pays one entry and one exit with the same ticks and commission as the rule, so the comparison is net on both sides.
- Percentage fees (crypto tables) depend on the price: use the engine run for the final figure.
