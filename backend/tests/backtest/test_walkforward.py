"""Walk forward and refit (SPEC-020: V-24, B-37, and the use of the splitter of H1, V-23). Every expected value is worked out by hand."""
import numpy as np
import pytest

from hokma.backtest import walkforward
from hokma.backtest.walkforward import LeakageError, WalkForwardError, refit, run, windows
from hokma.quant.errors import BadInput
from hokma.quant.validation import cv

POINTS = [{"p": "A"}, {"p": "B"}, {"p": "C"}]


def matrix():
    """100 bars, three combinations. A earns 2 a bar on bars 0 to 39 and loses 1 after; B loses 1 on bars 0 to 19, earns 1 on bars 20 to 59 and 0
    after; C earns 0.5 a bar all along."""
    a = np.array([2.0] * 40 + [-1.0] * 60)
    b = np.array([-1.0] * 20 + [1.0] * 40 + [0.0] * 40)
    c = np.full(100, 0.5)
    return np.column_stack([a, b, c])


# ---------------------------------------------------------------------------------------------------------------- V-24
def test_four_windows_each_choose_with_their_own_training_and_the_curve_is_the_four_tests_end_to_end_V_24():
    result = run(POINTS, matrix(), scheme="rolling", n_folds=4, purge=0, embargo=0)
    assert [w.train for w in result.windows] == [(0, 20), (20, 40), (40, 60), (60, 80)] and [w.test_blocks for w in result.windows] == [((20, 40),), ((40, 60),), ((60, 80),), ((80, 100),)]
    # train sums: window 0 (bars 0-19): A 40, B -20, C 10 -> A; window 1 (20-39): A 40, B 20, C 10 -> A; window 2 (40-59): A -20, B 20, C 10 -> B;
    # window 3 (60-79): A -20, B 0, C 10 -> C
    assert [w.chosen["p"] for w in result.windows] == ["A", "A", "B", "C"] and [w.in_sample for w in result.windows] == [40.0, 40.0, 20.0, 10.0]
    # on the tests: A on 20-39 earns 40, A on 40-59 loses 20, B on 60-79 earns 0, C on 80-99 earns 10
    assert [w.out_of_sample for w in result.windows] == [40.0, -20.0, 0.0, 10.0]
    assert result.oos_rows == tuple(range(20, 100)) and len(result.oos_pnl) == 80                       # the four tests, one after the other
    assert result.oos_pnl == tuple([2.0] * 20 + [-1.0] * 20 + [0.0] * 20 + [0.5] * 20) and result.oos_total == 30.0
    # efficiency: out of sample 30 / 80 rows = 0.375 a row; in sample (40 + 40 + 20 + 10) / 4 windows of 20 rows: (2 + 2 + 1 + 0.5) / 4 = 1.375 a row
    assert result.efficiency == pytest.approx(0.375 / 1.375) and result.efficiency == pytest.approx(3 / 11) and result.leakage_free and result.warnings == ()


def test_the_choice_of_a_window_never_reads_its_test_rows_V_24():
    base = run(POINTS, matrix(), scheme="rolling", n_folds=4, purge=0, embargo=0)
    changed = matrix()
    for a, b in ((20, 40), (40, 60), (60, 80), (80, 100)):
        changed[a:b] = np.random.default_rng(a).normal(0, 1000, size=(b - a, 3))                    # wreck the test rows of every window
    again = run(POINTS, changed, scheme="rolling", n_folds=4, purge=0, embargo=0)
    # the train rows of window k are the test rows of window k - 1, so only window 0 keeps its training: it must choose the same way
    assert again.windows[0].chosen == base.windows[0].chosen and again.windows[0].in_sample == base.windows[0].in_sample
    shifted = matrix()
    shifted[20:] += 5.0                                                                                 # a change that only touches rows after window 0's training
    assert run(POINTS, shifted, scheme="rolling", n_folds=4, purge=0, embargo=0).windows[0].chosen == base.windows[0].chosen


def test_an_anchored_window_trains_from_the_first_row_V_24():
    result = run(POINTS, matrix(), scheme="anchored", n_folds=4, purge=0, embargo=0)
    assert [w.train for w in result.windows] == [(0, 20), (0, 40), (0, 60), (0, 80)]
    # train sums over bars 0..: window 1 (0-39): A 80, B 0, C 20 -> A; window 2 (0-59): A 60, B 20, C 30 -> A; window 3 (0-79): A 40, B 20, C 40 -> A and C tie: A (the first)
    assert [w.chosen["p"] for w in result.windows] == ["A", "A", "A", "A"]
    assert [w.out_of_sample for w in result.windows] == [40.0, -20.0, -20.0, -20.0] and result.oos_total == -20.0


