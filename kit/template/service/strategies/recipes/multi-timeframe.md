---
name: multi-timeframe
ref: rec-001
rows: [B-38, B-30]
lines: [DT, SWC]
summary: Read a higher timeframe from a strategy that decides on a short bar, without ever seeing a bar that is still forming.
runnable: true
calls: []
---
# Higher timeframe context

## Idea

A strategy that decides on one minute bars often wants the state of a slower frame: is the five minute RSI stretched, is the daily trend up.
The trap is the bar that is still forming. At 10:03 the ten o'clock five minute bar does not exist yet, and a rule that reads it is reading the
future. `hokma.factory.timeframes.resample_closed` gives every row the latest bar of the higher frame that closed at or before the open of that
row, so the rule can use it freely.

## Steps

1. Pick the frame for the line. DT: `5min` or `15min` on minute bars. SWC: `1D` and `1W` on daily bars (weeks start on Monday).
2. Compute the indicator on the closed bars of the higher frame only, once per bar, and show the value on every row of the fast frame.
3. Declare it with `ctx.indicator`. The runner checks that the value at a bar does not change when later bars are removed.
4. Decide on the fast frame. Orders still fill at the next open of the fast frame plus a tick.

## Example

The function below is the whole recipe. `htf_rsi` builds the closed bars of a frame from the minute closes it is given, computes a Wilder RSI on
those bars and puts each value back on the rows that can see it. The strategy buys when the five minute RSI is below `low` and leaves when it is
above `high`.

```python runnable engine
from decimal import Decimal

import numpy as np
import pandas as pd

from hokma.backtest import run
from hokma.backtest.runner import Services
from hokma.backtest.spec import BacktestSpec, CostModel
from hokma.factory.orders import buy, reduce_intent
from hokma.factory.params import Param, ParamSchema
from hokma.factory.timeframes import resample_closed

TIMES = pd.DatetimeIndex([b.ts for b in BARS_DAY])


def wilder_rsi(values, length):
    out = np.full(len(values), np.nan)
    if len(values) <= length:
        return out
    change = np.diff(values)
    gain, loss = np.maximum(change, 0.0), np.maximum(-change, 0.0)
    avg_gain, avg_loss = gain[:length].mean(), loss[:length].mean()
    for i in range(length, len(change) + 1):
        if i > length:
            avg_gain = (avg_gain * (length - 1) + gain[i - 1]) / length
            avg_loss = (avg_loss * (length - 1) + loss[i - 1]) / length
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def htf_rsi(values, rule, length):
    """RSI of the closed bars of `rule`, shown on every row of the fast frame. Rows with no closed bar hold NaN."""
    frame = pd.DataFrame({"close": np.asarray(values, dtype=float)}, index=TIMES[: len(values)])
    higher = resample_closed(frame, rule)
    first = higher["htf_end"].notna() & ~higher["htf_end"].duplicated()      # the first row that sees each closed bar
    rsi = wilder_rsi(higher.loc[first, "close"].to_numpy(), length)
    slot = first.cumsum().to_numpy() - 1                                      # which closed bar each row sees
    return np.where(higher["htf_end"].notna().to_numpy(), rsi[np.clip(slot, 0, None)], np.nan)


class FiveMinuteRsi:
    params = ParamSchema((Param("length", "int", 5, 2, 50), Param("low", "float", 40.0, 1.0, 49.0), Param("high", "float", 60.0, 51.0, 99.0)))

    def init(self, ctx):
        n = ctx.params["length"]
        ctx.indicator("rsi_5m", htf_rsi, warmup=5 * (n + 2), rule="5min", length=n)

    def on_bar(self, ctx):
        value = ctx.indicators["rsi_5m"].last
        held = ctx.position is not None and ctx.position.qty != 0
        if not held and value < ctx.params["low"]:
            return [buy(CONTRACT, 1, tag="rsi_low")]
        if held and value > ctx.params["high"]:
            return [reduce_intent(CONTRACT, "buy", abs(ctx.position.qty), tag="rsi_high")]
        return []


# The row that opens at minute 5 sees the bar of minutes 0 to 4, closed at minute 5: its close is the close of minute 4. Minute 4 is not visible
# at minute 4 itself, because that bar was still forming.
closes = [float(b.close) for b in BARS_DAY]
frame = resample_closed(pd.DataFrame({"close": closes}, index=TIMES), "5min")
assert frame["close"].iloc[5] == closes[4] and frame["close"].iloc[9] == closes[4] and np.isnan(frame["close"].iloc[4])

# The indicator does not move when later bars are removed.
full = htf_rsi(closes, "5min", 5)
assert np.allclose(htf_rsi(closes[:200], "5min", 5), full[:200], equal_nan=True)

spec = BacktestSpec(strategy_id="five_minute_rsi", strategy_version=1, params={}, line="DT", instruments=(CONTRACT,), bar="1m",
                    start=BARS_DAY[0].ts, end=BARS_DAY[-1].ts, as_of=BARS_DAY[-1].ts, capital_usd=Decimal("1000000"), costs=CostModel("schwab"))
services = Services(db=DB, source=source_for(BARS_DAY), curve_dir=CURVES, strategy=lambda params: FiveMinuteRsi())
result = run(spec, services=services)
assert result.status == "done" and len(result.trades) >= 1
OK = True
```

For SWC the same function reads `1D` and `1W` on a daily series; for the daily rule use the open of the next session as the offset when the trading day
starts the evening before (`offset="18h"`).

## Checks

- The value at row 5 is the close of minute 4, and the value at row 4 is empty. A rule that sees minute 4 at minute 4 reads the future.
- Cutting the series anywhere does not change the earlier values (the causal check of the runner).
- The warm-up counts fast bars: a five minute RSI of length 5 needs about 35 minute bars before its first value.
- Frames are `5min`, `15min`, `1h`, `1D` and `1W` (a count and a unit); anything else is refused.
