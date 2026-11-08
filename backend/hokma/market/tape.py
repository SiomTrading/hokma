"""Tick tape: time, price and size of every tick of one symbol in compact ring arrays (int64 / float64 / float64).

The oldest ticks leave when the cap is reached. Memory grows with the ticks stored, up to CAPACITY. Ticks are
expected in non-decreasing time order (`iter_since` uses a binary search on that order).
"""
from array import array
from collections.abc import Iterator

CAPACITY = 500_000


class TickTape:
    __slots__ = ("capacity", "_ts", "_px", "_sz", "_start", "_total")

    def __init__(self, capacity: int = CAPACITY):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._ts = array("q")
        self._px = array("d")
        self._sz = array("d")
        self._start = 0  # index of the oldest tick once the ring is full
        self._total = 0

    def append(self, ts_ms: int, price: float, size: float | None = None) -> None:
        """Store one tick. A missing size is stored as 0.0."""
        self._total += 1
        size = 0.0 if size is None else size
        if len(self._ts) < self.capacity:
            self._ts.append(ts_ms)
            self._px.append(price)
            self._sz.append(size)
            return
        i = self._start
        self._ts[i] = ts_ms
        self._px[i] = price
        self._sz[i] = size
        self._start = i + 1 if i + 1 < self.capacity else 0

    def __len__(self) -> int:
        return len(self._ts)

    @property
    def total_appended(self) -> int:
        """Monotonic count of every tick ever appended (not reduced when old ticks leave)."""
        return self._total

    @property
    def first_ts_ms(self) -> int | None:
        return self._ts[self._start] if self._ts else None

    @property
    def last_ts_ms(self) -> int | None:
        return self._ts[self._start - 1] if self._ts else None

    def _at(self, logical: int) -> tuple[int, float, float]:
        i = (self._start + logical) % len(self._ts)
        return self._ts[i], self._px[i], self._sz[i]

    def _iter_from(self, logical: int) -> Iterator[tuple[int, float, float]]:
        n = len(self._ts)
        ts, px, sz = self._ts, self._px, self._sz
        for k in range(logical, n):
            i = self._start + k
            if i >= n:
                i -= n
            yield ts[i], px[i], sz[i]

    def __iter__(self) -> Iterator[tuple[int, float, float]]:
        """Every tick, oldest first, as (ts_ms, price, size)."""
        return self._iter_from(0)

    def iter_since(self, ts_ms: int) -> Iterator[tuple[int, float, float]]:
        """Ticks with time >= ts_ms, oldest first."""
        lo, hi = 0, len(self._ts)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._at(mid)[0] < ts_ms:
                lo = mid + 1
            else:
                hi = mid
        return self._iter_from(lo)
