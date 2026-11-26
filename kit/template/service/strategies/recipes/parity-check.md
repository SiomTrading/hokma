---
name: parity-check
ref: rec-007
rows: [V-26, B-32, B-35]
lines: [DT, SWC]
summary: When two ways of running the same rule disagree, compare the signals first, then the fills, then the costs, and explain the gap to the cent.
runnable: true
calls: []
---
# Parity between the fast sweep and the engine run

## Idea

Tiny differences make big gaps: a different average, a fill one tick away, a commission counted once. When two runs of the same rule give different
results, do not trust either. Peel the difference in order: first the signals (same bars in, same entries and exits out?), then the fills (same
price per trade?), then the costs (same ticks and commission?). Each layer explains part of the gap and the rest moves down to the next layer.
The fast sweep and the engine run of Hokma agree by design; the exercise below changes one cost on one side and accounts for every dollar.

## Steps

1. Build the rule once as a state per bar (long or flat). Feed that same state to the sweep and to the strategy of the engine run.
2. Compare the realized result (the closed trades) of both. With the same costs they match. A lot that is still open at the last bar is marked by
   each side with its own rule, so it stays out of the parity.
3. Change one thing on one side only (here, two ticks against instead of one: the slippage of the costs and the next open fill rule move together,
   the spec refuses them apart) and predict the gap by hand before running it.
4. Run and compare the gap with the prediction. A gap that the prediction does not explain is a bug in one of the two sides, and the layer that
   was last changed is the first place to look.

## Example

A five bar RSI that buys under 35 and leaves over 55, on the real daily NQ sessions. One tick of NQ is 0.25 points, 5 USD with a multiplier of 20.

```python runnable engine
from decimal import Decimal

import numpy as np

from hokma.backtest import run, sweep
from hokma.backtest.runner import Services
from hokma.backtest.spec import BacktestSpec, CostModel, FillRules
from hokma.factory.orders import buy, reduce_intent
from hokma.factory.params import ParamSchema


def wilder_rsi(values, length):
    out = np.full(len(values), np.nan)
    change = np.diff(values)
    gain, loss = np.maximum(change, 0.0), np.maximum(-change, 0.0)
    avg_gain, avg_loss = gain[:length].mean(), loss[:length].mean()
    for i in range(length, len(change) + 1):
        if i > length:
            avg_gain = (avg_gain * (length - 1) + gain[i - 1]) / length
            avg_loss = (avg_loss * (length - 1) + loss[i - 1]) / length
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def rsi_state(values, length=5, low=35.0, high=55.0):
    """1 from the bar where the RSI is under `low` until the bar where it is over `high`; 0 otherwise."""
    rsi, out, now = wilder_rsi(np.asarray(values, dtype=float), length), np.zeros(len(values)), 0
    for i, value in enumerate(rsi):
        now = 1 if value < low else 0 if value > high else now
        out[i] = now if not np.isnan(value) else 0
    return out


class RsiState:
    params = ParamSchema(())

    def init(self, ctx):
        ctx.indicator("state", rsi_state, warmup=7)

    def on_bar(self, ctx):
        wanted = ctx.indicators["state"].last
        held = ctx.position is not None and ctx.position.qty != 0
        if wanted == 1 and not held:
            return [buy(CONTRACT, 1, tag="rsi_state")]
        if wanted == 0 and held:
            return [reduce_intent(CONTRACT, "buy", abs(ctx.position.qty))]
        return []


def spec_with(ticks):
    return BacktestSpec(strategy_id="rsi_state", strategy_version=1, params={}, line="SWC", instruments=(CONTRACT,), bar="1d", start=BARS_DAILY[0].ts,
                        end=BARS_DAILY[-1].ts, as_of=BARS_DAILY[-1].ts, capital_usd=Decimal("1000000"), costs=CostModel("schwab", slippage_ticks=ticks),
                        fill=FillRules(next_open_ticks=ticks))


def engine(ticks):
    services = Services(db=DB, source=source_for(BARS_DAILY), curve_dir=CURVES, strategy=lambda params: RsiState())
    return run(spec_with(ticks), services=services)


# Layer 1, the signals: both sides read the same state vector, so they cannot differ.
closes = np.array([float(b.close) for b in BARS_DAILY])
state = rsi_state(closes)
fast = sweep.signal_sweep(BARS_DAILY, lambda px, point: rsi_state(px.close), [{}], sweep.sweep_costs(CostModel("schwab"), tick_size=Decimal("0.25"),
                          multiplier=Decimal(20), price=BARS_DAILY[0].close))

# Layers 2 and 3, fills and costs: same costs, same result to the cent. The comparison is on the closed trades; a lot that is still open at the
# last bar is marked by each side with its own rule and is left out of the parity.
base = engine(1)
assert base.status == "done" and abs(float(base.realized_pnl_usd) - float(fast.realized[0])) < 0.005

# Predict the gap by hand: two ticks instead of one is one more tick (5 USD) on each of the two fills of every closed trade.
closed = sum(t.exit_at is not None for t in base.trades)
slipped = engine(2)
assert closed >= 2 and abs(float(base.realized_pnl_usd - slipped.realized_pnl_usd) - 5.0 * 2 * closed) < 0.005
print("closed trades", closed, "sweep", float(fast.realized[0]), "engine", float(base.realized_pnl_usd), "slipped engine", float(slipped.realized_pnl_usd))
OK = True
```

## Checks

- The signal layer is where real ports fail: two definitions of an average or of an RSI give different entries on a handful of bars, and that is
  enough to change the result. Compare the entry and exit bars first (a set difference), then the numbers.
- A gap that is a whole number of ticks times the number of fills is slippage. A gap that is a multiple of the commission is a fee.
- Changing the fee table or the ticks on one side only is a test, never a setting. Both sides read the same table and the same ticks in production.
- Keep the check in the strategy folder: the day an engine upgrade changes a fill, the gap is the first symptom.
