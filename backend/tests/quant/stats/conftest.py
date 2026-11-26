"""Fixtures of the stats module tests: fixed data, no network, registry restored after a reset, calls in this process."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry

FIXTURES = Path(__file__).parent / "fixtures"
SHARED = Path(__file__).parents[1] / "fixtures"
STATS_MODULES = (
    "hokma.quant.stats.volatility", "hokma.quant.stats.hurst", "hokma.quant.stats.cointegration", "hokma.quant.stats.timeseries",
    "hokma.quant.stats.kalman", "hokma.quant.stats.regression", "hokma.quant.stats.causal", "hokma.quant.stats.events",
    "hokma.quant.stats.sentiment", "hokma.quant.stats.carry",
)


@pytest.fixture(autouse=True)
def _stats_registered(monkeypatch):
    """Other test files reset the registry: reload the stats modules when that happened. Calls run in the test process."""
    if not registry.list("stats"):
        modules = [importlib.import_module(name) for name in STATS_MODULES]     # a first import registers the functions
        if not registry.list("stats"):                                          # already imported and then reset: run them again
            for module in modules:
                importlib.reload(module)
    monkeypatch.setattr(registry, "IN_PROCESS", True)
    yield


@pytest.fixture(scope="session")
def ohlcv() -> pd.DataFrame:
    """1,000 daily bars with fixed seed (fixtures/ohlcv_daily_spy.csv)."""
    return pd.read_csv(FIXTURES / "ohlcv_daily_spy.csv", index_col=0, parse_dates=True)


@pytest.fixture(scope="session")
def closes(ohlcv: pd.DataFrame) -> pd.Series:
    return ohlcv["close"]


@pytest.fixture(scope="session")
def daily_returns() -> pd.Series:
    """The returns fixture of H1 (756 business days)."""
    return pd.read_csv(SHARED / "returns_daily.csv", index_col=0, parse_dates=True)["return"]


@pytest.fixture(scope="session")
def earnings_events() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "earnings_events.csv")


@pytest.fixture(scope="session")
def option_chain() -> dict:
    return json.loads((FIXTURES / "option_chain_spy.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def mentions() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "mentions_daily.csv")


@pytest.fixture(scope="session")
def toy_bars() -> pd.DataFrame:
    """Ten bars (two weeks) small enough to compute every range estimator by hand."""
    idx = pd.bdate_range("2024-01-01", periods=10)
    o = pd.Series([100, 101, 100.5, 102, 101.5, 103, 102, 101, 102.5, 103.5], index=idx)
    cl = pd.Series([100.6, 100.2, 101.8, 101.1, 102.6, 102.2, 101.2, 102.4, 103.3, 102.9], index=idx)
    top, bot = pd.concat([o, cl], axis=1).max(axis=1), pd.concat([o, cl], axis=1).min(axis=1)
    h = top + pd.Series([1.0, 1.2, 0.8, 1.5, 1.0, 1.1, 0.9, 1.3, 1.0, 0.7], index=idx)
    low = bot - pd.Series([0.9, 0.7, 1.1, 0.6, 1.2, 0.8, 1.0, 0.5, 0.9, 1.2], index=idx)
    return pd.DataFrame({"open": o, "high": h, "low": low, "close": cl})


def call(name: str, **kwargs):
    return registry.call(name, **kwargs)


def piece_ids(name: str) -> tuple[str, ...]:
    return registry.describe(name).piece_ids


def rng(seed: int = 7) -> np.random.Generator:
    return np.random.default_rng(seed)
