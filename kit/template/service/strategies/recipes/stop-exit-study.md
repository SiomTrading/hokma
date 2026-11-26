---
name: stop-exit-study
ref: rec-004
rows: [V-26, B-40]
lines: [DT, SWC]
summary: Compare a hard stop, a target, a trailing exit, a random exit and plain holding over many entry windows before trusting any stop.
runnable: true
calls: []
---
# Which exit earns: stop, target, trailing, random or hold

## Idea

A stop looks wise on one chart. The honest question is what it does on average, over many entries, against the two baselines that cost nothing:
holding to the end of the window and leaving at a random moment. The study cuts the history into windows, enters once at the start of each window,
and runs the same entries with a different exit rule each time. The rule is judged by its expectancy per trade, not by its best case.

## Steps

1. Cut the series into windows of equal length. Enter at the first bar of each window, always on the same side.
2. One run per exit type, same entries: `stop` (bracket stop), `target` (bracket target), `trail` (exit on a close under the peak), `random` (a
   seeded random holding time) and `hold` (leave at the end of the window). Every type also leaves at the end of the window.
3. Costs are the ones of the spec: the same table, the same ticks. A stop that fires often pays the cost often.
4. Per exit type: trades, win rate and expectancy = win rate x average win - (1 - win rate) x average loss. The module gives the rates; the
   expectancy is the line above.
5. Repeat for several distances. Trust a distance only when its neighbours behave alike: that is the surface of recipe optimize-and-surface.

## Example

Windows of twenty daily sessions of the real NQ fixture. The sample is seventy sessions, so three windows close and the fourth is cut by the end of
the data and its trade stays open under plain holding; read it as a template for a long study, not as a result.

```python runnable engine
from decimal import Decimal

import numpy as np
import pandas as pd

from hokma.backtest import run
from hokma.backtest.runner import Services
from hokma.backtest.spec import BacktestSpec, CostModel
from hokma.factory.orders import Pct, buy, reduce_intent
from hokma.factory.params import ParamSchema
from hokma.quant import registry

WINDOW = 20


class WindowEntry:
    """Buys at the first bar of every window and leaves by the rule of `kind`, or at the end of the window at the latest."""

    params = ParamSchema(())

    def __init__(self, kind, distance=2.0, seed=7):
        self.kind, self.distance, self.peak = kind, distance, 0.0
        self.random_stay = np.random.default_rng(seed).integers(1, WINDOW - 1, size=16)      # bars held, one draw per window

    def init(self, ctx):
        pass

    def on_bar(self, ctx):
        phase, window = (ctx.index - 1) % WINDOW, (ctx.index - 1) // WINDOW
        held = ctx.position is not None and ctx.position.qty != 0
        close, high = float(ctx.data.close.last), float(ctx.data.high.last)
        if not held:
            if phase != 0:
                return []
            self.peak = high
            bracket = {"stop": {"sl": Pct(self.distance)}, "target": {"tp": Pct(self.distance)}}.get(self.kind, {})
            return [buy(CONTRACT, 1, tag=self.kind, **bracket)]
        self.peak = max(self.peak, high)
        leave = [reduce_intent(CONTRACT, "buy", abs(ctx.position.qty))]
        if phase == WINDOW - 1:
            return leave
        if self.kind == "trail" and close < self.peak * (1 - self.distance / 100.0):
            return leave
        if self.kind == "random" and phase >= self.random_stay[window]:
            return leave
        return []


spec = BacktestSpec(strategy_id="stop_study", strategy_version=1, params={}, line="SWC", instruments=(CONTRACT,), bar="1d", start=BARS_DAILY[0].ts,
                    end=BARS_DAILY[-1].ts, as_of=BARS_DAILY[-1].ts, capital_usd=Decimal("1000000"), costs=CostModel("schwab"))
table = {}
for kind in ("stop", "trail", "random", "hold"):
    services = Services(db=DB, source=source_for(BARS_DAILY), curve_dir=CURVES, strategy=lambda params, kind=kind: WindowEntry(kind))
    result = run(spec, services=services)
    assert result.status == "done"
    closed = [t for t in result.trades if t.exit_at is not None]
    returns = pd.Series([float(t.pnl_usd) / 1_000_000 for t in closed])
    stats = registry.call("metrics.win_rate_stats", returns=returns).value
    win, avg_win, avg_loss = stats["win_rate"], stats["avg_win"] or 0.0, stats["avg_loss"] or 0.0
    table[kind] = {"entries": len(result.trades), "trades": len(closed), "win_rate": win, "expectancy_usd": 1_000_000 * (win * avg_win - (1 - win) * abs(avg_loss))}

# Every type enters once per window, four times, whatever the exit: the entries are the same and only the exits differ.
assert all(row["entries"] == 4 for row in table.values())
assert set(table) == {"stop", "trail", "random", "hold"} and table["hold"]["trades"] == 3      # the fourth window is cut by the end of the data
print(table)
OK = True
```

## Checks

- Every type has the same number of entries. If a type has fewer, a rule is skipping trades and the comparison is no longer fair.
- A stop beats holding only when its expectancy is higher after costs, over many windows. One window proves nothing.
- The random exit is the control: a rule that cannot beat a seeded random holding time has no timing skill.
- Give each type the same distance in the units of the contract. Percent of the entry price (`Pct`), ticks (`Ticks`) and average range (`ATR`) are
  different experiments.
