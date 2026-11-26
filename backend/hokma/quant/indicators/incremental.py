"""Incremental indicators for the fast code path of the DT line (SPEC-020: N-34, two speeds).

Each class keeps the state of one indicator and takes one bar at a time in constant time, so a bar by bar loop does not recompute the
whole window. They are not registered functions: a registry call is stateless and runs in a worker. The registered indicators of this
package (sma, ema, rsi, atr, bollinger) are the batch speed and the reference: after every bar the incremental value equals the batch
value (the tests compare them to 1e-9), with the same seeds (the simple average of the first window values) and the same Wilder smoothing.

update() returns None until the indicator has enough bars. A value that is not finite raises ValueError (the batch functions refuse
the same bars), and the state is left as it was.
"""
from __future__ import annotations

import math
from collections import deque
from typing import NamedTuple

from hokma.quant import limits


def _window(window: int) -> int:
    n = limits.as_int("window", window)
    limits.check_param("window", n)
    if n < 2:
        raise ValueError("bad_input: window must be at least 2")
    return n


def _finite(label: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"bad_input: {label} must be a finite number")
    return number


class IncrementalSMA:
    """Simple moving average of the last window values."""

    def __init__(self, window: int) -> None:
        self.window = _window(window)
        self._values: deque[float] = deque()
        self._sum = 0.0
        self.value: float | None = None

    def update(self, price: float) -> float | None:
        x = _finite("price", price)
        self._values.append(x)
        self._sum += x
        if len(self._values) > self.window:
            self._sum -= self._values.popleft()
        self.value = self._sum / self.window if len(self._values) == self.window else None
        return self.value


class IncrementalEMA:
    """Exponential moving average with constant 2 / (window + 1), seeded with the simple average of the first window values."""

    def __init__(self, window: int) -> None:
        self.window = _window(window)
        self._alpha = 2.0 / (self.window + 1.0)
        self._seed = 0.0
        self._n = 0
        self.value: float | None = None

    def update(self, price: float) -> float | None:
        x = _finite("price", price)
        if self._n < self.window:
            self._n += 1
            self._seed += x
            if self._n == self.window:
                self.value = self._seed / self.window
            return self.value
        assert self.value is not None
        self.value += self._alpha * (x - self.value)
        return self.value


class _Wilder:
    """Wilder smoothing: the simple average of the first window values, then average += (x - average) / window."""

    def __init__(self, window: int) -> None:
        self.window = _window(window)
        self._seed = 0.0
        self._n = 0
        self.value: float | None = None

    def add(self, x: float) -> float | None:
        if self._n < self.window:
            self._n += 1
            self._seed += x
            if self._n == self.window:
                self.value = self._seed / self.window
            return self.value
        assert self.value is not None
        self.value += (x - self.value) / self.window
        return self.value


class IncrementalRSI:
    """Relative strength index (0 to 100) with Wilder smoothing. The first value comes after window changes, so on bar window + 1."""

    def __init__(self, window: int) -> None:
        self.window = _window(window)
        self._gain, self._loss = _Wilder(window), _Wilder(window)
        self._previous: float | None = None
        self.value: float | None = None

    def update(self, price: float) -> float | None:
        x = _finite("price", price)
        previous, self._previous = self._previous, x
        if previous is None:
            return None
        change = x - previous
        gain, loss = self._gain.add(max(change, 0.0)), self._loss.add(max(-change, 0.0))
        if gain is None or loss is None or (gain == 0.0 and loss == 0.0):
            self.value = None
        elif loss == 0.0:
            self.value = 100.0
        else:
            self.value = 100.0 - 100.0 / (1.0 + gain / loss)
        return self.value


class IncrementalATR:
    """Average true range with Wilder smoothing. The first bar has no previous close, so its true range is high minus low."""

    def __init__(self, window: int) -> None:
        self.window = _window(window)
        self._wilder = _Wilder(window)
        self._previous_close: float | None = None
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        h, lo, c = _finite("high", high), _finite("low", low), _finite("close", close)
        if h < lo:
            raise ValueError("bad_input: high is below low")
        true_range = h - lo
        if self._previous_close is not None:
            true_range = max(true_range, abs(h - self._previous_close), abs(lo - self._previous_close))
        self._previous_close = c
        self.value = self._wilder.add(true_range)
        return self.value


class Bands(NamedTuple):
    upper: float
    middle: float
    lower: float


class IncrementalBollinger:
    """Bollinger bands: simple average of the last window closes plus and minus num_std_dev population standard deviations.

    The deviation comes from running sums of the values shifted by a value of the window, which keeps the subtraction of two large sums
    small; the sums are rebuilt from the window every window bars so that rounding never builds up."""

    def __init__(self, window: int, num_std_dev: float = 2.0) -> None:
        self.window = _window(window)
        if not math.isfinite(num_std_dev) or num_std_dev <= 0:
            raise ValueError("bad_input: num_std_dev must be a positive number")
        self.num_std_dev = float(num_std_dev)
        self._values: deque[float] = deque()
        self._shift = 0.0
        self._sum = 0.0
        self._sum_sq = 0.0
        self._since = 0
        self.value: Bands | None = None

    def _rebuild(self) -> None:
        self._shift = self._values[0]
        shifted = [v - self._shift for v in self._values]
        self._sum, self._sum_sq, self._since = sum(shifted), sum(d * d for d in shifted), 0

    def update(self, price: float) -> Bands | None:
        x = _finite("price", price)
        if not self._values:
            self._shift = x
        self._values.append(x)
        d = x - self._shift
        self._sum += d
        self._sum_sq += d * d
        if len(self._values) > self.window:
            old = self._values.popleft() - self._shift
            self._sum -= old
            self._sum_sq -= old * old
        self._since += 1
        if self._since >= self.window and len(self._values) == self.window:
            self._rebuild()
        if len(self._values) < self.window:
            self.value = None
            return None
        mean_shifted = self._sum / self.window
        variance = max(self._sum_sq / self.window - mean_shifted * mean_shifted, 0.0)
        middle = self._shift + mean_shifted
        spread = self.num_std_dev * math.sqrt(variance)
        self.value = Bands(middle + spread, middle, middle - spread)
        return self.value
