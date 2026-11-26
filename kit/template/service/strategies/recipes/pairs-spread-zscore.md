---
name: pairs-spread-zscore
ref: rec-005
rows: [V-26, B-35]
lines: [SWC, LP]
summary: Trade the gap between two related instruments from the z-score of a rolling regression spread, with both legs sized by the hedge ratio.
runnable: true
calls: []
---
# Pairs: the spread, its z-score and the signals

## Idea

Two instruments that move together leave a gap that comes back. The gap is the residual of a regression of one price on the other, measured in
standard deviations of its own recent history. When the gap is wide the pair opens (sell the rich leg, buy the cheap one, sized by the slope of
the regression); when the gap closes the pair is flat. Two traps decide the quality of the study: the regression and the z-score must use only
the window that ends at the bar being decided, and the number of pairs tested must be paid for with a multiple testing correction.

## Steps

1. Work on log prices. For each bar, fit `log(a) = slope x log(b) + intercept` on the last `window` bars and take the residual of the last bar as the
   spread (`hokma.quant.validation.stat_tests.ols` fits the line; nothing here is solved by hand).
2. Standardize the spread over the last `window` spreads (`hokma.factory.features.rolling_zscore`).
3. A z-score above `upper` sells the spread, below `-upper` buys it, and a pair that is open closes when the z-score comes back inside `exit`.
4. Both legs are two order intents sent in the same bar, sized by the slope. The decision uses the close of the bar and fills at the next open
   of each leg, like any other order.
5. Several pairs: count every pair that was tried as a trial and correct the p values (`hokma.quant.validation.multiple_testing`).

## Example

The two legs are the real one minute bars of ES and MES (the e-mini and the micro of the same index) from the multi symbol fixture: fourteen bars,
so the window is short and the output is only the signal logic checked end to end.

```python runnable
import numpy as np

from hokma.factory import features
from hokma.quant.validation import stat_tests

WINDOW, UPPER, EXIT = 6, 1.0, 0.25
log_a = np.log([float(b.close) for b in MULTI["ES"]])
log_b = np.log([float(b.close) for b in MULTI["MES"]])


def spread_of(a, b, window):
    """Residual of the last bar of each rolling regression of a on b. NaN until the window is full."""
    out = np.full(len(a), np.nan)
    for end in range(window, len(a) + 1):
        fit = stat_tests.ols(a[end - window:end], b[end - window:end].reshape(-1, 1))
        const, slope = fit["beta"]
        out[end - 1] = a[end - 1] - (slope * b[end - 1] + const)
    return out


def states(z):
    """The wanted position of the spread after each bar: -1 short, +1 long, 0 flat. A position closes inside the exit band, never in between."""
    out, now = np.zeros(len(z), dtype=int), 0
    for i, value in enumerate(z):
        if np.isnan(value):
            now = 0
        elif now == 0 and value > UPPER:
            now = -1
        elif now == 0 and value < -UPPER:
            now = 1
        elif now != 0 and abs(value) < EXIT:
            now = 0
        out[i] = now
    return out


spread = spread_of(log_a, log_b, WINDOW)
z = features.rolling_zscore(spread, WINDOW)
wanted = states(z)

# The first spread needs one full window of prices; the first z-score needs one more full window of spreads.
assert np.isnan(spread[:WINDOW - 1]).all() and not np.isnan(spread[WINDOW - 1])
assert np.isnan(z[:2 * WINDOW - 2]).all() and not np.isnan(z[2 * WINDOW - 2])

# Nothing in the signal comes from the future: cutting the series anywhere leaves the earlier states unchanged.
for cut in range(WINDOW + 2, len(log_a)):
    part = states(features.rolling_zscore(spread_of(log_a[:cut], log_b[:cut], WINDOW), WINDOW))
    assert (part == wanted[:cut]).all()
print("spread", np.round(spread, 6), "z", np.round(z, 3), "wanted", wanted)
OK = True
```

The two legs of a signal, as intents, for a run of both instruments (`hokma.backtest.runner.run_many`, the instruments of one run are limited by
`n_instruments`):

```python illustrative
from hokma.factory.orders import buy, sell

def legs(wanted_now, slope, a="FUT:ES:2025-12", b="FUT:MES:2025-12"):
    """Short spread: sell a, buy b scaled by the slope. Long spread: the opposite."""
    if wanted_now == -1:
        return [sell(a, 1, tag="pair_short"), buy(b, max(1, round(slope)), tag="pair_short")]
    if wanted_now == 1:
        return [buy(a, 1, tag="pair_long"), sell(b, max(1, round(slope)), tag="pair_long")]
    return []
```

## Checks

- The fixture has the same index in two sizes, so the slope is close to one and the gap is made of ticks. A real pair is two different
  instruments; the logic is the same.
- The cut test above is the guard against look ahead: it must keep passing when the window or the thresholds change.
- A pair with a short leg needs shares to borrow or a contract that can be sold; if the broker cannot lend them the pair is dropped.
- Test many pairs and the best one is luck. Count them, correct the p values, and run the walk forward of the strategy before believing one.
