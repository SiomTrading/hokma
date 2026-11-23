"""Dispersion and concentration. Rows SK-031 and SK-032."""
import numpy as np
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.risk import _lib

from .conftest import call, piece_ids

TOL = 1e-9


def test_gini_mean_difference_SK_031(ret, tiny):
    assert call("risk.gini_mean_difference", returns=ret).value == pytest.approx(_lib.sk_measures.gini_mean_difference(ret.to_numpy()), abs=TOL)
    # hand value: the sorted returns weighted by the ordered weights give the Gini mean difference
    weights = np.array([(2 * i + 1 - 5) / (5 * 4 / 2) for i in range(5)])
    assert call("risk.gini_mean_difference", returns=tiny).value == pytest.approx(float(np.sort(tiny) @ weights), abs=TOL)
    assert call("risk.gini_mean_difference", returns=tiny).value == pytest.approx(0.026, abs=TOL)
    assert "SK-031" in piece_ids("risk.gini_mean_difference")


def test_owa_gmd_weights_SK_031():
    res = call("risk.owa_gmd_weights", n_observations=5)
    assert res.value["weights"] == pytest.approx([-0.4, -0.2, 0.0, 0.2, 0.4], abs=TOL)
    assert res.value["weights"] == pytest.approx(list(_lib.sk_measures.owa_gmd_weights(5)), abs=TOL)
    assert sum(call("risk.owa_gmd_weights", n_observations=40).value["weights"]) == pytest.approx(0.0, abs=TOL)
    assert call("risk.owa_gmd_weights", n_observations=1).value is None
    with pytest.raises(BadInput):
        call("risk.owa_gmd_weights", n_observations="5")
    assert "SK-031" in piece_ids("risk.owa_gmd_weights")


def test_effective_number_assets_SK_032(weights):
    assert call("risk.effective_number_assets", weights=weights).value == pytest.approx(5.0, abs=TOL)
    assert call("risk.effective_number_assets", weights={"a": 0.5, "b": 0.5}).value == pytest.approx(2.0, abs=TOL)
    assert call("risk.effective_number_assets", weights=[1.0, 0.0, 0.0, 0.0]).value == pytest.approx(1.0, abs=TOL)
    w = np.array([0.7, 0.2, 0.1])
    assert call("risk.effective_number_assets", weights=list(w)).value == pytest.approx(_lib.sk_measures.effective_number_assets(w), abs=TOL)
    assert call("risk.effective_number_assets", weights=list(w)).value == pytest.approx(1.0 / (0.49 + 0.04 + 0.01), abs=TOL)
    # weights that do not add to one are normalized first
    assert call("risk.effective_number_assets", weights=[2.0, 2.0]).value == pytest.approx(2.0, abs=TOL)
    res = call("risk.effective_number_assets", weights={"a": 0.0, "b": 0.0})
    assert res.value is None and res.warnings
    assert call("risk.effective_number_assets", weights=[]).value is None
    with pytest.raises(BadInput):
        call("risk.effective_number_assets", weights="x")
    assert "SK-032" in piece_ids("risk.effective_number_assets")
