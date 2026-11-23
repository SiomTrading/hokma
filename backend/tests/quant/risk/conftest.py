"""Fixtures of the risk module tests: fixed data, no network, registry restored after a reset."""
from __future__ import annotations

import importlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry

FIXTURES = Path(__file__).parent / "fixtures"
RISK_MODULES = (
    "hokma.quant.risk.moments", "hokma.quant.risk.dispersion", "hokma.quant.risk.distributions", "hokma.quant.risk.drawdown_risk",
    "hokma.quant.risk.var", "hokma.quant.risk.var_backtest", "hokma.quant.risk.copulas", "hokma.quant.risk.scenarios",
    "hokma.quant.risk.volatility", "hokma.quant.risk.liquidity", "hokma.quant.risk.sizing",
)


@pytest.fixture(autouse=True)
def _risk_registered():
    """Other test files reset the registry; reload the risk modules when that happened."""
    if not registry.list("risk"):
        for name in RISK_MODULES:
            importlib.reload(importlib.import_module(name))
    yield


@pytest.fixture(scope="session")
def panel() -> pd.DataFrame:
    """Five assets, 1,000 business days, fixed seed (see fixtures/returns_panel.csv)."""
    return pd.read_csv(FIXTURES / "returns_panel.csv", index_col=0, parse_dates=True)


@pytest.fixture(scope="session")
def ret(panel: pd.DataFrame) -> pd.Series:
    return panel["AAA"]


@pytest.fixture(scope="session")
def weights(panel: pd.DataFrame) -> dict[str, float]:
    return {col: 0.2 for col in panel.columns}


@pytest.fixture(scope="session")
def tiny() -> list[float]:
    """Five returns small enough to compute every measure by hand."""
    return [0.01, -0.02, 0.03, -0.01, 0.02]


@pytest.fixture(scope="session")
def ramp() -> pd.Series:
    """101 evenly spaced returns from -5% to +5%: every quantile is known exactly."""
    return pd.Series(np.round(np.linspace(-0.05, 0.05, 101), 10), index=pd.bdate_range("2021-01-04", periods=101))


def call(name: str, **kwargs):
    return registry.call(name, **kwargs)


def piece_ids(name: str) -> tuple[str, ...]:
    return registry.describe(name).piece_ids
