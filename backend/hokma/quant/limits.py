"""Size and time limits for every quant call that an agent can trigger (resource exhaustion guard).

`registry.call` applies them to all modules; functions that size their own work also check their parameter directly.
A value above a limit raises BadInput: it is never clipped silently.
"""
from __future__ import annotations

from typing import Any

from hokma.quant.errors import BadInput

PARAM_CAPS: dict[str, int] = {
    "n_samples": 100_000,
    "n_observations": 1_000_000,
    "horizon": 1_000,
    "n_simulations": 100_000,
    "n_blocks": 16,                  # the overfitting estimator tries C(n_blocks, n_blocks / 2) splits
    "n_folds": 1_000,
    "n_test_folds": 100,
    "n_subsamples": 1_000,
    "n_iter": 1_000,
    "asset_subset_size": 1_000,
    "target_train_size": 1_000_000,
    "target_n_test_paths": 1_000,
    "wf_folds": 1_000,
    "window_size": 1_000_000,
    "rolling_window": 1_000_000,
    "warmup_size": 1_000_000,
    "test_size": 1_000_000,
    "train_size": 1_000_000,
    "n_trials": 1_000_000,
    "n_boot": 100_000,
    "num_trials": 100_000,
    "runs": 100_000,
    "mean_block": 100_000,
    "window": 100_000,
    "lags": 1_000,
    "n_workers": 32,                 # processes of one backtest job (queue.py)
    "n_packages": 100_000,           # work packages of one job
    "n_instruments": 100,            # instruments of one multi instrument run
    "desk_steps": 1_000_000,         # steps of one desk loop (backtest runner)
    "n_combinations": 20_000,        # configurations of one search space (SPEC-020: grid, TPE and refine)
    "opt_trials": 5_000,             # proposals of one TPE or refine search
    "top_k": 1_000,                  # points of a fast sweep that are confirmed in the engine
    "refine_steps": 100,             # moves of the step by step refinement around the best point
    "max_lag": 1_000,                # longest lag of a test or an exponent (stats module)
    "weekly_window": 100_000,        # HAR-RV windows (stats module)
    "monthly_window": 100_000,
    "k_ar_diff": 10,                 # lagged differences of a VECM or a Johansen test (stats module)
    "nw_lags": 1_000,                # Newey-West lags (stats module)
    "hac_lags": 1_000,
    "max_breaks": 10,                # structural breaks searched (stats module)
    "n_steps": 5_000,                # binomial tree steps (options module)
    "time_steps": 1_000,             # simulation steps per path (options module)
    "n_strikes": 2_000,              # strike grid of a risk-neutral density (options module)
    "n_paths": 2_000,                # simulated paths of a Monte Carlo null (indicators module)
    "last_n": 1_000_000,             # rows returned by an indicator (indicators module)
    "lag": 10_000,                   # observations of a growth or change descriptor (fundamentals module)
    "skip": 10_000,                  # observations skipped by a momentum descriptor (fundamentals module)
    "min_periods": 100_000,          # observations before a descriptor or a profile gives a value (fundamentals module)
    "aggregation_period": 10_000,    # observations averaged by a beta descriptor (fundamentals module)
    # portfolio module (SPEC-019): sizing parameters of the optimizers, alpha evaluation and pre-selection
    "n_assets": 5_000,
    "zeros": 5_000,
    "k": 5_000,
    "cardinality": 5_000,
    "holding_period": 10_000,
    "signal_lag": 10_000,
    "evaluation_step": 10_000,
    "n_forward_periods": 1_000,
    "lookahead_days": 3_650,
    "min_count": 1_000_000,
}
MAX_COMBINATIONS = 10_000            # C(n_folds, n_test_folds): the number of test sets of a combinatorial split
MAX_RESAMPLED_VALUES = 50_000_000    # n_samples * length of the series in a resampling
MAX_INPUT_CELLS = 5_000_000          # rows x columns of any series or table passed in
MAX_SECONDS = 120.0                  # wall time of one registry call
MAX_DEPTH_NO_PANEL = 64
MAX_CONCURRENT = 2                   # quant calls running at the same time
MAX_OPEN_JOBS = 8                    # backtest jobs queued or running at the same time


