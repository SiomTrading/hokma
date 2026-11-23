"""Evidence bundle for the desk judge: AH-67."""
import numpy as np
import pandas as pd
import pytest
import sys

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant import validation

from .conftest import assert_registered

ev = sys.modules["hokma.quant.validation.evidence"]


def _matrix(seed: int, drift: float = 0.0, rows: int = 640, strategies: int = 20) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(rng.normal(0.0, 0.01, (rows, strategies)), index=pd.bdate_range("2020-01-01", periods=rows),
                         columns=[f"s{i}" for i in range(strategies)])
    frame["s0"] += drift
    return frame


def test_bundle_with_a_real_edge_CPCV_PBO_DSR_WF_AH_67():
    bundle = validation.evidence(_matrix(20261122, drift=0.002), n_trials=20)  # default 16 blocks
    assert bundle.best_strategy == "s0" and bundle.n_strategies == 20 and bundle.n_trials == 20
    assert bundle.leakage_free is True
    assert bundle.pbo is not None and bundle.pbo < 0.2
    assert bundle.dsr is not None and bundle.dsr > 0.95
    assert bundle.cpcv["n_splits"] == 45 and bundle.cpcv["loss_rate"] < 0.2 and bundle.cpcv["oos_sharpe_mean"] > 0
    assert bundle.walk_forward["n_splits"] >= 3 and bundle.walk_forward["oos_sharpe_mean"] > 0
    assert bundle.sr0 > 0


def test_bundle_without_an_edge_does_not_pass_AH_67():
    bundle = ev.bundle(_matrix(20261122), n_trials=20, n_blocks=8)
    assert bundle.dsr < 0.95
    assert bundle.pbo > 0.2


def test_judge_payload_is_stable_and_hashed_AH_67():
    first = ev.bundle(_matrix(3, drift=0.001), n_trials=20, n_blocks=8).judge_payload()
    second = ev.bundle(_matrix(3, drift=0.001), n_trials=20, n_blocks=8).judge_payload()
    assert first["content_hash"] == second["content_hash"]
    assert first["warnings"] == [] and first["parameters"]["n_blocks"] == 8
    changed = ev.bundle(_matrix(3, drift=0.0015), n_trials=20, n_blocks=8).judge_payload()
    assert changed["content_hash"] != first["content_hash"]


def test_registered_function_returns_the_payload_AH_67():
    result = registry.call("validation.evidence", matrix=_matrix(5, drift=0.002), n_trials=20, n_blocks=8)
    assert result.value["best_strategy"] == "s0" and "content_hash" in result.value
    assert_registered("validation.evidence", "AH-67")


def test_degenerate_inputs_give_none_AH_67():
    constant = pd.DataFrame(0.01, index=range(640), columns=["a", "b"])
    nan = pd.DataFrame(np.nan, index=range(640), columns=["a", "b"])
    short = _matrix(1, rows=40)
    for bad in (pd.DataFrame({"a": []}, dtype=float), constant, nan, short):
        result = registry.call("validation.evidence", matrix=bad)
        assert result.value is None and result.warnings
    assert validation.evidence(constant, n_trials=2).best_strategy is None


def test_wrong_type_is_bad_input_AH_67():
    with pytest.raises(BadInput):
        validation.evidence([[1, 2], [3, 4]], n_trials=2)

def test_module_entry_point_is_reachable_from_the_package_AH_67():
    assert validation.evidence.bundle is ev.bundle
