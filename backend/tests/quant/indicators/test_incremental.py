"""N-34: the incremental indicators of the fast code path equal the batch indicators bar by bar (1e-9), and their small hand values."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.indicators import incremental, momentum, overlap, volatility

TOL = 1e-9


def assert_stream_equals_batch(stream, batch_values) -> None:
    """Same warm-up (None where the batch has no value) and the same number elsewhere, bar by bar."""
    assert len(stream) == len(batch_values)
    for n, (got, want) in enumerate(zip(stream, batch_values)):
        if want is None:
            assert got is None, n
        else:
            assert got is not None and abs(got - want) <= TOL, (n, got, want)


@pytest.mark.parametrize("window", [2, 5, 14, 50])
def test_averages_rsi_and_atr_equal_the_batch_values_on_1000_bars(spy, window):
    batch = lambda res, key: res.value["values"][key]  # noqa: E731
    sma, ema, rsi, atr = (incremental.IncrementalSMA(window), incremental.IncrementalEMA(window), incremental.IncrementalRSI(window),
                          incremental.IncrementalATR(window))
    assert_stream_equals_batch([sma.update(x) for x in spy["close"]], batch(overlap.sma(spy, window=window, last_n=None), "sma"))
    assert_stream_equals_batch([ema.update(x) for x in spy["close"]], batch(overlap.ema(spy, window=window, last_n=None), "ema"))
    assert_stream_equals_batch([rsi.update(x) for x in spy["close"]], batch(momentum.rsi(spy, window=window, last_n=None), "rsi"))
    assert_stream_equals_batch([atr.update(h, lo, c) for h, lo, c in zip(spy["high"], spy["low"], spy["close"])], batch(volatility.atr(spy, window=window, last_n=None), "atr"))


@pytest.mark.parametrize("window,k", [(5, 2.0), (20, 2.0), (20, 1.5), (50, 3.0)])
def test_bollinger_bands_equal_the_batch_bands_on_1000_bars(spy, window, k):
    bands = incremental.IncrementalBollinger(window, k)
    stream = [bands.update(x) for x in spy["close"]]
    batch = volatility.bollinger(spy, window=window, num_std_dev=k, last_n=None).value["values"]
    for key in ("upper", "middle", "lower"):
        assert_stream_equals_batch([None if b is None else getattr(b, key) for b in stream], batch[key])


def test_small_hand_values():
    sma = incremental.IncrementalSMA(3)
    assert [sma.update(x) for x in (1, 2, 3, 4)] == [None, None, 2.0, 3.0]
    ema = incremental.IncrementalEMA(3)                                           # seed (1+2+3)/3 = 2, then 2 + 0.5 * (4 - 2) = 3
    assert [ema.update(x) for x in (1, 2, 3, 4)] == [None, None, 2.0, 3.0]
    rsi = incremental.IncrementalRSI(2)                                           # changes +1, -1: average gain 0.5, average loss 0.5, so 50
    assert [rsi.update(x) for x in (10, 11, 10)] == [None, None, 50.0]
    assert rsi.update(12) == pytest.approx(100 - 100 / 6)                         # change +2: gain 0.5 + (2 - 0.5) / 2 = 1.25, loss 0.5 - 0.5 / 2 = 0.25, rs 5
    atr = incremental.IncrementalATR(2)                                           # true ranges 2 (no previous close), 2, 2, then 3
    assert [atr.update(h, lo, c) for h, lo, c in ((11, 9, 10), (12, 10, 11), (13, 11, 12.5))] == [None, 2.0, 2.0]
    assert atr.update(15, 12, 14) == 2.5                                          # max(3, |15 - 12.5|, |12 - 12.5|) = 3: 2 + (3 - 2) / 2
    bands = incremental.IncrementalBollinger(3, 2.0)
    assert [bands.update(x) for x in (1, 2)] == [None, None]
    b = bands.update(3)
    assert b.middle == 2.0 and b.upper == pytest.approx(2 + 2 * math.sqrt(2 / 3)) and b.lower == pytest.approx(2 - 2 * math.sqrt(2 / 3))


def test_rsi_with_no_losses_is_100_and_with_no_change_has_no_value():
    up = incremental.IncrementalRSI(2)
    assert [up.update(x) for x in (1, 2, 3, 4)] == [None, None, 100.0, 100.0]
    flat = incremental.IncrementalRSI(2)
    assert [flat.update(5) for _ in range(4)] == [None, None, None, None]         # 0 over 0, as in the batch function
    batch = momentum.rsi(pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}, index=pd.bdate_range("2024-01-02", periods=4)), window=2, last_n=4)
    assert batch.value["values"]["rsi"][2:] == [100.0, 100.0]


def test_bad_values_and_windows_are_refused_and_leave_the_state_alone():
    sma = incremental.IncrementalSMA(2)
    sma.update(1.0)
    with pytest.raises(ValueError, match="bad_input"):
        sma.update(float("nan"))
    assert sma.update(3.0) == 2.0                                                 # the refused value was never added
    with pytest.raises(ValueError, match="bad_input"):
        incremental.IncrementalATR(2).update(1.0, 2.0, 1.5)                       # high below low
    with pytest.raises(ValueError):
        incremental.IncrementalSMA(1)
    with pytest.raises(ValueError):
        incremental.IncrementalBollinger(5, 0.0)
    with pytest.raises(BadInput):
        incremental.IncrementalSMA(2.5)
    with pytest.raises(BadInput):
        incremental.IncrementalEMA(1_000_000)                                      # above the window cap of the quant limits


def test_the_state_is_constant_in_the_window_not_in_the_bars_seen():
    bands = incremental.IncrementalBollinger(20)
    for x in range(10_000):
        bands.update(100.0 + (x % 7))
    assert len(bands._values) == 20                                               # the deque keeps the window only
