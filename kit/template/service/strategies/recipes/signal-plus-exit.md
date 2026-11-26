---
name: signal-plus-exit
ref: rec-003
rows: [B-40, B-08, V-10, V-14]
lines: [DT, SWC]
summary: One entry signal and three independent ways out (a hard stop, a trailing exit and a time limit), each one a rule that can be removed on its own.
runnable: true
calls: []
---
# Entry signal with a stop, a trailing exit and a time limit

## Idea

An entry rule says when to be in. Everything that decides how the trade ends is a second, separate rule, and it is worth more than the first. This
recipe keeps the pieces apart: the signal opens the trade, a hard stop travels with the order, the trailing exit and the time limit are
read on closed bars. The engine tags a hard stop exit as `stop` and any exit of the strategy as `exit`, so the table of trades separates the stop
from the rest; the entry keeps the tag of the rule that opened it.

## Steps

1. Entry: close above the highest high of the last `lookback` bars. The order fills at the next open plus a tick against.
2. Hard stop: `sl=Pct(stop_pct)` on the entry order. It is a bracket child, filled by the engine; when stop and target fall in the same bar the stop
   goes first, and a gap fills at the open and not at the stop price.
3. Trailing exit: keep the highest high since the entry. When a close is more than `trail_pct` percent under it, send a reduce-only order. A stop
   is never moved against the position.
4. Time limit: after `max_bars` bars in the trade, send a reduce-only order.
5. DT: the same shape on minute bars, with the distances in ticks (`Ticks(20)`) and the limit in minutes. SWC: daily bars and percentages, as below.

## Example

```python runnable engine
from decimal import Decimal

from hokma.backtest import run
from hokma.backtest.runner import Services
from hokma.backtest.spec import BacktestSpec, CostModel
from hokma.factory.orders import Pct, buy, reduce_intent
from hokma.factory.params import Param, ParamSchema


class BreakoutWithExits:
    params = ParamSchema((Param("lookback", "int", 10, 2, 200), Param("stop_pct", "float", 2.0, 0.1, 20.0),
                          Param("trail_pct", "float", 1.5, 0.1, 20.0), Param("max_bars", "int", 8, 1, 200)))

    def __init__(self):
        self.entered_at, self.peak = None, 0.0

    def init(self, ctx):
        ctx.set_warmup(ctx.params["lookback"] + 1)

    def on_bar(self, ctx):
        p = ctx.params
        close, high = float(ctx.data.close.last), float(ctx.data.high.last)
        held = ctx.position is not None and ctx.position.qty != 0
        if not held:
            self.entered_at = None
            if close > ctx.data.high.tail(p["lookback"] + 1)[:-1].max():                 # the signal: a breakout of the last highs
                self.entered_at, self.peak = ctx.index, high
                return [buy(CONTRACT, 1, sl=Pct(p["stop_pct"]), tag="breakout")]
            return []
        self.peak = max(self.peak, high)
        if close < self.peak * (1 - p["trail_pct"] / 100.0):
            return [reduce_intent(CONTRACT, "buy", abs(ctx.position.qty), tag="trail")]
        if ctx.index - self.entered_at >= p["max_bars"]:
            return [reduce_intent(CONTRACT, "buy", abs(ctx.position.qty), tag="time_limit")]
        return []


spec = BacktestSpec(strategy_id="breakout_exits", strategy_version=1, params={}, line="SWC", instruments=(CONTRACT,), bar="1d", start=BARS_DAILY[0].ts,
                    end=BARS_DAILY[-1].ts, as_of=BARS_DAILY[-1].ts, capital_usd=Decimal("1000000"), costs=CostModel("schwab"))
services = Services(db=DB, source=source_for(BARS_DAILY), curve_dir=CURVES, strategy=lambda params: BreakoutWithExits())
result = run(spec, services=services)
assert result.status == "done" and len(result.trades) >= 1
position = {b.ts: i for i, b in enumerate(BARS_DAILY)}
for trade in result.trades:
    assert trade.entry_tag == "breakout" and trade.exit_at is not None
    assert position[trade.exit_at] - position[trade.entry_at] <= 8 + 1                    # the time limit, plus the bar of the fill
print([(t.exit_tag, str(t.pnl_usd)) for t in result.trades])
OK = True
```

## Checks

- Every trade has the tag of its entry (`breakout`). Group the table by the exit tag: `stop` trades are the ones the hard stop closed, `exit` trades
  are the ones the strategy closed itself.
- No trade is longer than `max_bars` plus the bar of the fill.
- The stop sits on the order, so it still works when the strategy stops getting data. The trailing exit and the time limit need the next bar to act.
- Run the same strategy with each exit removed in turn. An exit that changes nothing is dead weight; one that changes everything is the strategy.
