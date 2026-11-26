---
name: own-data-adapter
ref: rec-017
rows: [FT-102, FT-101]
lines: [DT, SWC, SWO, LP, RF]
summary: Feed a backtest from your own bars the safe way, with the oldest bar first, one row per bar, the same hash on any machine and a loud failure on bad rows.
runnable: true
calls: []
---
# Bring your own data

## Idea

A backtest is only as good as its bars. Your own file is welcome as long as it passes the same door as the data of the service: the loader refuses
what it cannot trust (a negative volume, a bar with an impossible range) and it never repairs history, because a simulation on repaired data is a
fiction. Two details break most imports: the rows come newest first, and names of columns differ. Fix the order and the names in the adapter, once,
and keep the hash of what was loaded so any result can be traced to its bars.

## Steps

1. Normalize the names to the five fields `open`, `high`, `low`, `close`, `volume`, with a time index in UTC, one row per bar.
2. Sort oldest first. A source in descending order is common; a loader that assumes ascending order reads the future as the past.
3. Wrap the adapter as a fetch function that returns a `DataResult` (`hokma.data.sentinels.ok`) and give it to `hokma.backtest.data_feed.load`.
4. Keep `data_hash`: the fingerprint of the rows. The same rows give the same hash, so a changed file is a different run.
5. Rows that are bad are reported with the first date and stop the load. Do not drop them silently.

## Example

The real daily NQ bars of the fixture, given to the loader newest first, as a typical export would be.

```python runnable
from datetime import timezone

import pandas as pd

from hokma.backtest import data_feed
from hokma.data import sentinels

ascending = pd.DataFrame({"open": [float(b.open) for b in BARS_DAILY], "high": [float(b.high) for b in BARS_DAILY], "low": [float(b.low) for b in BARS_DAILY],
                          "close": [float(b.close) for b in BARS_DAILY], "volume": [float(b.volume) for b in BARS_DAILY]},
                         index=pd.DatetimeIndex([b.ts for b in BARS_DAILY], name="time"))
export = ascending.iloc[::-1]                                                       # newest first, as the file came


def adapter(frame):
    """The fetch function of an own file: sorted oldest first, one row per bar, never repaired."""
    def fetch(symbol, interval, *, start, end, as_of):
        ordered = frame.sort_index()
        assert not ordered.index.duplicated().any(), "two rows for the same bar"
        return sentinels.ok(ordered, source="own_file", as_of=as_of)
    return fetch


start, end = ascending.index[0].to_pydatetime(), ascending.index[-1].to_pydatetime()
loaded = data_feed.load("NQ", "1d", start=start, end=end, as_of=end, fetch=adapter(export))
assert loaded.frame.index.is_monotonic_increasing and len(loaded.frame) == len(BARS_DAILY) and loaded.source == "own_file"
assert loaded.data_hash == data_feed.data_hash(ascending)                            # the order of the file does not change the identity of the bars

# A bad row is a failed load that says where: it is never repaired.
broken = ascending.copy()
broken.iloc[10, broken.columns.get_loc("volume")] = -1.0
try:
    data_feed.load("NQ", "1d", start=start, end=end, as_of=end, fetch=adapter(broken))
    raise AssertionError("a negative volume must stop the load")
except data_feed.DataFeedError as error:
    assert error.code == "negative_volume" and error.at is not None
OK = True
```

## Checks

- The hash of the descending and the ascending file is the same only because the adapter sorts. If it does not, fix the adapter, not the data.
- A gap in the bars stays a gap. A minute with no trade is missing, not zero.
- Column names differ by provider; keep the mapping in the adapter and in one place. A mapping typed inside a strategy is a bug waiting.
- Time zones: bars are UTC in the loader; convert at the edge, once, and write the zone of the source in the notes.
