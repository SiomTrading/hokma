---
name: macro-context
ref: rec-024
rows: [FT-102, FT-094]
lines: [LP, SWC, RF, DT]
summary: Use macro series (growth, inflation, unemployment, rates, confidence) as slow context for a decision, with the date each figure was known and a smoothing window.
runnable: false
calls: [hokma.data.macro]
---
# Macro context

## Idea

Macro data moves slowly and arrives late. Used well it sets the background of a decision (is growth slowing, is inflation falling, are rates rising); used badly it
tells a backtest what happened before it was published. Two rules keep it honest: every figure carries the date it became known, and the series is smoothed with a window of past
values only. The result is a small board of series, never a trigger on its own.

## Steps

1. One series per question: gross domestic product (growth), consumer prices (inflation), unemployment, the short and long interest rates, consumer confidence, house prices.
2. Ask the data module (`hokma.data.macro`) for the series at an `as_of` date; the cut is applied by the module, and the result says its source and the time it was received.
3. Smooth with a rolling mean of the last periods (three for a noisy monthly print) or sum a trailing window to annualize a quarterly flow. Past values only.
4. Year over year change for series that are levels (`macro.year_over_year`); the figure alone is not comparable between countries.
5. Compare countries on the same date and the same unit. A board with a missing country says so instead of filling the hole.
6. In a backtest, use the release date, not the period the figure describes. A figure for March published in April cannot be used in March.

## Example

The call shape through the data module. It needs a provider key and the network, so this block only has to parse here:

```python illustrative
from datetime import datetime, timezone

from hokma.data import macro

as_of = datetime(2025, 11, 10, tzinfo=timezone.utc)
inflation = macro.macro("CPIAUCSL", as_of=as_of, look_back_days=800)
yoy = macro.year_over_year(inflation.value["observations"])
```

## Checks

- When the provider has no data the answer is `NO_DATA` or `DATA_UNAVAILABLE` with a reason. A gap is reported, never filled.
- A smoothed series lags. A three month mean is two months late by construction; do not read it as a turning point.
- Countries publish on different calendars. Align on the release date before comparing.
- A macro board informs the slow decisions of the lines; it never sets a position size by itself.