def test_cpcv_gives_every_combination_of_test_groups_and_no_single_curve_V_24():
    data = np.column_stack([np.where(np.arange(100) < 50, 1.0, -1.0), np.where(np.arange(100) < 50, -1.0, 1.0)])
    result = run(POINTS[:2], data, scheme="cpcv", n_folds=5, purge=1, embargo=1, n_test_folds=2)
    assert len(result.windows) == 10 and result.oos_rows is None and result.oos_pnl is None and result.oos_total is None   # C(5, 2) = 10 test sets
    for w in result.windows:
        assert w.train_rows < 100 and len(w.test_blocks) >= 1 and w.chosen["p"] in ("A", "B")
    # the groups are 20 rows: the test set {0, 1} (rows 0-39) leaves train rows 40.. (less the purge and the embargo) where B wins: B is chosen there and loses in the test
    first = result.windows[0]
    assert first.test_blocks == ((0, 40),) and first.chosen["p"] == "B" and first.out_of_sample == -40.0
    assert result.leakage_free and run(POINTS[:2], data, scheme="cpcv", n_folds=5, purge=1, embargo=1, n_test_folds=2) == result


def test_the_run_is_deterministic():
    one, two = run(POINTS, matrix(), scheme="rolling", n_folds=4, purge=0, embargo=0), run(POINTS, matrix(), scheme="rolling", n_folds=4, purge=0, embargo=0)
    assert one == two and one.to_dict() == two.to_dict() and one.to_dict()["windows"][2]["chosen"] == {"p": "B"}


def test_bad_shapes_schemes_and_sizes_are_refused():
    with pytest.raises(WalkForwardError, match="matrix of bars by combinations"):
        run(POINTS, matrix()[:, :2], scheme="rolling", n_folds=4, purge=0, embargo=0)
    with pytest.raises(WalkForwardError, match="not finite"):
        bad = matrix()
        bad[3, 1] = np.nan
        run(POINTS, bad, scheme="rolling", n_folds=4, purge=0, embargo=0)
    with pytest.raises(WalkForwardError, match="scheme"):
        run(POINTS, matrix(), scheme="random", n_folds=4, purge=0, embargo=0)
    with pytest.raises(WalkForwardError, match="objective"):
        run(POINTS, matrix(), scheme="rolling", n_folds=4, purge=0, embargo=0, objective="sharpe")
    with pytest.raises(BadInput):
        run(POINTS, matrix(), scheme="rolling", n_folds=1_001, purge=0, embargo=0)                 # above the cap of folds
    with pytest.raises(BadInput):
        run(POINTS, matrix(), scheme="rolling", n_folds=4, purge=15, embargo=10)                   # purge plus embargo must be smaller than a window
    with pytest.raises(BadInput):
        run(POINTS, matrix(), scheme="cpcv", n_folds=40, purge=0, embargo=0, n_test_folds=20)     # too many test sets
    losing = np.column_stack([np.full(100, -1.0)] * 3)
    assert run(POINTS, losing, scheme="rolling", n_folds=4, purge=0, embargo=0).efficiency is None
    assert run(POINTS, losing, scheme="rolling", n_folds=4, purge=0, embargo=0).warnings == ("in_sample_not_positive",)


# ---------------------------------------------------------------------------------------------------------------- V-23 (H1) used here
def test_the_windows_are_the_splits_of_the_quant_module_and_no_test_row_is_within_the_purge_of_a_train_row_V_23():
    for scheme, h1 in (("rolling", "walk_forward"), ("anchored", "expanding")):
        mine = windows(100, scheme=scheme, n_folds=3, purge=5, embargo=2)
        theirs = cv.split(100, scheme=h1, n_folds=3, purge=5, embargo=2)
        assert len(mine) == len(theirs) >= 2                                   # the gap of 7 rows leaves room for two windows of 25 rows after the first
        for a, b in zip(mine, theirs):
            assert a.train.tolist() == b.train.tolist() and a.test.tolist() == b.test.tolist()
            assert a.test.min() - a.train.max() >= 5 + 2                                          # the gap is the purge plus the embargo: at least 5 bars
    for fold in windows(100, scheme="cpcv", n_folds=5, purge=5, embargo=2, n_test_folds=2):
        train = set(fold.train.tolist())
        for start, stop in fold.test_blocks:
            assert not any(t in train for t in range(max(0, start - 5), stop + 2))                   # nothing within 5 bars before or 2 after a test block
    assert "def split" not in open(walkforward.__file__, encoding="utf-8").read()                    # there is no second splitter in this file


