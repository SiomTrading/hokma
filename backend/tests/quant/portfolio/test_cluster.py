"""kmeans of the portfolio module: known values, determinism and its limits (SPEC-032)."""
from __future__ import annotations

import numpy as np
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.portfolio.cluster import kmeans

TWO = np.array([[0, 0], [0, 1], [1, 0], [10, 10], [10, 11], [11, 10]], dtype=float)


def test_two_clear_groups_are_found_with_their_exact_centres():
    labels, centers = kmeans(TWO, 2, 0)
    assert len(set(labels[:3])) == 1 and len(set(labels[3:])) == 1 and labels[0] != labels[3]
    by_label = {int(labels[0]): centers[labels[0]], int(labels[3]): centers[labels[3]]}
    assert np.allclose(by_label[int(labels[0])], [1 / 3, 1 / 3]) and np.allclose(by_label[int(labels[3])], [31 / 3, 31 / 3])


def test_one_group_is_the_mean_and_k_equal_n_gives_each_point_its_own_group():
    labels, centers = kmeans(TWO, 1, 5)
    assert set(labels) == {0} and np.allclose(centers[0], TWO.mean(axis=0))
    labels, centers = kmeans(TWO, len(TWO), 5)
    assert sorted(labels) == list(range(len(TWO))) and np.allclose(centers[labels], TWO)


def test_the_same_points_k_and_seed_give_the_same_answer_and_the_input_is_not_changed():
    pts = np.random.default_rng(3).normal(size=(60, 3))
    before = pts.copy()
    a, b = kmeans(pts, 4, 7), kmeans(pts, 4, 7)
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1]) and np.array_equal(pts, before)
    assert a[1].shape == (4, 3) and set(a[0]) == {0, 1, 2, 3}


def test_identical_points_and_a_line_do_not_leave_an_empty_group():
    labels, centers = kmeans(np.zeros((5, 2)), 3, 0)
    assert labels.shape == (5,) and np.allclose(centers, 0)
    labels, _ = kmeans(np.arange(8.0), 2, 1)
    assert set(labels) == {0, 1} and labels[0] != labels[-1]


@pytest.mark.parametrize("points, k", [([], 1), ([[1.0, float("nan")]], 1), (TWO, 0), (TWO, 7), (TWO, 1.5), (np.zeros((2, 2, 2)), 1)])
def test_bad_input_is_refused(points, k):
    with pytest.raises(BadInput):
        kmeans(points, k, 0)
