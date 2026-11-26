---
name: universe-screen
ref: rec-013
rows: [FT-102, FT-097]
lines: [SWO, LP, SWC]
summary: Find candidates from a universe with filters and a movers table (gainers, losers, most active) before any strategy is written.
runnable: true
calls: []
---
# Universe screen and movers

## Idea

Before a strategy there is a list: which instruments are worth looking at today. Two tools make the list. The instrument catalog filters a whole asset
class by plain columns (sector, country, exchange) and gives the symbols. A movers table ranks those symbols by what they did over a window: the biggest
gainers, the biggest losers and the most active. The list is a candidate list, never a signal: it feeds a study, and the study decides.

## Steps

1. Filter the asset class in the catalog (`hokma.data.universe.universe`): the filters are columns and the result says it is today's listing, so a
   past date carries survivorship bias and the warning stays in the result.
2. Check the identifiers of the result (`reconcile_identifiers`) before joining other data to them.
3. Pull the closes of the symbols found and build the movers table: return over the window, and volume over the same window.
4. Rank three ways (gainers, losers, most active). Keep the top of each as candidates and write down the date of the screen.
5. Live lists of the day (gainers, losers, most active, sectors) come from `hokma.data.universe.discover`, which needs the key of the provider and says so when it is missing.

## Example

The movers table of the three instruments of the multi symbol fixture (real one minute bars of ES, MES and CL): return from the first open to the last
close, and the volume traded.

```python runnable
import pandas as pd

rows = {}
for symbol, bars in MULTI.items():
    rows[symbol] = {"return": float(bars[-1].close / bars[0].open - 1), "volume": float(sum(b.volume for b in bars)), "last": float(bars[-1].close)}
table = pd.DataFrame(rows).T

gainers = table.sort_values("return", ascending=False)
losers = table.sort_values("return", ascending=True)
active = table.sort_values("volume", ascending=False)

# By hand: the best performer is the one with the highest value in the column, the worst is the lowest, the busiest has the most volume.
assert gainers.index[0] == max(rows, key=lambda s: rows[s]["return"]) and losers.index[0] == min(rows, key=lambda s: rows[s]["return"])
assert active.index[0] == max(rows, key=lambda s: rows[s]["volume"])
assert gainers.index[0] != losers.index[0] and list(table.columns) == ["return", "volume", "last"]
print(table)
OK = True
```

The catalog and the live lists need the catalog files or a provider key, so this block only has to parse here:

```python illustrative
from datetime import datetime, timezone

from hokma.data import universe

now = datetime.now(timezone.utc)
found = universe.universe(asset_class="equities", as_of=now, sector="Technology", country="United States")
movers = universe.discover("biggest_gainers", as_of=now)
```

## Checks

- The catalog describes instruments as they are now. Never build a past universe from it without reading the survivorship warning.
- A movers table of three instruments says nothing about the market; the point is the shape: one row per symbol, one column per measure.
- Volume and return are on the same window. A table that mixes windows ranks noise.
- A candidate list never goes straight to an order. It goes to a study, a judge and a desk.
