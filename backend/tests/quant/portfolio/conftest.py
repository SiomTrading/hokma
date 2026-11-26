"""Fixtures of the portfolio module tests: fixed data, no network, registry restored after a reset, calls in this process."""
from __future__ import annotations

import importlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry

SHARED = Path(__file__).resolve().parents[1] / "fixtures"
PORTFOLIO_MODULES = ("container", "optimize", "priors", "factors", "uncertainty", "preselect", "alpha", "weights", "ic", "blend", "forecasts", "overlay")


@pytest.fixture(autouse=True)
def _portfolio_registered(monkeypatch):
    """Other test files reset the registry: reload the portfolio modules when that happened. Calls run in the test process."""
    importlib.import_module("hokma.quant.portfolio")           # registers the functions the first time
    if not registry.list("portfolio"):
        for name in PORTFOLIO_MODULES:
            importlib.reload(importlib.import_module(f"hokma.quant.portfolio.{name}"))
    monkeypatch.setattr(registry, "IN_PROCESS", True)
    yield


def call(name: str, **kwargs):
    return registry.call(name, **kwargs)


@pytest.fixture(scope="session")
def panel() -> pd.DataFrame:
    """Five assets, 1,000 business days, fixed seed (fixtures/returns_panel.csv of H1)."""
    return pd.read_csv(SHARED / "returns_panel.csv", index_col=0, parse_dates=True)


@pytest.fixture(scope="session")
def history() -> pd.DataFrame:
    """Past views of four profiles on three symbols (fixtures/signals_history.csv, shaped like desk_signals)."""
    return pd.read_csv(SHARED / "signals_history.csv")


@pytest.fixture()
def orthogonal() -> pd.DataFrame:
    """Two assets with zero mean and zero covariance: variances 1e-4 and 4e-4 (population), so every risk figure is known by hand."""
    a = np.array([1, -1, 1, -1, 1, -1, 1, -1]) * 0.01
    b = np.array([1, 1, -1, -1, 1, 1, -1, -1]) * 0.02
    return pd.DataFrame({"a": a, "b": b}, index=pd.bdate_range("2024-01-01", periods=8))
