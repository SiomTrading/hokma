"""Evidence bundle for a desk judge: combinatorial purged CV, PBO, deflated Sharpe and walk forward (SPEC-017 row AH-67).

The bundle holds numbers and the leakage check. It never decides: each tribunal fixes its own thresholds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from hokma.core.contenthash import content_hash
from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult
from hokma.quant.validation import cv, overfit


@dataclass(frozen=True)
class EvidenceBundle:
    n_observations: int
    n_strategies: int
    n_trials: int
    best_strategy: str | None
    best_sharpe: float | None
    cpcv: dict
    walk_forward: dict
    pbo: float | None
    dsr: float | None
    sr0: float | None
    leakage_free: bool | None
    warnings: tuple[str, ...] = ()
    inputs_hash: str = ""
    parameters: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {k: getattr(self, k) for k in ("n_observations", "n_strategies", "n_trials", "best_strategy", "best_sharpe", "cpcv",
                                              "walk_forward", "pbo", "dsr", "sr0", "leakage_free", "inputs_hash", "parameters")}
        data["warnings"] = list(self.warnings)
        return data

    def judge_payload(self) -> dict:
        """What the judge receives: the evidence with a content hash, ready to cache or compare."""
        payload = self.to_dict()
        payload["content_hash"] = content_hash(payload)
        return payload


def _sharpe(values: np.ndarray) -> float:
    sd = values.std(axis=0, ddof=1)
    return values.mean(axis=0) / np.where(sd > 0, sd, np.nan)


def _selection_study(frame: pd.DataFrame, folds: list[cv.Fold]) -> dict:
    """In every fold pick the best in-sample strategy and record its out-of-sample Sharpe."""
    data = frame.to_numpy()
    oos = []
    for fold in folds:
        if len(fold.train) < 3 or len(fold.test) < 3:
            continue
        is_sr = _sharpe(data[fold.train])
        if not np.isfinite(is_sr).any():
            continue
        winner = int(np.nanargmax(is_sr))
        test_sr = _sharpe(data[fold.test][:, [winner]])[0]
        if math.isfinite(test_sr):
            oos.append(float(test_sr))
    if not oos:
        return {"n_splits": 0, "oos_sharpe_mean": None, "oos_sharpe_p05": None, "loss_rate": None}
    arr = np.array(oos)
    return {"n_splits": len(arr), "oos_sharpe_mean": float(arr.mean()), "oos_sharpe_p05": float(np.quantile(arr, 0.05)),
            "loss_rate": float((arr <= 0).mean())}


def bundle(matrix: pd.DataFrame, *, n_trials: int | None = None, n_blocks: int = 16, n_folds: int = 10, n_test_folds: int = 2,
           purge: int = 5, embargo: int = 2, wf_folds: int = 5) -> EvidenceBundle:
    """Build the evidence bundle from a returns matrix with one column per tried strategy."""
    if not isinstance(matrix, pd.DataFrame):
        raise BadInput("matrix must be a DataFrame with one column per strategy")
    warnings: list[str] = []
    frame = matrix.astype(float).replace([np.inf, -np.inf], np.nan)
    complete = frame.dropna(how="any")
    if len(complete) < len(frame):
        warnings.append(f"dropped {len(frame) - len(complete)} row(s) with NaN or infinite values")
    flat = [c for c in complete.columns if float(complete[c].max() - complete[c].min()) == 0.0] if len(complete) > 1 else list(complete.columns)
    if flat:
        warnings.append(f"dropped {len(flat)} constant strategy column(s)")
        complete = complete.drop(columns=flat)
    trials = int(n_trials) if n_trials is not None else int(matrix.shape[1])
    from hokma.quant import limits

    n_blocks, n_folds, n_test_folds, wf_folds = (limits.as_int(k, v) for k, v in (("n_blocks", n_blocks), ("n_folds", n_folds), ("n_test_folds", n_test_folds), ("wf_folds", wf_folds)))
    for _k, _v in (("n_blocks", n_blocks), ("n_folds", n_folds), ("n_test_folds", n_test_folds), ("wf_folds", wf_folds)):
        limits.check_param(_k, _v)
    limits.check_combinations(n_folds, n_test_folds)
    params = {"n_blocks": n_blocks, "n_folds": n_folds, "n_test_folds": n_test_folds, "purge": purge, "embargo": embargo, "wf_folds": wf_folds}
    minimum = max(n_blocks, n_folds * 2, 30)
    if complete.shape[1] < 2 or len(complete) < minimum or trials < 1:
        warnings.append(f"need at least 2 non constant strategies and {minimum} observations, got {complete.shape[1]} and {len(complete)}")
        return EvidenceBundle(len(complete), complete.shape[1], trials, None, None, {}, {}, None, None, None, None, tuple(warnings),
                              helpers.hash_inputs(complete), params)
    sharpes = _sharpe(complete.to_numpy())
    best = int(np.nanargmax(sharpes))
    sr_std = float(np.nanstd(sharpes, ddof=1))
    try:
        cpcv_folds = cv.split(len(complete), scheme="cpcv", n_folds=n_folds, purge=purge, embargo=embargo, n_test_folds=n_test_folds)
        wf = cv.split(len(complete), scheme="walk_forward", n_folds=wf_folds, purge=purge, embargo=embargo)
    except BadInput as exc:
        warnings.append(str(exc))
        return EvidenceBundle(len(complete), complete.shape[1], trials, None, None, {}, {}, None, None, None, None, tuple(warnings),
                              helpers.hash_inputs(complete), params)
    dsr_result = overfit.dsr(complete.iloc[:, best], trials, sr_std)
    pbo_result = overfit.pbo(complete, n_blocks)
    leak = cv.leakage_violations(cpcv_folds, purge=purge, embargo=embargo) + cv.leakage_violations(wf, purge=purge, embargo=embargo)
    warnings += [w for r in (dsr_result, pbo_result) for w in r.warnings]
    return EvidenceBundle(
        n_observations=len(complete), n_strategies=complete.shape[1], n_trials=trials, best_strategy=str(complete.columns[best]),
        best_sharpe=float(sharpes[best]), cpcv=_selection_study(complete, cpcv_folds), walk_forward=_selection_study(complete, wf),
        pbo=pbo_result.value, dsr=dsr_result.value, sr0=dsr_result.params.get("sr0"), leakage_free=not leak,
        warnings=tuple(warnings), inputs_hash=helpers.hash_inputs(complete, trials, n_blocks, n_folds, n_test_folds, purge, embargo, wf_folds),
        parameters=params,
    )


@registry.register(
    "validation.evidence", module="validation",
    summary="Evidence bundle for the desk judge from a returns matrix: CPCV selection study, PBO, DSR, walk forward and the leakage check.",
    inputs={"matrix": {"type": "dataframe", "required": True}, "n_trials": {"type": "int", "required": False, "default": None},
            "n_blocks": {"type": "int", "required": False, "default": 16}, "n_folds": {"type": "int", "required": False, "default": 10},
            "n_test_folds": {"type": "int", "required": False, "default": 2}, "purge": {"type": "int", "required": False, "default": 5},
            "embargo": {"type": "int", "required": False, "default": 2}, "wf_folds": {"type": "int", "required": False, "default": 5}},
    piece_ids=("AH-67",),
)
def evidence_result(matrix: pd.DataFrame, n_trials: int | None = None, n_blocks: int = 16, n_folds: int = 10, n_test_folds: int = 2,
                    purge: int = 5, embargo: int = 2, wf_folds: int = 5) -> QuantResult:
    name = "validation.evidence"
    b = bundle(matrix, n_trials=n_trials, n_blocks=n_blocks, n_folds=n_folds, n_test_folds=n_test_folds, purge=purge, embargo=embargo, wf_folds=wf_folds)
    if b.best_strategy is None:
        return QuantResult.empty(name, "evidence", "cpcv_pbo_dsr", b.warnings[-1])
    return QuantResult(name=name, value=b.judge_payload(), unit="evidence", method="cpcv_pbo_dsr", params=b.parameters, inputs_hash=b.inputs_hash,
                       warnings=b.warnings, piece_ids=("AH-67",))


def evidence(matrix: pd.DataFrame, *, n_trials: int) -> EvidenceBundle:
    """Public shortcut used by `hokma.quant.validation.evidence`."""
    return bundle(matrix, n_trials=n_trials)