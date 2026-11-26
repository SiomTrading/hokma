"""The budget of the READS of an account (P&L of the broker), apart from the orders. Generic: the Schwab door (`schwab/http.py`) uses it and so can
any other broker's door (Alpaca).

An order never waits for a read. A read of an account is of lower priority and yields (raises `ReadYielded`, with no call to the broker) when:
  - the door is paused by a 429 of an ORDER or of the market data (the pause of the whole door),
  - the reads have their own pause (a 429 answered to a read pauses ONLY the reads, never an order),
  - the reads used their own per-minute cap, or
  - the shared count of the door left less than `reserve` calls for the orders.
A read that yields is not a failure of the broker: the reader of the P&L does not count it as a failed read."""
from __future__ import annotations

import threading
from collections.abc import Callable

READS_PER_MINUTE = 20          # the cap of the reads of an account, apart from the cap of the door
ORDER_RESERVE = 30             # calls per minute of the door that no read may take: they are for the orders
PAUSE_SECONDS = 60.0


class ReadYielded(Exception):
    """A read of an account gave way to the orders. No call was made. It is not a failed read."""


class ReadBudget:
    def __init__(self, clock: Callable[[], float], *, per_minute: int = READS_PER_MINUTE, reserve: int = ORDER_RESERVE,
                 window: float = 60.0) -> None:
        self._clock = clock
        self.per_minute = per_minute
        self.reserve = reserve
        self.window = window
        self._lock = threading.Lock()
        self._events: list[float] = []
        self._paused_until = 0.0

    def pause(self, seconds: float = PAUSE_SECONDS) -> None:
        """A 429 answered to a read: from now the reads wait, the orders do not."""
        with self._lock:
            self._paused_until = self._clock() + seconds

    def paused(self) -> bool:
        """True while the reads are resting after a 429 of a read."""
        with self._lock:
            return self._clock() < self._paused_until

    def admit(self, shared_used: int, shared_limit: int, door_paused: bool) -> bool:
        """True and one read taken from the budget, or False (the read yields). `shared_used` is what the door counted in the last minute."""
        now = self._clock()
        with self._lock:
            if door_paused or now < self._paused_until or shared_used >= shared_limit - self.reserve:
                return False
            self._events = [t for t in self._events if now - t < self.window]
            if len(self._events) >= self.per_minute:
                return False
            self._events.append(now)
            return True
