"""Builds the bar in progress of every interval that has a subscription (any time interval of `intervals.py`) from
ticks or from 1m bar updates, in the session of the symbol (`sessions.py`).

Bars start at the anchor of the session of the instrument (futures 18:00 New York, forex 17:00 New York, crypto 00:00
UTC, stocks the regular open of the exchange) and count from there: the same rule the history uses. A day is a session
and a week starts on Monday (futures and forex: Sunday evening).

The intervals are a list that the hub keeps in step with the subscriptions: `add_interval` when the first panel
subscribes to one, `remove_interval` when the last one leaves. Whatever the list, the aggregator keeps the 1-minute
bars of the last days, so an interval added late starts from what already happened. Intervals under a minute, or
not a whole number of minutes, need ticks and are not built from 1m bar updates.

`on_tick` and `on_bar_update` return the bars that changed as `(interval, Candle, final)`. A bar is final when the
next one has started (or the provider said so for a 1m bar); each bar is final once.
"""
from collections.abc import Iterable
from datetime import tzinfo
from functools import lru_cache

from hokma.market.intervals import IntervalError, TimeInterval, of_seconds, parse_time
from hokma.market.models import TIMEFRAMES, Candle
from hokma.market.sessions import Session, session_for
from hokma.market.stream import BarUpdate

Change = tuple[str, Candle, bool]
KEEP_SECONDS = 8 * 86400
PRUNE_SLACK = 600                                   # minutes of slack before the buffer is trimmed again


@lru_cache(maxsize=512)
def _parse(text: str) -> TimeInterval:
    return parse_time(text)


def _from_minutes(interval: TimeInterval) -> bool:
    """Bars made of whole 1-minute bars: at least a minute and a whole number of them."""
    return interval.seconds >= 60 and interval.seconds % 60 == 0


