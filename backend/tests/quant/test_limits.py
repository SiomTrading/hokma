"""Resource limits on every quant call an agent can make."""
import time

import numpy as np
import pandas as pd
import pytest

from hokma.quant import limits, registry
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult


@pytest.fixture(autouse=True)
def _isolated_registry():
    saved = dict(registry._FUNCTIONS)
    registry._reset_for_tests()
    yield
    kept = {n: s for n, s in registry._FUNCTIONS.items() if str(getattr(s.fn, "__module__", "")).startswith("hokma.quant.")}
    registry._reset_for_tests()
    registry._FUNCTIONS.update(saved)
    registry._FUNCTIONS.update(kept)   # modules imported during the test stay registered: Python will not import them again


def test_param_caps_raise_bad_input():
    for name, value in [("n_samples", 100_001), ("n_observations", 1_000_001), ("horizon", 1_001)]:
        with pytest.raises(BadInput):
            limits.check_param(name, value)
    limits.check_param("horizon", 1_000)


def test_max_depth_is_capped_by_assets():
    with pytest.raises(BadInput):
        limits.check_max_depth(5, 3)
    limits.check_max_depth(3, 3)


def test_registry_call_rejects_large_params_and_inputs():

    @registry.register("risk.dummy", module="risk", summary="x", inputs={"horizon": {}, "panel": {}, "max_depth": {}})
    def dummy(**k):
        return QuantResult(name="risk.dummy", value=1.0, unit="u", method="m")

    with pytest.raises(BadInput):
        registry.call("risk.dummy", horizon=5_000)
    with pytest.raises(BadInput):
        registry.call("risk.dummy", panel=np.zeros((6_000, 1_000)))
    with pytest.raises(BadInput):
        registry.call("risk.dummy", panel=pd.DataFrame(np.zeros((10, 3))), max_depth=4)
    assert registry.call("risk.dummy", horizon=10).value == 1.0


def test_module_functions_check_their_own_size_parameters():
    from hokma.quant.risk import dispersion, scenarios, volatility
    with pytest.raises(BadInput):
        dispersion.owa_gmd_weights(2_000_000)
    with pytest.raises(BadInput):
        volatility.garch(pd.Series(np.random.default_rng(1).normal(0, 0.01, 300)), horizon=5_000)
    with pytest.raises(BadInput):
        scenarios.synthetic_scenarios(pd.DataFrame(np.random.default_rng(1).normal(0, 0.01, (300, 3))), n_samples=500_000)


def test_combination_budget():
    import math
    limits.check_combinations(10, 2)
    with pytest.raises(BadInput):
        limits.check_combinations(200, 5)
    assert math.comb(200, 5) > limits.MAX_COMBINATIONS


def test_new_caps_exist():
    for name in ("n_subsamples", "n_test_folds", "wf_folds", "window_size", "rolling_window", "warmup_size", "test_size", "n_iter",
                 "asset_subset_size", "target_train_size", "runs", "mean_block"):
        assert name in limits.PARAM_CAPS
    assert limits.PARAM_CAPS["n_blocks"] <= 20
    with pytest.raises(BadInput):
        limits.check_param("n_subsamples", 10_000)


def test_resampling_budget():
    with pytest.raises(BadInput):
        limits.check_product("x", 100_000, 1_000)
    limits.check_product("x", 1_000, 1_000)


