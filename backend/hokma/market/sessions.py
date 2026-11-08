"""Where a trading session starts and where each bar begins and ends. One rule for the history and for the live feed.

The anchor is the start of the session of the instrument:
- futures: 18:00 New York the evening before the trade date;
- forex: 17:00 New York the evening before;
- crypto: 00:00 UTC;
- stocks, ETFs, indices and anything else: the regular open in the exchange time zone, 09:30.

A session is named by a label date: the trade date for the evening sessions (the futures session that opens Sunday
18:00 is Monday's), the calendar date of the open for the rest.

Bars shorter than or equal to 24 hours (hours, minutes, seconds) count from the anchor of their session and never
cross the next anchor, so the last bar of a session is cut short when the length does not divide it. Bars longer than
24 hours count from the anchor of the first session of their week. `D` groups N sessions and `W` groups N weeks, both
counted from a fixed origin (Monday 5 January 1970), so the same instant always falls in the same bar. Stocks, futures
and forex have sessions from Monday to Friday (the weekend is part of the Friday bar); crypto has one every day. Market
holidays are not known here: a holiday is a session without bars.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo

from hokma.market.intervals import TimeInterval
from hokma.market.tz import NEW_YORK

EPOCH_MONDAY = date(1970, 1, 5)
DAY = 86400


@dataclass(frozen=True)
class Session:
    tz: tzinfo               # zone of the anchor wall clock
    hour: int
    minute: int
    evening: bool            # the session starts the evening before its label date
    weekdays: bool           # no sessions on Saturday and Sunday

    # ---- sessions ----

    def label(self, ts: int) -> date:
        """The session that holds the instant `ts` (epoch seconds)."""
        local = datetime.fromtimestamp(ts, self.tz)
        day = (local - timedelta(hours=self.hour, minutes=self.minute)).date()
        return day + timedelta(days=1) if self.evening else day

    def start(self, label: date) -> int:
        """Epoch second at which the session `label` opens."""
        day = label - timedelta(days=1) if self.evening else label
        return int(datetime(day.year, day.month, day.day, self.hour, self.minute, tzinfo=self.tz).timestamp())

    def _index(self, label: date) -> int:
        days = (label - EPOCH_MONDAY).days
        return days // 7 * 5 + min(days % 7, 4) if self.weekdays else days

    def _label_at(self, index: int) -> date:
        days = index // 5 * 7 + index % 5 if self.weekdays else index
        return EPOCH_MONDAY + timedelta(days=days)

    # ---- bars ----

    def bounds(self, ts: int, interval: TimeInterval) -> tuple[int, int]:
        """`[start, end)` of the bar of `interval` that holds the instant `ts`."""
        label = self.label(ts)
        if interval.unit == "D":
            first = self._index(label) // interval.n * interval.n
            return self.start(self._label_at(first)), self.start(self._label_at(first + interval.n))
        if interval.unit == "W":
            week = (label - EPOCH_MONDAY).days // 7 // interval.n * interval.n
            return (self.start(EPOCH_MONDAY + timedelta(days=7 * week)),
                    self.start(EPOCH_MONDAY + timedelta(days=7 * (week + interval.n))))
        if interval.seconds <= DAY:
            base, cap = self.start(label), self.start(label + timedelta(days=1))
        else:
            monday = label - timedelta(days=label.weekday())
            base, cap = self.start(monday), self.start(monday + timedelta(days=7))
        start = base + max(0, (ts - base) // interval.seconds) * interval.seconds
        return start, min(start + interval.seconds, cap)


def session_for(kind: str, tz: tzinfo) -> Session:
    """The session of an instrument kind (`Series.kind`: equity, etf, index, future, crypto, currency, other)."""
    if kind == "future":
        return Session(NEW_YORK, 18, 0, True, True)
    if kind in ("currency", "forex"):
        return Session(NEW_YORK, 17, 0, True, True)
    if kind == "crypto":
        return Session(timezone.utc, 0, 0, False, False)
    return Session(tz, 9, 30, False, True)
