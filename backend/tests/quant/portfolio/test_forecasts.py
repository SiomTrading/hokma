"""P-09: scale and cap of a forecast and the combination of forecasts, with values written by hand."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput

from .conftest import call


def test_p09_the_scalar_puts_the_average_absolute_forecast_at_10():
    out = call("portfolio.forecast_scale_cap", forecast=[1.0, -2.0, 3.0, -2.0])         # mean |f| = 2, scalar 5
    assert out.piece_ids == ("P-09",)
    assert out.value["scalar"] == pytest.approx(5.0) and out.value["mean_abs"] == pytest.approx(2.0)
    assert out.value["forecast"] == pytest.approx([5.0, -10.0, 15.0, -10.0]) and out.value["n_capped"] == 0


def test_p09_the_cap_of_20_cuts_the_scaled_values_and_keeps_the_sign():
    out = call("portfolio.forecast_scale_cap", forecast=[0.0, 0.0, 1.0, -9.0])          # mean |f| = 2.5, scalar 4: 4 and -36
    assert out.value["forecast"] == pytest.approx([0.0, 0.0, 4.0, -20.0]) and out.value["n_capped"] == 1
    custom = call("portfolio.forecast_scale_cap", forecast=[1.0, -3.0], target_abs=5, cap=8)   # mean 2, scalar 2.5: 2.5 and -7.5
    assert custom.value["forecast"] == pytest.approx([2.5, -7.5])


def test_p09_a_series_is_accepted_and_missing_values_are_left_out_of_the_average():
    out = call("portfolio.forecast_scale_cap", forecast=pd.Series([2.0, float("nan"), -2.0]))
    assert out.value["scalar"] == pytest.approx(5.0)
    assert out.value["forecast"][0] == pytest.approx(10.0) and out.value["forecast"][1] is None


@pytest.mark.parametrize("forecast", [[0.0, 0.0], [], [float("nan")], [float("inf")]])
def test_p09_a_forecast_with_nothing_to_scale_gives_no_value_and_says_why(forecast):
    out = call("portfolio.forecast_scale_cap", forecast=forecast)
    assert out.value is None and out.warnings


def test_p09_bad_types_and_parameters_are_refused():
    with pytest.raises(BadInput):
        call("portfolio.forecast_scale_cap", forecast="abc")
    assert call("portfolio.forecast_scale_cap", forecast=[1.0], cap=0).value is None
    assert call("portfolio.forecast_scale_cap", forecast=[1.0], target_abs=-1).value is None
    with pytest.raises(BadInput):
        call("portfolio.forecast_scale_cap", forecast=[1.0], cap="20")
    with pytest.raises(BadInput):
        call("portfolio.forecast_scale_cap", forecast=[1.0], path="/etc/passwd")


def _rules():
    # a and b: products of the centred values sum to zero, so their correlation is exactly zero
    return pd.DataFrame({"a": [10.0, -10.0, 10.0, -10.0], "b": [10.0, 10.0, -10.0, -10.0]})


def test_p09_equal_weights_and_uncorrelated_rules_get_a_multiplier_of_the_square_root_of_two():
    out = call("portfolio.forecast_combine", forecasts=_rules())
    assert out.value["weights"] == {"a": 0.5, "b": 0.5}
    assert out.value["diversification_multiplier"] == pytest.approx(math.sqrt(2.0))              # 1 / sqrt(0.5)
    assert out.value["combined"] == pytest.approx([10 * math.sqrt(2), 0.0, 0.0, -10 * math.sqrt(2)])


def test_p09_weights_are_normalized_and_a_given_multiplier_is_used_as_it_is():
    out = call("portfolio.forecast_combine", forecasts=_rules(), weights={"a": 3, "b": 1}, diversification_multiplier=1.0)
    assert out.value["weights"] == {"a": 0.75, "b": 0.25}
    assert out.value["combined"] == pytest.approx([10.0, -5.0, 5.0, -10.0])                      # 0.75 a + 0.25 b
    assert out.value["diversification_multiplier"] == 1.0
    scaled = call("portfolio.forecast_combine", forecasts=_rules(), weights={"a": 3, "b": 1}, diversification_multiplier=1.2)
    assert scaled.value["combined"] == pytest.approx([12.0, -6.0, 6.0, -12.0])


def test_p09_the_combination_is_capped_and_the_cap_is_counted():
    out = call("portfolio.forecast_combine", forecasts=_rules(), diversification_multiplier=2.0, cap=15)     # 20, 0, 0, -20 become 15
    assert out.value["combined"] == pytest.approx([15.0, 0.0, 0.0, -15.0]) and out.value["n_capped"] == 2


def test_p09_identical_rules_get_no_extra_multiplier_and_the_multiplier_has_a_ceiling():
    same = pd.DataFrame({"a": [1.0, 2.0, 3.0, 5.0], "b": [1.0, 2.0, 3.0, 5.0]})
    assert call("portfolio.forecast_combine", forecasts=same).value["diversification_multiplier"] == pytest.approx(1.0)
    # four uncorrelated rules give 1 / sqrt(0.25) = 2: a ceiling of 1.5 holds it down
    four = pd.DataFrame({"a": [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0], "b": [1.0, 1.0, -1.0, -1.0, 1.0, 1.0, -1.0, -1.0],
                         "c": [1.0, 1.0, 1.0, 1.0, -1.0, -1.0, -1.0, -1.0], "d": [1.0, -1.0, -1.0, 1.0, -1.0, 1.0, 1.0, -1.0]})
    assert call("portfolio.forecast_combine", forecasts=four).value["diversification_multiplier"] == pytest.approx(2.0)
    assert call("portfolio.forecast_combine", forecasts=four, max_multiplier=1.5).value["diversification_multiplier"] == pytest.approx(1.5)


def test_p09_negatively_correlated_rules_do_not_raise_the_multiplier_above_the_uncorrelated_one():
    opposite = pd.DataFrame({"a": [1.0, -1.0, 2.0, -2.0], "b": [-1.0, 1.0, -2.0, 2.0]})
    assert call("portfolio.forecast_combine", forecasts=opposite).value["diversification_multiplier"] == pytest.approx(math.sqrt(2.0))


def test_p09_a_missing_value_drops_that_rule_at_that_date_and_the_others_share_its_weight():
    frame = pd.DataFrame({"a": [10.0, 10.0], "b": [20.0, float("nan")]})
    out = call("portfolio.forecast_combine", forecasts=frame, diversification_multiplier=1.0)
    assert out.value["combined"] == pytest.approx([15.0, 10.0])
    empty_row = pd.DataFrame({"a": [float("nan"), 4.0], "b": [float("nan"), 6.0]})
    assert call("portfolio.forecast_combine", forecasts=empty_row, diversification_multiplier=1.0).value["combined"] == [None, pytest.approx(5.0)]


def test_p09_a_single_rule_is_returned_capped_with_a_multiplier_of_one():
    out = call("portfolio.forecast_combine", forecasts=pd.DataFrame({"a": [5.0, 30.0, -30.0]}))
    assert out.value["combined"] == pytest.approx([5.0, 20.0, -20.0]) and out.value["diversification_multiplier"] == 1.0


def test_p09_bad_inputs_are_refused_or_say_why():
    with pytest.raises(BadInput):
        call("portfolio.forecast_combine", forecasts=[1.0, 2.0])
    with pytest.raises(BadInput):
        call("portfolio.forecast_combine", forecasts=_rules(), weights={"zzz": 1})
    with pytest.raises(BadInput):
        call("portfolio.forecast_combine", forecasts=_rules(), weights=[0.5, 0.5])
    assert call("portfolio.forecast_combine", forecasts=_rules(), weights={"a": 0, "b": 0}).value is None
    assert call("portfolio.forecast_combine", forecasts=_rules(), weights={"a": -1, "b": 2}).value is None
    assert call("portfolio.forecast_combine", forecasts=_rules(), diversification_multiplier=0.5).value is None
    assert call("portfolio.forecast_combine", forecasts=_rules().iloc[:0]).value is None
    wide = pd.DataFrame([[1.0] * (registry.describe("portfolio.forecast_combine").fn.__globals__["MAX_RULES"] + 1)])
    with pytest.raises(BadInput):
        call("portfolio.forecast_combine", forecasts=wide)
