"""Multiple testing corrections: ECT-036 (Bonferroni, Holm, BH by hand) and ECT-055 (4,850 random pairs through BH)."""
import numpy as np
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.validation import multiple_testing as mt

from .conftest import assert_registered

P = [0.01, 0.04, 0.03, 0.005]      # m = 4 tests, written by hand below


def test_bonferroni_by_hand_ECT_036():
    """Each p times 4: 0.04, 0.16, 0.12, 0.02. At 0.05 the first and the last survive."""
    result = mt.bonferroni(P, 0.05)
    assert result.adjusted == pytest.approx((0.04, 0.16, 0.12, 0.02))
    assert result.reject == (True, False, False, True) and result.n_rejected == 2


def test_holm_by_hand_ECT_036():
    """Sorted 0.005, 0.01, 0.03, 0.04 times 4, 3, 2, 1 gives 0.02, 0.03, 0.06, 0.04; the running maximum makes the last 0.06."""
    result = mt.holm(P, 0.05)
    assert result.adjusted == pytest.approx((0.03, 0.06, 0.06, 0.02))
    assert result.reject == (True, False, False, True)


def test_benjamini_hochberg_by_hand_ECT_036():
    """Sorted 0.005, 0.01, 0.03, 0.04 times 4/1, 4/2, 4/3, 4/4 gives 0.02, 0.02, 0.04, 0.04; the running minimum keeps them."""
    result = mt.benjamini_hochberg(P, 0.05)
    assert result.adjusted == pytest.approx((0.02, 0.04, 0.04, 0.02))
    assert result.reject == (True, True, True, True)


def test_the_order_of_strength_is_bonferroni_then_holm_then_bh():
    p = [0.001, 0.008, 0.012, 0.2, 0.5, 0.0004, 0.03]
    counts = [mt.correct(p, method, 0.05).n_rejected for method in mt.METHODS]
    assert counts[0] <= counts[1] <= counts[2]
    # m = 7. Sorted 0.0004, 0.001, 0.008, 0.012, 0.03, 0.2, 0.5. Bonferroni (x7): 0.0028, 0.007, 0.056 -> 2 survive.
    # Holm (x7, 6, 5, 4, 3): 0.0028, 0.006, 0.04, 0.048, 0.09 -> 4. BH (x7/1 .. 7/5): 0.0028, 0.0035, 0.0187, 0.021, 0.042 -> 5.
    assert counts == [2, 4, 5]


def test_a_single_test_is_not_changed_and_ties_keep_their_order():
    assert mt.bonferroni([0.03]).adjusted == (0.03,) and mt.holm([0.03]).adjusted == (0.03,) and mt.benjamini_hochberg([0.03]).adjusted == (0.03,)
    assert mt.holm([0.02, 0.02]).adjusted == pytest.approx((0.04, 0.04))
    assert mt.bonferroni([0.7, 0.9]).adjusted == (1.0, 1.0)


def test_there_is_no_invented_sharpe_haircut_in_the_module_ECT_036():
    assert not [n for n in dir(mt) if "haircut" in n.lower() or "shrink" in n.lower()]
    assert sorted(mt.METHODS) == ["bh", "bonferroni", "holm"]


@pytest.mark.parametrize("bad", [[], [0.1, 1.5], [-0.1], [float("nan")], [[0.1, 0.2]], ["a"]])
def test_bad_p_values_raise_bad_input(bad):
    with pytest.raises(BadInput):
        mt.holm(bad)


def test_bad_alpha_and_method_raise_bad_input():
    for alpha in (0, 1, -0.1, True, "x"):
        with pytest.raises(BadInput):
            mt.bonferroni(P, alpha)
    with pytest.raises(BadInput):
        mt.correct(P, "sidak")


def test_registered_function_ECT_036():
    assert_registered("validation.multiple_testing", "ECT-036")
    assert_registered("validation.multiple_testing", "ECT-055")
    result = registry.call("validation.multiple_testing", p_values=P, method="holm", alpha=0.05)
    assert result.value["n_rejected"] == 2 and result.value["reject"] == [True, False, False, True]
    assert result.value["adjusted"] == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert result.params == {"alpha": 0.05, "n_tests": 4}


def test_registered_function_bad_data_gives_none_ECT_036():
    for bad in ([], [0.1, float("nan")], [0.2, 1.2]):
        result = registry.call("validation.multiple_testing", p_values=bad)
        assert result.value is None and result.warnings
    with pytest.raises(BadInput):
        registry.call("validation.multiple_testing", p_values="not a series")
    with pytest.raises(BadInput):
        registry.call("validation.multiple_testing", p_values=P, method="nope")


def test_4850_random_pairs_leave_almost_none_ECT_055():
    """Under the null every p-value is uniform: about 5 percent (242.5 of 4,850) pass raw, and the correction leaves none."""
    p = np.random.default_rng(20251122).uniform(size=4850)
    raw = int((p <= 0.05).sum())
    assert 200 <= raw <= 290
    assert mt.benjamini_hochberg(p, 0.05).n_rejected == 0
    assert mt.holm(p, 0.05).n_rejected == 0 and mt.bonferroni(p, 0.05).n_rejected == 0
