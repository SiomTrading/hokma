---
name: indicator-panel
ref: rec-023
rows: [FT-102]
lines: [DT, SWC, SWO, LP]
summary: Build a panel of technical indicators from bars, such as bands, relative strength, trend strength, channels, volume weighted price and volatility cones, through the indicator catalog.
runnable: true
calls: [hokma.quant.indicators.overlap, hokma.quant.indicators.momentum, hokma.quant.indicators.volatility, hokma.quant.indicators.breadth]
---
# Indicator panel

## Idea

An indicator is a function of past bars. A panel puts a few of them side by side so a decision does not rest on one. The useful families are four: overlap
(averages, bands, channels, volume weighted price, parabolic stops, pivot points), momentum (relative strength, trend strength, the Ichimoku lines), volatility (average range, the
cone of historical volatility by horizon) and breadth (new highs against new lows, the advance and decline measures across a basket, which are computed across symbols and not over time).

## Steps

1. Bars first: oldest first, UTC, closed bars only (see own-data-adapter). An indicator computed on a bar that is still forming repaints.
2. Ask the indicator module (`hokma.quant.indicators`: `overlap`, `momentum`, `volatility`, `breadth`) for the name; every indicator is registered as `indicators.<name>` with its inputs and its
   warm-up (`registry.list("indicators")`). `indicators.catalog_value` gives thirteen of them with their use and their cautions. A window can be given, or the `line` scales it (DT short, LP long).
3. Declare each indicator in the `init` of a strategy with its warm-up (see multi-timeframe), so the runner checks that no value changes when later bars are removed. An alpha that reads an
   indicator asks for it with `requests()`, and the runner supplies it from this same module (`hokma.factory.indicator_library`): the number in a backtest is the number here.
4. Keep the panel small: one indicator per question (trend, stretch, volatility, participation). Five indicators that measure the same thing are one vote counted five times.
5. Breadth indicators need a basket; run them on the universe you screened (see universe-screen).

## Example

The panel of the last session of the real daily bars of NQ: a trend line with its bands, the relative strength, the trend strength and the average range.

```python runnable
import numpy as np
import pandas as pd

from hokma.quant import registry

bars = pd.DataFrame({"open": [float(b.open) for b in BARS_DAILY], "high": [float(b.high) for b in BARS_DAILY], "low": [float(b.low) for b in BARS_DAILY],
                     "close": [float(b.close) for b in BARS_DAILY], "volume": [float(b.volume) for b in BARS_DAILY]},
                    index=pd.DatetimeIndex([b.ts for b in BARS_DAILY]))
closes = bars["close"].to_numpy()
bands = registry.call("indicators.bollinger", bars=bars, window=20, num_std_dev=2.0, last_n=1).value["latest"]
strength = registry.call("indicators.rsi", bars=bars, window=14, last_n=1).value["latest"]["rsi"]
trend = registry.call("indicators.adx", bars=bars, window=14, last_n=1).value["latest"]["adx"]
spread = registry.call("indicators.atr", bars=bars, window=14, last_n=1).value["latest"]["atr"]

# The middle band is the simple average of the last 20 closes, and the bands are the same distance from it.
assert abs(bands["middle"] - closes[-20:].mean()) < 1e-6
assert abs((bands["upper"] - bands["middle"]) - (bands["middle"] - bands["lower"])) < 1e-6
# The first value of a window is empty until the warm-up is full: 20 bars of a 20 bar average give a value only from the 20th.
full = registry.call("indicators.sma", bars=bars, window=20, last_n=None).value["values"]["sma"]
assert full[18] is None and abs(full[19] - closes[:20].mean()) < 1e-6
assert 0.0 <= strength <= 100.0 and 0.0 <= trend <= 100.0 and spread > 0.0
OK = True
```

## Checks

- The first values are empty until the warm-up is full. A strategy that trades on an empty value is a bug the runner reports.
- A band or an average over twenty bars on bars of different sizes is a different indicator; name the bar with the indicator.
- Parameters are inputs with limits (`PARAM_CAPS`); an agent cannot ask a window of a million bars.
- An indicator that is positive in every backtest is probably using the future. Run the causal check before the performance.
