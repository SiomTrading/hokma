"""SK-085 to SK-089: forecast units, the three alpha estimators and the evaluation of a forecast."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _lib

from .conftest import call

ROWS, ASSETS = 300, 40
SCORE = [{"kind": "passthrough", "name": "s", "params": {"field": "score"}}]
RAW = {"outlier": "passthrough", "scoring": "passthrough"}


def _fields() -> tuple[dict[str, pd.DataFrame], np.ndarray]:
    """A score z at each date and a return that is exactly 0.01 * z of the day before: the forecast is perfect."""
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2022-01-03", periods=ROWS)
    cols = [f"A{i}" for i in range(ASSETS)]
    z = rng.normal(0, 1, (ROWS, ASSETS))
    ret = np.zeros((ROWS, ASSETS))
    ret[1:] = 0.01 * z[:-1]

    def frame(a: np.ndarray) -> pd.DataFrame:
        return pd.DataFrame(a, index=idx, columns=cols)

    fields = {"returns": frame(ret), "idio_returns": frame(ret), "idio_variances": frame(np.full((ROWS, ASSETS), 1e-4)), "score": frame(z),
              "market_cap": frame(np.exp(rng.normal(10, 1, (ROWS, ASSETS)))), "book_equity": frame(np.exp(rng.normal(9, 1, (ROWS, ASSETS))))}
    return fields, z


def _vector(result, key: str = "alpha") -> np.ndarray:
    return np.array(list(result.value[key].values()))


def test_sk085_idio_sharpe_is_converted_with_the_volatility_by_hand():
    result = call("portfolio.alpha_base", forecast={"a": 2.0, "b": -1.0}, unit="idio_sharpe", idio_variances={"a": 0.04, "b": 0.09})
    assert result.value["alpha"] == {"a": pytest.approx(0.4), "b": pytest.approx(-0.3)}           # sharpe times sqrt(variance)
    as_return = call("portfolio.alpha_base", forecast={"a": 0.02}, unit="idio_return")
    assert as_return.value["alpha"] == {"a": 0.02} and result.piece_ids == ("SK-085",)


def test_sk085_lists_the_units_and_the_estimators_with_their_base_class():
    out = call("portfolio.alpha_base").value
    assert set(out["units"]) == {"idio_return", "idio_sharpe"} and set(_lib.FORECAST_UNITS) == set(out["units"])
    assert {k: v["extends_base"] for k, v in out["estimators"].items()} == {"fixed_weighted": True, "ew_sharpe_optimal": True, "predictor": True}
    assert out["estimators"]["ew_sharpe_optimal"]["needs_fields"] == ["idio_returns", "idio_variances"]
    assert "book_to_price" in out["descriptors"] and "passthrough" in out["descriptors"]


def test_sk085_out_of_domain_gives_none():
    assert call("portfolio.alpha_base", unit="idio_sharpe", forecast={"a": 1.0}).value is None                     # no variances
    assert call("portfolio.alpha_base", unit="idio_sharpe", forecast={"a": 1.0}, idio_variances={"b": 0.1}).value is None
    assert call("portfolio.alpha_base", unit="idio_sharpe", forecast={"a": 1.0}, idio_variances={"a": -0.1}).value is None
    assert call("portfolio.alpha_base", unit="dollars").value is None
    assert call("portfolio.alpha_base", forecast={}).value is None
    assert call("portfolio.alpha_base", forecast={"a": float("nan")}).value is None


def test_sk087_fixed_weights_give_scale_times_the_score_by_hand():
    fields, z = _fields()
    result = call("portfolio.alpha_fixed_weighted", fields=fields, descriptors=SCORE, forecast_scale=0.01, **RAW)
    assert np.allclose(_vector(result), 0.01 * z[-1], atol=1e-12, rtol=0)
    assert result.piece_ids == ("SK-087",) and result.value["descriptor_weights"] == {"s": 1.0}


def test_sk087_two_descriptors_with_signed_weights_by_hand():
    fields, z = _fields()
    fields["score2"] = pd.DataFrame(-z, index=fields["score"].index, columns=fields["score"].columns)
    two = SCORE + [{"kind": "passthrough", "name": "t", "params": {"field": "score2"}}]
    result = call("portfolio.alpha_fixed_weighted", fields=fields, descriptors=two, forecast_scale=0.01, weights=[2.0, 1.0], **RAW)
    # composite = (2 z - z) / (|2| + |1|) = z / 3
    assert np.allclose(_vector(result), 0.01 * z[-1] / 3.0, atol=1e-12, rtol=0)


def test_sk087_matches_the_library_with_the_default_scoring_and_idio_sharpe():
    fields, _ = _fields()
    result = call("portfolio.alpha_fixed_weighted", fields=fields, descriptors=[{"kind": "book_to_price"}], forecast_scale=0.002)
    est = _lib.sk_alpha.FixedWeightedAlpha(descriptors=[("book_to_price", _lib.sk_descriptor.BookToPrice())], forecast_scale=0.002)
    est.fit(_lib.sk_panel(fields))
    assert np.allclose(_vector(result), est.alpha_, atol=1e-8, rtol=0)
    sharpe = call("portfolio.alpha_fixed_weighted", fields=fields, descriptors=SCORE, forecast_scale=0.5, forecast_unit="idio_sharpe", **RAW)
    assert np.allclose(_vector(sharpe), 0.5 * 0.01 * fields["score"].iloc[-1].to_numpy(), atol=1e-12, rtol=0)      # volatility is sqrt(1e-4)


def test_sk087_missing_fields_and_bad_parameters_give_none_or_raise():
    fields, _ = _fields()
    out = call("portfolio.alpha_fixed_weighted", fields={k: v for k, v in fields.items() if k != "book_equity"}, descriptors=[{"kind": "book_to_price"}],
               forecast_scale=0.01)
    assert out.value is None and "data_missing:book_equity" in out.warnings[0]
    assert call("portfolio.alpha_fixed_weighted", fields=fields, descriptors=[{"kind": "nonsense"}], forecast_scale=0.01).value is None
    assert call("portfolio.alpha_fixed_weighted", fields=fields, descriptors=SCORE, forecast_scale=0.01, forecast_unit="pounds").value is None
    assert call("portfolio.alpha_fixed_weighted", fields=fields, descriptors=SCORE, forecast_scale=float("nan")).value is None
    assert call("portfolio.alpha_fixed_weighted", fields=fields, descriptors=SCORE, forecast_scale=0.01, scoring="magic").value is None
    assert call("portfolio.alpha_fixed_weighted", fields=fields, descriptors=SCORE, forecast_scale=0.01, min_coverage=2.0).value is None
    assert call("portfolio.alpha_fixed_weighted", fields={k: v.iloc[:5] for k, v in fields.items()}, descriptors=SCORE, forecast_scale=0.01).value is None
    assert call("portfolio.alpha_fixed_weighted", fields={}, descriptors=SCORE, forecast_scale=0.01).value is None
    with pytest.raises(BadInput):
        call("portfolio.alpha_fixed_weighted", fields=fields, descriptors=SCORE, forecast_scale=0.01, weights=[1.0, 2.0])
    with pytest.raises(BadInput):
        call("portfolio.alpha_fixed_weighted", fields=fields, descriptors=[], forecast_scale=0.01)
    with pytest.raises(BadInput):
        call("portfolio.alpha_fixed_weighted", fields=fields, descriptors=[{"kind": "book_to_price", "params": {"bad": 1}}], forecast_scale=0.01)
    with pytest.raises(BadInput):
        call("portfolio.alpha_fixed_weighted", fields="x", descriptors=SCORE, forecast_scale=0.01)


def test_sk086_the_estimated_coefficient_is_the_true_one_when_the_relation_is_exact():
    fields, z = _fields()
    result = call("portfolio.alpha_ew_sharpe_optimal", fields=fields, descriptors=SCORE, **RAW)
    assert result.piece_ids == ("SK-086",)
    assert result.value["coef"]["s"] == pytest.approx(0.01, abs=1e-6)        # the return is 0.01 times the score
    assert np.allclose(_vector(result), 0.01 * z[-1], atol=1e-6, rtol=0)


def test_sk086_matches_the_library_and_needs_the_idiosyncratic_fields():
    fields, _ = _fields()
    result = call("portfolio.alpha_ew_sharpe_optimal", fields=fields, descriptors=SCORE, half_life=10, horizon=2, **RAW)
    est = _lib.sk_alpha.EWSharpeOptimalAlpha(descriptors=[("s", _lib.sk_descriptor.Passthrough(field="score"))], half_life=10, horizon=2,
                                             outlier_transformer="passthrough", scoring_transformer="passthrough")
    est.fit(_lib.sk_panel(fields))
    assert np.allclose(_vector(result), est.alpha_, atol=1e-8, rtol=0)
    out = call("portfolio.alpha_ew_sharpe_optimal", fields={k: v for k, v in fields.items() if k != "idio_variances"}, descriptors=SCORE, **RAW)
    assert out.value is None and "data_missing:idio_variances" in out.warnings[0]
    assert call("portfolio.alpha_ew_sharpe_optimal", fields=fields, descriptors=SCORE, half_life=0, **RAW).value is None
    assert call("portfolio.alpha_ew_sharpe_optimal", fields=fields, descriptors=SCORE, horizon=0, **RAW).value is None


def test_sk088_a_linear_predictor_learns_the_exact_relation():
    fields, z = _fields()
    result = call("portfolio.alpha_predictor", fields=fields, descriptors=SCORE, predictor="linear", **RAW)
    assert result.piece_ids == ("SK-088",)
    assert np.allclose(_vector(result), 0.01 * z[-1], atol=2e-5, rtol=0)        # calibration to return units leaves a small bias


def test_sk088_matches_the_library_for_ridge_and_rejects_unknown_predictors():
    fields, _ = _fields()
    result = call("portfolio.alpha_predictor", fields=fields, descriptors=SCORE, predictor="ridge", predictor_alpha=2.0, **RAW)
    est = _lib.sk_alpha.PredictorAlpha(predictor=_lib.sk_linear_model.Ridge(alpha=2.0), descriptors=[("s", _lib.sk_descriptor.Passthrough(field="score"))],
                                       outlier_transformer="passthrough", scoring_transformer="passthrough")
    est.fit(_lib.sk_panel(fields))
    assert np.allclose(_vector(result), est.alpha_, atol=1e-8, rtol=0)
    assert call("portfolio.alpha_predictor", fields=fields, descriptors=SCORE, predictor="forest", **RAW).value is None
    assert call("portfolio.alpha_predictor", fields=fields, descriptors=SCORE, predictor="ridge", predictor_alpha=-1.0, **RAW).value is None
    no_target = {k: v for k, v in fields.items() if k != "idio_returns"}
    assert call("portfolio.alpha_predictor", fields=no_target, descriptors=SCORE, **RAW).value is None


def test_sk089_a_perfect_forecast_has_an_information_coefficient_of_one():
    fields, _ = _fields()
    spec = {"kind": "fixed_weighted", "descriptors": SCORE, "forecast_scale": 0.01, **RAW}
    result = call("portfolio.alpha_evaluation", fields=fields, estimators={"perfect": spec}, target="returns")
    assert result.piece_ids == ("SK-089",)
    assert result.value["ic"]["perfect"]["spearman_ic"]["mean"] == pytest.approx(1.0, abs=1e-9)
    assert result.value["ic"]["perfect"]["pearson_ic"]["mean"] == pytest.approx(1.0, abs=1e-9)
    assert result.value["ic"]["perfect"]["spearman_ic"]["hit_rate"] == 1.0
    assert "comparison" not in result.value


def test_sk089_two_estimators_are_compared_and_the_reversed_one_has_negative_ic():
    fields, _ = _fields()
    good = {"kind": "fixed_weighted", "descriptors": SCORE, "forecast_scale": 0.01, **RAW}
    bad = {"kind": "fixed_weighted", "descriptors": SCORE, "forecast_scale": 0.01, "weights": [-1.0], **RAW}
    result = call("portfolio.alpha_evaluation", fields=fields, estimators={"good": good, "bad": bad}, target="returns", holding_period=1)
    assert result.value["ic"]["good"]["spearman_ic"]["mean"] > 0.99 and result.value["ic"]["bad"]["spearman_ic"]["mean"] < -0.99
    assert set(result.value["comparison"]) == {"ic", "portfolio"} and {"good", "bad"} <= set(result.value["portfolio"])


def test_sk089_matches_the_library_evaluation():
    fields, _ = _fields()
    spec = {"kind": "fixed_weighted", "descriptors": [{"kind": "book_to_price"}], "forecast_scale": 0.01}
    result = call("portfolio.alpha_evaluation", fields=fields, estimators={"v": spec}, target="returns", n_forward_periods=3)
    est = _lib.sk_alpha.FixedWeightedAlpha(descriptors=[("book_to_price", _lib.sk_descriptor.BookToPrice())], forecast_scale=0.01)
    direct = _lib.sk_alpha.alpha_forecast_evaluation(est, _lib.sk_panel(fields), target="returns", n_forward_periods=3, name="v")
    assert result.value["ic"]["v"]["spearman_ic"]["mean"] == pytest.approx(float(direct.ic_summary().loc["spearman_ic", "mean"]), abs=1e-8)


def test_sk089_missing_target_unknown_estimator_and_wrong_shapes():
    fields, _ = _fields()
    spec = {"kind": "fixed_weighted", "descriptors": SCORE, "forecast_scale": 0.01, **RAW}
    assert call("portfolio.alpha_evaluation", fields=fields, estimators={"x": spec}, target="nothing").value is None
    assert call("portfolio.alpha_evaluation", fields=fields, estimators={"x": {**spec, "kind": "oracle"}}).value is None
    assert call("portfolio.alpha_evaluation", fields={k: v.iloc[:5] for k, v in fields.items()}, estimators={"x": spec}).value is None
    assert call("portfolio.alpha_evaluation", fields=fields, estimators={"x": spec}, holding_period=0).value is None
    with pytest.raises(BadInput):
        call("portfolio.alpha_evaluation", fields=fields, estimators={})
    with pytest.raises(BadInput):
        call("portfolio.alpha_evaluation", fields=fields, estimators={"x": {**spec, "surprise": 1}})
    with pytest.raises(BadInput):
        call("portfolio.alpha_evaluation", fields=fields, estimators={"x": "fixed_weighted"})
    assert registry.describe("portfolio.alpha_evaluation").module == "portfolio"
