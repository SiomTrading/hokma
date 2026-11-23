"""Validation module (SPEC-017, section 4.4).

Public entry points: split, psr, dsr, pbo, evidence. Every other function is reached through `hokma.quant.registry`.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from hokma.quant import registry
from hokma.quant.types import QuantResult
from hokma.quant.validation import (  # noqa: F401  (importing registers the functions)
    bootstrap,
    covariance_eval,
    cv,
    evidence as _evidence,
    fidelity,
    overfit,
    residual_edge,
    stat_tests,
)
from hokma.quant.validation.cv import Fold
from hokma.quant.validation.evidence import EvidenceBundle


def split(index: Any, *, scheme: str = "cpcv", n_folds: int, purge: int, embargo: int, n_test_folds: int | None = None) -> list[Fold]:
    """Train and test partitions of an index with purge and embargo (schemes: cpcv, walk_forward, expanding)."""
    return cv.split(index, scheme=scheme, n_folds=n_folds, purge=purge, embargo=embargo, n_test_folds=n_test_folds)


def psr(returns: Any, *, sr_benchmark: float = 0.0) -> QuantResult:
    """Probabilistic Sharpe ratio with raw kurtosis, not annualized."""
    return overfit.psr(returns, sr_benchmark)


def dsr(returns: Any, *, n_trials: int, sr_std: float) -> QuantResult:
    """Deflated Sharpe ratio. sr_std is the per period standard deviation of the trial Sharpe ratios."""
    return overfit.dsr(returns, n_trials, sr_std)


def pbo(matrix: pd.DataFrame, *, n_blocks: int = 16) -> QuantResult:
    """Probability of backtest overfitting over a trials matrix."""
    return overfit.pbo(matrix, n_blocks)


def evidence(matrix: pd.DataFrame, *, n_trials: int) -> EvidenceBundle:
    """The evidence bundle a desk judge receives (AH-67)."""
    return _evidence.bundle(matrix, n_trials=n_trials)


evidence.bundle = _evidence.bundle  # `validation.evidence.bundle(...)` is the full entry point of the module

__all__ = ["Fold", "EvidenceBundle", "split", "psr", "dsr", "pbo", "evidence", "registry"]
