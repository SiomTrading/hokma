"""Dependence. Rows FT-066, SK-067, SK-068, SK-069 and SK-070."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.risk import _lib

from .conftest import call, piece_ids

TOL = 1e-9
FT = _lib.ft_copula


def _pseudo(a, b):
    return _lib.sk_dist.compute_pseudo_observations(np.column_stack([a.to_numpy(), b.to_numpy()]))


@pytest.fixture
def pair(panel):
    return panel["AAA"], panel["BBB"]


@pytest.fixture
def independent_pair(panel):
    rng = np.random.default_rng(11)
    return (pd.Series(rng.standard_normal(1000), index=panel.index), pd.Series(rng.standard_normal(1000), index=panel.index))


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.parametrize("family", ["gaussian", "student_t", "clayton", "gumbel", "frank"])
def test_copula_dependence_matches_origin_FT_066(pair, family):
    a, b = pair
    res = call("risk.copula_dependence", x=a, y=b, family=family)
    origin = {"gaussian": FT.get_gaussian_copula_parameters, "student_t": FT.get_student_t_copula_parameters,
              "clayton": FT.get_clayton_copula_parameters, "gumbel": FT.get_gumbel_copula_parameters,
              "frank": FT.get_frank_copula_parameters}[family](a, b)
    for key, val in origin.items():
        assert res.value[key.lower().replace(" ", "_").replace("-", "_")] == pytest.approx(float(val), abs=TOL)
    assert res.method == family and "FT-066" in res.piece_ids


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_copula_dependence_closed_forms_FT_066(pair):
    a, b = pair
    clayton = call("risk.copula_dependence", x=a, y=b, family="clayton").value
    assert clayton["lower_tail_dependence"] == pytest.approx(2 ** (-1.0 / clayton["theta"]), abs=1e-9)
    gumbel = call("risk.copula_dependence", x=a, y=b, family="gumbel").value
    assert gumbel["upper_tail_dependence"] == pytest.approx(2 - 2 ** (1.0 / gumbel["theta"]), abs=1e-9)
    assert call("risk.copula_dependence", x=a, y=b, family="gaussian").value["lower_tail_dependence"] == 0.0
    empirical = call("risk.copula_dependence", x=a, y=b, family="empirical", level=0.95).value
    origin = FT.get_tail_dependence_coefficient(a, b, q=0.95, method="empirical")
    assert empirical["lower_tail_dependence"] == pytest.approx(origin["Lower Tail Dependence"], abs=TOL)
    assert empirical["upper_tail_dependence"] == pytest.approx(origin["Upper Tail Dependence"], abs=TOL)
    # hand value: a series against itself has full tail dependence
    assert call("risk.copula_dependence", x=a, y=a, family="empirical").value["lower_tail_dependence"] == pytest.approx(1.0, abs=TOL)


def test_copula_dependence_bad_input_FT_066(pair):
    a, b = pair
    assert call("risk.copula_dependence", x=a, y=b, family="vine").value is None
    assert call("risk.copula_dependence", x=a, y=b, level=1.0).value is None
    assert call("risk.copula_dependence", x=a.iloc[:10], y=b.iloc[:10]).value is None
    assert call("risk.copula_dependence", x=a, y=pd.Series(0.0, index=a.index)).value is None
    with pytest.raises(BadInput):
        call("risk.copula_dependence", x=a.iloc[:50].to_list(), y=b.iloc[:40].to_list())
    with pytest.raises(BadInput):
        call("risk.copula_dependence", x="a", y=b)


@pytest.mark.parametrize("family", ["gaussian", "student_t", "clayton", "gumbel", "joe", "independent"])
def test_copula_fit_matches_origin_SK_067(pair, family):
    a, b = pair
    res = call("risk.copula_fit", x=a, y=b, family=family, seed=0)
    u = _pseudo(a, b)
    origin = _lib.COPULA_FAMILIES[family](random_state=0).fit(u)
    assert res.value["log_likelihood"] == pytest.approx(float(origin.score_samples(u).sum()), abs=TOL)
    assert res.value["lower_tail_dependence"] == pytest.approx(float(origin.lower_tail_dependence), abs=TOL)
    assert res.value["upper_tail_dependence"] == pytest.approx(float(origin.upper_tail_dependence), abs=TOL)
    assert res.value["n_params"] == origin.n_params
    for attr, key in (("rho_", "rho"), ("theta_", "theta"), ("dof_", "dof")):
        if hasattr(origin, attr):
            assert res.value[key] == pytest.approx(float(getattr(origin, attr)), abs=TOL)
    assert res.method == family and res.params == {"family": family, "seed": 0}
    assert "SK-067" in piece_ids("risk.copula_fit")


def test_copula_fit_closed_forms_SK_067(pair):
    a, b = pair
    clayton = call("risk.copula_fit", x=a, y=b, family="clayton").value
    expected = {"180°": ("upper_tail_dependence", 2 ** (-1.0 / clayton["theta"])), "0°": ("lower_tail_dependence", 2 ** (-1.0 / clayton["theta"]))}
    key, hand = expected[clayton["rotation"]]
    assert clayton[key] == pytest.approx(hand, abs=1e-9)
    gumbel = call("risk.copula_fit", x=a, y=b, family="gumbel").value
    assert gumbel["rotation"] == "0°"
    assert gumbel["upper_tail_dependence"] == pytest.approx(2 - 2 ** (1.0 / gumbel["theta"]), abs=1e-9)
    independent = call("risk.copula_fit", x=a, y=b, family="independent").value
    assert independent["n_params"] == 0 and independent["log_likelihood"] == pytest.approx(0.0, abs=TOL)
    gaussian = call("risk.copula_fit", x=a, y=b, family="gaussian").value
    assert gaussian["n_params"] == 1 and gaussian["upper_tail_dependence"] == 0.0
    # a series with itself is fully dependent
    assert call("risk.copula_fit", x=a, y=a, family="gaussian").value["rho"] > 0.99
    assert call("risk.copula_fit", x=a, y=b, family="frank").value is None
    assert call("risk.copula_fit", x=a, y=pd.Series(0.01, index=a.index)).value is None


def test_select_bivariate_copula_SK_068(pair, independent_pair):
    a, b = pair
    res = call("risk.select_bivariate_copula", x=a, y=b)
    origin = _lib.sk_dist.select_bivariate_copula(_pseudo(a, b), copula_candidates=[
        cls(random_state=0) for key, cls in _lib.COPULA_FAMILIES.items() if key != "independent"])
    names = {cls: key for key, cls in _lib.COPULA_FAMILIES.items()}
    assert res.value["family"] == names[type(origin)]
    assert res.value["log_likelihood"] == pytest.approx(float(origin.score_samples(_pseudo(a, b)).sum()), abs=TOL)
    # hand value: two unrelated series keep the independence copula
    x, y = independent_pair
    assert call("risk.select_bivariate_copula", x=x, y=y).value["family"] == "independent"
    assert call("risk.select_bivariate_copula", x=a, y=b, criterion="bic").method == "lowest_bic"
    assert call("risk.select_bivariate_copula", x=a, y=b, criterion="mdl").value is None
    assert "SK-068" in piece_ids("risk.select_bivariate_copula")


def test_tail_concentration_SK_069(pair, independent_pair):
    a, b = pair
    q = [0.05, 0.1, 0.5, 0.9, 0.95]
    res = call("risk.tail_concentration", x=a, y=b, quantiles=q)
    origin = _lib.sk_dist.empirical_tail_concentration(_pseudo(a, b), np.array(q))
    assert res.value["concentration"] == pytest.approx(list(origin), abs=TOL)
    assert res.value["quantiles"] == q
    # hand values: a series against itself falls and rises together every time; unrelated series match the quantile
    same = call("risk.tail_concentration", x=a, y=a, quantiles=[0.05, 0.95]).value["concentration"]
    assert same == pytest.approx([1.0, 1.0], abs=TOL)
    x, y = independent_pair
    unrelated = call("risk.tail_concentration", x=x, y=y, quantiles=[0.1]).value["concentration"][0]
    assert unrelated == pytest.approx(0.1, abs=0.06)
    assert call("risk.tail_concentration", x=a, y=b, quantiles=[0.0, 1.5]).value is None
    with pytest.raises(BadInput):
        call("risk.tail_concentration", x=a, y=b, quantiles=["a"])
    assert "SK-069" in piece_ids("risk.tail_concentration")


def test_vine_copula_SK_070(panel):
    res = call("risk.vine_copula", panel=panel, seed=0)
    origin = _lib.sk_dist.VineCopula(random_state=0, max_depth=4).fit(panel)
    assert res.value["n_params"] == origin.n_params
    assert res.value["log_likelihood"] == pytest.approx(float(origin.score_samples(panel).sum()), abs=1e-6)
    assert res.value["assets"] == list(panel.columns)
    # hand value: 5 assets with depth 4 hold 10 pair copulas, every fitted marginal has at least 2 parameters
    assert res.value["n_trees"] == 4 and res.value["n_params"] >= 10
    assert set(res.value["marginals"]) == set(panel.columns)
    assert res.value["bic"] == pytest.approx(res.value["n_params"] * np.log(1000) - 2 * res.value["log_likelihood"], abs=1e-6)
    shallow = call("risk.vine_copula", panel=panel, max_depth=2)
    assert shallow.value["n_trees"] == 2
    assert call("risk.vine_copula", panel=panel, max_depth=1).value is None
    assert call("risk.vine_copula", panel=panel.iloc[:10]).value is None
    with pytest.raises(BadInput):
        call("risk.vine_copula", panel=panel[["AAA"]])
    assert "SK-070" in piece_ids("risk.vine_copula")