def test_a_partition_with_a_leak_is_refused_before_it_is_used(monkeypatch):
    real = cv.split

    def leaky(*args, **kwargs):
        folds = real(*args, **kwargs)
        folds[0] = cv.Fold(train=np.append(folds[0].train, folds[0].test[0]), test=folds[0].test, test_blocks=folds[0].test_blocks)
        return folds
    monkeypatch.setattr(walkforward.cv, "split", leaky)
    with pytest.raises(LeakageError) as err:
        windows(100, scheme="rolling", n_folds=4, purge=0, embargo=0)
    assert err.value.code == "leakage" and "both in train and in test" in str(err.value)


# ---------------------------------------------------------------------------------------------------------------- B-37
def series():
    return {"y": np.arange(100.0), "date": np.arange(100)}


def test_in_each_window_the_model_is_trained_only_with_the_train_rows_and_scored_in_the_window_after_B_37():
    trained = []

    def fit(view):
        trained.append(view.positions.tolist())
        return float(view.get("y").mean())                                    # the model: the mean of the train rows

    def score(model, view):
        return float(view.get("y").mean() - model)                            # how far the test rows are above the model

    result = refit(series(), fit=fit, score=score, scheme="rolling", n_folds=4, purge=0, embargo=0)
    # train means 9.5, 29.5, 49.5, 69.5 against test means 29.5, 49.5, 69.5, 89.5: every window scores 20
    assert [w.score for w in result.windows] == [20.0] * 4 and result.mean_score == 20.0 and result.leakage_free
    assert [(p[0], p[-1]) for p in trained] == [(0, 19), (20, 39), (40, 59), (60, 79)]
    assert all(max(p) < test[0][0] for p, test in zip(trained, [w.test_blocks for w in result.windows]))     # each train ends before its test starts
    anchored = refit(series(), fit=fit, score=score, scheme="anchored", n_folds=4, purge=0, embargo=0)
    # train means 9.5, 19.5, 29.5, 39.5 against the same test means: 20, 30, 40, 50
    assert [w.score for w in anchored.windows] == [20.0, 30.0, 40.0, 50.0] and anchored.mean_score == 35.0


def test_a_fit_that_touches_a_test_row_fails_the_run_B_37():
    def greedy(view):
        view.row(int(view.positions.max()) + 1)                                # the first row after the training: it is the test window (or its gap)
        return 0.0
    with pytest.raises(LeakageError) as err:
        refit(series(), fit=greedy, score=lambda m, v: 0.0, scheme="rolling", n_folds=4, purge=0, embargo=0)
    assert "row 20 is not a train row" in str(err.value) and err.value.code == "leakage"

    def honest(view):
        return view.row(int(view.positions[0]))["y"]                           # its own rows are fine
    assert refit(series(), fit=honest, score=lambda m, v: m, scheme="rolling", n_folds=4, purge=0, embargo=0).windows[1].score == 20.0
    with pytest.raises(LeakageError):
        refit(series(), fit=lambda view: 0.0, score=lambda m, v: v.row(0)["y"], scheme="rolling", n_folds=4, purge=0, embargo=0)   # a test step reading row 0


def test_the_views_are_read_only_copies_and_the_series_are_checked():
    seen = {}

    def fit(view):
        y = view.get("y")
        seen["writeable"] = y.flags.writeable
        with pytest.raises(ValueError):
            y[0] = 1.0
        with pytest.raises(WalkForwardError, match="no series named"):
            view.get("z")
        with pytest.raises(ValueError):
            view.positions[0] = 3
        return 0.0
    data = series()
    refit(data, fit=fit, score=lambda m, v: 0.0, n_folds=4, purge=0, embargo=0)
    assert seen["writeable"] is False and data["y"][0] == 0.0 and data["y"].flags.writeable                   # the original is untouched
    with pytest.raises(WalkForwardError, match="same number of rows"):
        refit({"y": [1, 2, 3], "x": [1, 2]}, fit=lambda v: 0, score=lambda m, v: 0, n_folds=2, purge=0, embargo=0)
    with pytest.raises(WalkForwardError):
        refit({}, fit=lambda v: 0, score=lambda m, v: 0, n_folds=2, purge=0, embargo=0)