def test_validation_functions_reject_explosive_sizes():
    from hokma.quant.validation import bootstrap, cv, evidence, overfit
    returns = pd.Series(np.random.default_rng(1).normal(0, 0.01, 400))
    with pytest.raises(BadInput):
        cv.split(400, scheme="cpcv", n_folds=60, purge=0, embargo=0, n_test_folds=30)
    with pytest.raises(BadInput):
        evidence.bundle(pd.DataFrame(np.random.default_rng(2).normal(0, 0.01, (400, 3))), n_trials=3, n_folds=60, n_test_folds=30)
    with pytest.raises(BadInput):
        bootstrap.bootstrap_sharpe(pd.Series(np.zeros(1) + np.arange(600_000) * 1e-9), n_samples=100_000)
    with pytest.raises(BadInput):
        overfit.pbo(pd.DataFrame(np.random.default_rng(3).normal(0, 0.01, (400, 3))), n_blocks=40)
    with pytest.raises(BadInput):
        cv.oos_population(pd.DataFrame(np.random.default_rng(4).normal(0, 0.01, (400, 3))), n_folds=60, n_test_folds=30)
    assert returns.size == 400


def test_registry_call_rejects_combinations():
    @registry.register("validation.dummy_split", module="validation", summary="x", inputs={"n_folds": {}, "n_test_folds": {}})
    def dummy(**k):
        return QuantResult(name="validation.dummy_split", value=1.0, unit="u", method="m")

    with pytest.raises(BadInput):
        registry.call("validation.dummy_split", n_folds=200, n_test_folds=5)
    assert registry.call("validation.dummy_split", n_folds=10, n_test_folds=2).value == 1.0


def test_worker_runs_registered_functions_and_kills_on_timeout():
    from hokma.quant import runner

    from hokma.quant.metrics import ratios

    returns = pd.Series(np.random.default_rng(5).normal(0.001, 0.01, 300))
    value = runner.run("metrics.sharpe", {"returns": returns}, timeout=60)
    assert value.value == ratios.sharpe(returns=returns).value
    # a call that cannot finish in 0.01 seconds: the worker is killed, not left running
    slot_pids = []
    original = runner._Slot.__init__

    def spy(self):
        original(self)
        slot_pids.append(self.proc)

    runner.shutdown()
    runner._Slot.__init__ = spy
    try:
        with pytest.raises(BadInput):
            runner.run("metrics.sharpe", {"returns": returns}, timeout=0.01)
    finally:
        runner._Slot.__init__ = original
    assert slot_pids and all(p.poll() is not None for p in slot_pids), "the worker process is still alive"
    # the semaphore was released: a new call works
    assert runner.run("metrics.sharpe", {"returns": returns}, timeout=60).value is not None
    runner.shutdown()


def test_validators_normalize_types_first():
    assert limits.as_int("x", 20.0) == 20 and limits.as_int("x", np.int64(7)) == 7
    for bad in (20.5, "20", None, True, float("nan"), float("inf"), [1]):
        with pytest.raises(BadInput):
            limits.as_int("x", bad)
    limits.check_combinations(20.0, 2.0)
    with pytest.raises(BadInput):
        limits.check_combinations(20.5, 2)
    with pytest.raises(BadInput):
        limits.check_combinations(40.0, 20.0)
    with pytest.raises(BadInput):
        limits.check_param("n_samples", 20.5)
    with pytest.raises(BadInput):
        limits.check_param("horizon", 5_000.0)
    with pytest.raises(BadInput):
        limits.check_product("x", 1e9, 10)
    with pytest.raises(BadInput):
        limits.check_max_depth(2.5, 3)


def test_validation_entry_points_use_the_normalized_integers():
    from hokma.quant.validation import cv, evidence
    frame = pd.DataFrame(np.random.default_rng(7).normal(0, 0.01, (400, 3)))
    with pytest.raises(BadInput):
        cv.oos_population(frame, n_folds=20.5, n_test_folds=2)
    with pytest.raises(BadInput):
        evidence.bundle(frame, n_trials=3, n_folds=20.5)
    with pytest.raises(BadInput):
        evidence.bundle(frame, n_trials=3, n_folds=40.0, n_test_folds=20.0)
    with pytest.raises(BadInput):
        cv.split(400, scheme="cpcv", n_folds=20.5, purge=0, embargo=0)
    assert len(cv.split(400, scheme="cpcv", n_folds=10.0, purge=0, embargo=0, n_test_folds=2.0)) == 45
