"""Interface of the risk module: section 4.4 signatures, registry coverage of every row and bad data handling."""
import inspect

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry, risk
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult

from .conftest import call

ROW_IDS = ["FT-066","SK-067","SK-068","SK-069","SK-070","SK-031","SK-032","FT-058","SK-062","SK-064","SK-065","SK-066","FT-049","FT-050","FT-053","SK-023","SK-027","SK-028","SK-029","SK-030","FT-065","SK-016","SK-017","SK-018","SK-019","SK-020","SK-021","SK-022","SK-033","SK-061","SK-063","FM-06","FT-048","CC-06","FT-041","FT-042","FT-043","FT-044","FT-045","FT-046","FT-047","SK-024","SK-025","SK-026","FT-056","FT-060"]

SERIES_FUNCTIONS = [
    ("risk.mean", {}), ("risk.mean_absolute_deviation", {}), ("risk.first_lower_partial_moment", {}), ("risk.variance", {}),
    ("risk.semi_variance", {}), ("risk.standard_deviation", {}), ("risk.semi_deviation", {}), ("risk.third_central_moment", {}),
    ("risk.skew", {}), ("risk.fourth_central_moment", {}), ("risk.kurtosis", {}), ("risk.fourth_lower_partial_moment", {}),
    ("risk.gini_mean_difference", {}), ("risk.hill_estimator", {}), ("risk.fit_distribution", {}), ("risk.select_univariate_dist", {}),
    ("risk.drawdown_risk", {}), ("risk.drawdown_series", {}), ("risk.drawdown_stats", {}), ("risk.worst_realization", {}),
    ("risk.max_drawdown", {}), ("risk.average_drawdown", {}), ("risk.drawdown_at_risk", {}), ("risk.cdar", {}), ("risk.edar", {}),
    ("risk.ulcer_index", {}), ("risk.rolling_cdar", {"window": 5}), ("risk.var", {}), ("risk.cvar", {}), ("risk.rolling_var", {"window": 5}),
    ("risk.rolling_cvar", {"window": 5}), ("risk.gpd_tail", {}), ("risk.evar", {}), ("risk.entropic_risk_measure", {}),
    ("risk.ewma_volatility", {}), ("risk.garch", {}), ("risk.roll_spread", {}),
]
RETURNS_ARG = {"risk.roll_spread": "close_prices"}


def test_every_row_has_a_registered_function():
    covered = {pid for spec in registry.list("risk") for pid in spec.piece_ids}
    assert set(ROW_IDS) <= covered, sorted(set(ROW_IDS) - covered)
    assert len(ROW_IDS) == len(set(ROW_IDS)) == 46


def test_registered_names_are_module_dot_function():
    for spec in registry.list("risk"):
        assert spec.module == "risk" and spec.name.startswith("risk.")
        assert spec.summary and spec.inputs and spec.piece_ids
        assert not spec.blocked


def test_public_signatures_follow_section_4_4():
    assert str(inspect.signature(risk.var)) == "(returns: 'Any', *, level: 'float' = 0.95, method: 'str' = 'historical') -> 'QuantResult'"
    assert str(inspect.signature(risk.cvar)) == "(returns: 'Any', *, level: 'float' = 0.95, method: 'str' = 'historical') -> 'QuantResult'"
    assert str(inspect.signature(risk.drawdown_risk)) == "(returns: 'Any', *, measure: 'str' = 'cdar', level: 'float' = 0.95) -> 'QuantResult'"
    params = inspect.signature(risk.var_backtest).parameters
    assert list(params)[:3] == ["returns", "var_series", "test"] and params["test"].default == "kupiec"
    assert list(inspect.signature(risk.stress).parameters) == ["panel", "weights", "scenario"]
    assert inspect.signature(risk.stress).parameters["scenario"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(risk.kelly_payoff).parameters) == ["p_win", "payoff"]


@pytest.mark.parametrize("name, extra", SERIES_FUNCTIONS)
def test_bad_data_gives_none_with_warnings(name, extra):
    arg = RETURNS_ARG.get(name, "returns")
    for label, data in (("empty", []), ("constant", [0.01] * 60), ("all_nan", [float("nan")] * 60)):
        res = call(name, **{arg: data}, **extra)
        assert isinstance(res, QuantResult)
        assert res.value is None, (name, label)
        assert res.warnings, (name, label)
        assert res.name == name and res.piece_ids


@pytest.mark.parametrize("name, extra", SERIES_FUNCTIONS)
def test_wrong_type_raises_bad_input(name, extra):
    arg = RETURNS_ARG.get(name, "returns")
    with pytest.raises(BadInput):
        call(name, **{arg: "not a series"}, **extra)
    with pytest.raises(BadInput):
        call(name, **{arg: pd.DataFrame({"a": [1.0, 2.0], "b": [1.0, 2.0]})}, **extra)


@pytest.mark.parametrize("name", ["risk.correlation", "risk.return_distribution", "risk.covariance_sqrt", "risk.vine_copula"])
def test_panel_functions_reject_other_types_and_degenerate_panels(name, panel):
    with pytest.raises(BadInput):
        call(name, panel=[[1.0, 2.0]])
    for degenerate in (panel.iloc[:0], panel * np.nan, panel * 0 + 0.01):
        res = call(name, panel=degenerate)
        assert res.value is None and res.warnings


def test_partial_nan_is_dropped_with_a_warning(ret):
    dirty = ret.copy()
    dirty.iloc[[3, 50, 400]] = np.nan
    res = call("risk.var", returns=dirty)
    clean = call("risk.var", returns=ret.drop(ret.index[[3, 50, 400]]))
    assert res.value == pytest.approx(clean.value, abs=1e-12)
    assert any("dropped 3" in w for w in res.warnings)


def test_result_metadata(ret):
    res = call("risk.var", returns=ret)
    assert res.name == "risk.var" and res.unit == "loss_fraction" and res.method == "historical"
    assert res.params == {"level": 0.95, "method": "historical"}
    assert len(res.inputs_hash) == 64
    assert res.as_of == ret.index[-1].to_pydatetime()
    assert call("risk.var", returns=ret).inputs_hash == res.inputs_hash
    assert call("risk.var", returns=ret, level=0.99).inputs_hash != res.inputs_hash
    assert res.to_dict()["piece_ids"] == list(res.piece_ids) and res.ok
    assert call("risk.var", returns=ret.to_numpy()).value == res.value


def test_unknown_or_missing_arguments_are_bad_input(ret):
    with pytest.raises(BadInput):
        call("risk.var", returns=ret, confidence=0.9)
    with pytest.raises(BadInput):
        call("risk.var")


def test_level_out_of_range_is_empty_not_an_error(ret):
    for level in (0.0, 1.0, -1, 5, float("nan")):
        res = call("risk.var", returns=ret, level=level)
        assert res.value is None and res.warnings
    with pytest.raises(BadInput):
        call("risk.var", returns=ret, level="95%")
