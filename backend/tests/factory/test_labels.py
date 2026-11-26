"""Labels without leakage (SPEC-020: V-28), values computed by hand."""
import numpy as np
import pytest

from hokma.factory.labels import Label, LeakError, check_features, direction_label, forward_return, split_purged
from hokma.quant.errors import BadInput

OPEN = [100.0, 101.0, 102.0, 104.0, 103.0, 105.0, 108.0, 107.0, 110.0, 111.0]


def test_a_5_bar_label_is_measured_from_the_open_of_t_plus_1():
    label = forward_return(OPEN, 3)
    values = label.target()
    # t = 0 uses the entry open[1] = 101 and the exit open[4] = 103 (not the close of bar 0 and not open[0])
    assert values[0] == pytest.approx(103 / 101 - 1)
    assert values[1] == pytest.approx(105 / 102 - 1)
    assert values[2] == pytest.approx(108 / 104 - 1)
    assert values[5] == pytest.approx(111 / 108 - 1)          # t = 5: entry open[6] = 108, exit open[9] = 111
    assert np.isnan(values[6:]).all() and not np.isnan(values[:6]).any()


def test_the_five_bar_horizon_needs_six_more_bars():
    series = [100.0 + i for i in range(10)]
    label = forward_return(series, 5)
    assert label.horizon == 5
    assert label.target()[0] == pytest.approx(106 / 101 - 1)   # open[6] over open[1]
    assert np.isnan(label.target()[4:]).all() and not np.isnan(label.target()[3])


def test_direction_label_with_a_dead_zone():
    out = direction_label(OPEN, 3, dead_zone=0.02).target()
    # t0: 103/101 - 1 = 0.0198 is inside the dead zone; t1: 0.0294 and t2: 0.0385 are above it
    assert list(out[:3]) == [0.0, 1.0, 1.0]
    assert np.isnan(out[6])


def test_a_label_cannot_be_an_input():
    label = forward_return(OPEN, 3)
    with pytest.raises(LeakError) as caught:
        check_features({"momentum": np.arange(10), "y": label})
    assert caught.value.code == "label_leak"
    with pytest.raises(LeakError):
        check_features({"y": np.arange(10)}, labels={"y": label})
    with pytest.raises(LeakError):
        np.asarray(label)
    with pytest.raises(LeakError):
        np.column_stack([np.arange(10), label])
    check_features({"momentum": np.arange(10)}, labels={"y": label})            # a real feature passes


def test_purge_and_embargo_by_hand():
    label = forward_return([100.0 + i for i in range(30)], 2)                    # finite for t = 0 to 26
    train, test = split_purged(label, 10, 14, embargo=1)
    # a row t spans bars t to t+3. The test rows span 10 to 13+3 = 16 and the embargo adds bar 17.
    assert list(test) == [10, 11, 12, 13]
    assert list(train) == [*range(0, 7), *range(18, 27)]                           # 7 to 17 touch the test span or the embargo
    assert not set(train) & set(test)


def test_no_train_label_window_overlaps_the_test_window():
    label = forward_return([100.0 + (i % 7) for i in range(60)], 4)
    for start in (5, 20, 40):
        train, test = split_purged(label, start, start + 5, embargo=2)
        h = label.horizon
        for t in train:
            assert t + 1 + h < start or t > start + 4 + 1 + h + 2


def test_bad_arguments():
    with pytest.raises(BadInput):
        forward_return(OPEN, 0)
    with pytest.raises(BadInput):
        forward_return(OPEN, 2_000)                                              # above the cap of quant limits
    with pytest.raises(BadInput):
        split_purged(forward_return(OPEN, 2), 5, 3)
    with pytest.raises(BadInput):
        split_purged(forward_return(OPEN, 2), 2, 4, embargo=-1)
    assert isinstance(forward_return(OPEN, 2), Label)