def as_int(name: str, value: Any) -> int:
    """A whole number as int: an int, a numpy integer or a float with no fraction. Anything else is BadInput."""
    import math

    if isinstance(value, bool):
        raise BadInput(f"{name} must be a whole number, not a boolean")
    if isinstance(value, int):
        return value
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (TypeError, ValueError):
            raise BadInput(f"{name} must be a whole number") from None
        if isinstance(value, bool):
            raise BadInput(f"{name} must be a whole number, not a boolean")
        if isinstance(value, int):
            return value
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        return int(value)
    raise BadInput(f"{name} must be a whole number, got {value!r}")


def check_param(name: str, value: Any) -> None:
    """BadInput when a sizing parameter is not a whole number or is above its cap. None means "use the default"."""
    cap = PARAM_CAPS.get(name)
    if cap is None or value is None:
        return
    if as_int(name, value) > cap:
        raise BadInput(f"{name}={value} is above the limit {cap}")


def check_combinations(n: Any, k: Any, *, label: str = "n_folds") -> None:
    """BadInput when math.comb(n, k) is above MAX_COMBINATIONS, or when n or k is not a whole number."""
    import math

    n_i, k_i = as_int("n_folds", n), as_int("n_test_folds", k)
    if n_i < 0 or k_i < 0 or k_i > n_i:
        raise BadInput(f"{label}: need 0 <= n_test_folds <= n_folds, got {n_i} and {k_i}")
    if math.comb(n_i, k_i) > MAX_COMBINATIONS:
        raise BadInput(f"{label}: C({n_i}, {k_i}) = {math.comb(n_i, k_i)} combinations is above the limit {MAX_COMBINATIONS}")


def check_product(label: str, *sizes: Any) -> None:
    """BadInput when the product of the sizes is above MAX_RESAMPLED_VALUES."""
    total = 1
    for i, s in enumerate(sizes):
        total *= max(as_int(f"{label} factor {i + 1}", s), 1)
    if total > MAX_RESAMPLED_VALUES:
        raise BadInput(f"{label}: {total} values is above the limit {MAX_RESAMPLED_VALUES}")


def check_max_depth(value: Any, n_assets: int | None) -> None:
    if value is None:
        return
    cap = n_assets if n_assets is not None else MAX_DEPTH_NO_PANEL
    if as_int("max_depth", value) > cap:
        raise BadInput(f"max_depth={value} is above the limit {cap} (the number of assets)")


def _cells(value: Any) -> int:
    shape = getattr(value, "shape", None)
    if shape:
        total = 1
        for d in shape:
            total *= int(d)
        return total
    if isinstance(value, (list, tuple)):
        return sum(_cells(v) if not isinstance(v, (int, float, str)) else 1 for v in value)
    if isinstance(value, dict):
        return sum(_cells(v) for v in value.values())
    return 1


def check_inputs(kwargs: dict[str, Any]) -> None:
    """Apply the caps to the arguments of a call: parameter caps, input size and max_depth against the panel."""
    n_assets = None
    for key, value in kwargs.items():
        check_param(key, value)
        if _cells(value) > MAX_INPUT_CELLS:
            raise BadInput(f"{key} has more than {MAX_INPUT_CELLS} values")
        shape = getattr(value, "shape", None)
        if key == "panel" and shape and len(shape) == 2:
            n_assets = int(shape[1])
    if "max_depth" in kwargs:
        check_max_depth(kwargs["max_depth"], n_assets)
    if "n_folds" in kwargs and kwargs.get("scheme", "cpcv") in (None, "cpcv"):
        k = kwargs.get("n_test_folds")
        check_combinations(kwargs["n_folds"], 2 if k is None else k)
    if "n_samples" in kwargs:
        series = next((v for v in kwargs.values() if getattr(v, "shape", None)), None)
        if series is not None:
            check_product("n_samples x length", kwargs["n_samples"], int(series.shape[0]))
