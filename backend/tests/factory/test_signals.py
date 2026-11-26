"""Signal utilities (SPEC-020: B-36, AH-38) with series of 10 points written by hand."""
import numpy as np
import pandas as pd
import pytest

from hokma.factory.signals import EdgeTrigger, barssince, cross, crossover, crossunder, edge_trigger, quantile
from hokma.quant.errors import BadInput

A = [1, 2, 3, 4, 3, 2, 1, 2, 3, 4]
LEVEL = [2] * 10


def idx(flags):
    return list(np.flatnonzero(flags))


def test_crossover_is_on_the_exact_bar_and_not_the_next():
    # a is at 2 on bar 1 (not above), 3 on bar 2: the cross is bar 2. Same on the way up at bars 7 and 8: bar 7 touches 2, bar 8 is above.
    assert idx(crossover(A, LEVEL)) == [2, 8]
    assert idx(crossunder(A, LEVEL)) == [6]                # 2 on bar 5 (not below), 1 on bar 6
    assert idx(cross(A, LEVEL)) == [2, 6, 8]


def test_two_series_and_a_pandas_input_and_nan():
    fast = pd.Series([1, 1, 3, 3, 1, 1, 1, 1, 1, 1], dtype=float)
    slow = pd.Series([2.0] * 10)
    assert idx(crossover(fast, slow)) == [2] and idx(crossunder(fast, slow)) == [4]
    nan = [np.nan, 1, 3, 1, 3, 1, 3, 1, 3, 1]
    assert idx(crossover(nan, LEVEL)) == [2, 4, 6, 8]       # a NaN bar never crosses
    assert not crossover([np.nan] + [3.0] * 9, LEVEL).any()


def test_barssince():
    flags = [False, False, True, False, False, True, False, False, False, False]
    assert list(barssince(flags)) == [-1, -1, 0, 1, 2, 0, 1, 2, 3, 4]
    assert list(barssince([False, False])) == [-1, -1]


def test_quantile_by_hand():
    data = list(range(1, 11))                              # 1..10
    assert quantile(data, 0.5) == 5.5
    assert quantile(data, 0.25) == 3.25                    # rank 0.25 * 9 = 2.25: 3 + 0.25
    assert quantile(data, 0) == 1 and quantile(data, 1) == 10
    assert quantile([1, np.nan, 3, np.inf], 0.5) == 2.0
    with pytest.raises(BadInput):
        quantile(data, 1.5)
    with pytest.raises(BadInput):
        quantile([np.nan], 0.5)


def test_bad_inputs():
    with pytest.raises(BadInput, match="same length"):
        crossover([1, 2, 3], [1, 2])
    with pytest.raises(BadInput):
        crossover(5, 6)
    with pytest.raises(BadInput):
        crossover(["a", "b"], 1)


def test_edge_trigger_opens_once_per_episode():                                          # AH-38
    wish = [0, 1, 1, 1, 1, 1, 0, 0, 1, 1]                  # five bars of signal, flat, then a new episode
    assert list(edge_trigger(wish)) == [0, 1, 0, 0, 0, 0, 0, 0, 1, 0]


def test_edge_trigger_flip_waits_for_flat_by_default():
    wish = [1, 1, -1, -1, 0, -1]
    assert list(edge_trigger(wish)) == [1, 0, 0, 0, 0, -1]
    assert list(edge_trigger(wish, on_flip="reverse")) == [1, 0, -1, 0, 0, -1]
    trigger = EdgeTrigger()
    assert [trigger.update(w) for w in (1, 1, 0, 1)] == [1, 0, 0, 1]
    with pytest.raises(BadInput):
        trigger.update(2)
    with pytest.raises(BadInput):
        edge_trigger([0.5, 1])
