"""Fixtures of the indicators tests: fixed data, no network, registry restored after a reset."""
from __future__ import annotations

import importlib
from pathlib import Path

import pandas as pd
import pytest

from hokma.quant import registry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MODULES = (
    "hokma.quant.indicators.breadth", "hokma.quant.indicators.momentum", "hokma.quant.indicators.overlap",
    "hokma.quant.indicators.volatility", "hokma.quant.indicators.signal_processing", "hokma.quant.indicators.catalog",
    "hokma.quant.indicators.screens.momentum_burst", "hokma.quant.indicators.screens.weekly_structure",
    "hokma.quant.indicators.screens.vcp", "hokma.quant.indicators.screens.distribution_days", "hokma.quant.indicators.screens.pead",
    "hokma.quant.indicators.screens.earnings_reaction", "hokma.quant.indicators.screens.episodic_pivot",
    "hokma.quant.indicators.patterns", "hokma.quant.indicators.profile", "hokma.quant.indicators.zigzag", "hokma.quant.indicators.td_sequential",
)


@pytest.fixture(autouse=True)
def _indicators_registered(monkeypatch):
    """Other test files reset the registry: reload the indicator modules when that happened. Calls run in this process."""
    if not registry.list("indicators"):
        for name in MODULES:
            importlib.reload(importlib.import_module(name))
    monkeypatch.setattr(registry, "IN_PROCESS", True)
    yield


@pytest.fixture(scope="session")
def spy() -> pd.DataFrame:
    """1,000 fixed daily bars (see fixtures/ohlcv_daily_spy.csv)."""
    return pd.read_csv(FIXTURES / "ohlcv_daily_spy.csv", index_col=0, parse_dates=True)


@pytest.fixture(scope="session")
def spy_weekly(spy: pd.DataFrame) -> pd.DataFrame:
    """The same bars as weekly bars labelled by their Friday."""
    from hokma.quant.indicators._common import to_weekly

    return to_weekly(spy)