class BarAggregator:
    def __init__(self, tz: tzinfo, kind: str = "equity", intervals: tuple[str, ...] | list[str] = TIMEFRAMES) -> None:
        self.tz = tz
        self.kind = kind
        self.session: Session = session_for(kind, tz)
        self._intervals: dict[str, TimeInterval] = {}
        self._cur: dict[str, Candle] = {}
        self._minutes: dict[int, Candle] = {}
        self._last_minute = 0
        self._finalized: dict[str, int] = {}
        for interval in intervals:
            self.add_interval(interval)

    # ---- the list of intervals ----

    @property
    def intervals(self) -> tuple[str, ...]:
        return tuple(self._intervals)

    def add_interval(self, interval: str) -> bool:
        """Start building `interval`. True when it was new. Raises `IntervalError` for text that is not a time interval."""
        if interval in self._intervals:
            return False
        parsed = _parse(interval)
        if parsed.canonical != interval:
            raise IntervalError("invalid")
        self._intervals[interval] = parsed
        if _from_minutes(parsed) and self._last_minute:
            start, end = self.bounds(self._last_minute, parsed)
            self._cur[interval] = self._fold(start, end)
        return True

    def remove_interval(self, interval: str) -> bool:
        if self._intervals.pop(interval, None) is None:
            return False
        self._cur.pop(interval, None)
        self._finalized.pop(interval, None)
        return True

    # ---- time buckets ----

    def bounds(self, ts: int, interval: "str | int | TimeInterval") -> tuple[int, int]:
        """`[start, end)` of the bar of `interval` that holds the instant `ts` (epoch seconds). `interval` is the
        canonical text (`7m`, `1D`), a parsed interval, or a plain number of seconds (counted from the anchor)."""
        if isinstance(interval, str):
            interval = _parse(interval)
        elif isinstance(interval, int):
            interval = of_seconds(interval)
        return self.session.bounds(ts, interval)

    def current(self, timeframe: str) -> Candle | None:
        return self._cur.get(timeframe)

    def minute_candles(self) -> list[Candle]:
        """The 1-minute bars held (the last days), oldest first."""
        return [self._minutes[t] for t in sorted(self._minutes)]

    def replay(self, interval: str, ticks: Iterable[tuple[int, float, float]]) -> list[Candle]:
        """Builds the bars of a watched `interval` from stored ticks `(ts_ms, price, size)`, oldest first, and leaves
        the aggregator ready to go on with live ticks. Returns every bar, the one in progress last. The interval must
        have been added. This gives the history of the bars that the history source cannot serve (seconds)."""
        parsed = self._intervals[interval]
        bars: list[Candle] = []
        cur: Candle | None = None
        for ts_ms, price, size in ticks:
            ts = int(ts_ms // 1000)
            start, _ = self.bounds(ts, parsed)
            volume = float(size or 0)
            if cur is None or start > cur.t:
                if cur is not None:
                    bars.append(cur)
                cur = Candle(start, price, price, price, price, volume)
            elif start == cur.t:
                cur = Candle(cur.t, cur.o, max(cur.h, price), min(cur.l, price), price, cur.v + volume)
        if bars:
            self._finalized[interval] = bars[-1].t
        if cur is not None:
            self._cur[interval] = cur
            bars.append(cur)
        return bars

    # ---- inputs ----

    def on_tick(self, ts_ms: int, price: float, size: float | None) -> list[Change]:
        ts = ts_ms // 1000
        volume = float(size or 0)
        self._add_to_minute(ts // 60 * 60, price, volume)
        changes: list[Change] = []
        for name, interval in self._intervals.items():
            start, _ = self.bounds(ts, interval)
            cur = self._cur.get(name)
            if cur is None or start > cur.t:
                if cur is not None and self._finalized.get(name) != cur.t:
                    self._finalized[name] = cur.t
                    changes.append((name, cur, True))
                cur = Candle(start, price, price, price, price, volume)
            elif start == cur.t:
                cur = Candle(cur.t, cur.o, max(cur.h, price), min(cur.l, price), price, cur.v + volume)
            else:
                continue                               # a late tick for a bar that is already behind
            self._cur[name] = cur
            changes.append((name, cur, False))
        self._prune(ts)
        return changes

    def on_bar_update(self, update: BarUpdate) -> list[Change]:
        start_s = update.start_ms // 1000 // 60 * 60                   # a bar starts on a minute, whatever the provider sent
        minute = Candle(start_s, update.o, update.h, update.l, update.c, update.v)
        self._minutes[start_s] = minute
        self._last_minute = max(self._last_minute, start_s)
        changes: list[Change] = []
        for name, interval in self._intervals.items():
            if not _from_minutes(interval):
                continue                               # seconds, or a length that is not whole minutes, need ticks
            start, end = self.bounds(start_s, interval)
            bar = minute if interval.seconds == 60 else self._fold(start, end)
            cur = self._cur.get(name)
            if cur is not None and start < cur.t:
                continue                               # a correction for a bar already behind
            if cur is not None and start > cur.t and self._finalized.get(name) != cur.t:
                self._finalized[name] = cur.t
                changes.append((name, cur, True))
            self._cur[name] = bar
            closed = interval.seconds == 60 and update.final
            if closed and self._finalized.get(name) != bar.t:
                self._finalized[name] = bar.t
                changes.append((name, bar, True))
            elif not closed:
                changes.append((name, bar, False))
        self._prune(start_s)
        return changes

    # ---- internals ----

    def _add_to_minute(self, start: int, price: float, volume: float) -> None:
        if start < self._last_minute:
            return                                     # a late tick for a minute that is already behind
        held = self._minutes.get(start)
        self._minutes[start] = (Candle(start, price, price, price, price, volume) if held is None else
                                Candle(start, held.o, max(held.h, price), min(held.l, price), price, held.v + volume))
        self._last_minute = start

    def _fold(self, start: int, end: int) -> Candle:
        """The bar `[start, end)` from the 1-minute bars held, up to the newest one."""
        first = max(start, self._last_minute - KEEP_SECONDS)
        parts = [self._minutes[t] for t in range(first, min(end, self._last_minute + 60), 60) if t in self._minutes]
        head, tail = parts[0], parts[-1]
        return Candle(start, head.o, max(p.h for p in parts), min(p.l for p in parts), tail.c, sum(p.v for p in parts))

    def _prune(self, now_s: int) -> None:
        if len(self._minutes) > KEEP_SECONDS // 60 + PRUNE_SLACK:
            for t in [t for t in self._minutes if t < now_s - KEEP_SECONDS]:
                del self._minutes[t]
