"""Fixtures of the fundamentals tests: fixed data, no network, registry restored after a reset."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AS_OF = "2024-06-30"
MODULES = ("formulas", "ratios", "scores", "dupont", "cost_of_capital", "value", "facts", "panel", "scalers", "descriptors", "criteria")


@pytest.fixture(autouse=True)
def _fundamentals_registered(monkeypatch):
    """Other test files reset the registry; reload the module when that happened. Calls run in this process (the worker path has its own test)."""
    importlib.import_module("hokma.quant.fundamentals")            # registers the functions the first time
    if not registry.list("fundamentals"):
        for name in MODULES:
            importlib.reload(importlib.import_module(f"hokma.quant.fundamentals.{name}"))
    monkeypatch.setattr(registry, "IN_PROCESS", True)
    yield


@pytest.fixture(scope="session")
def pit() -> dict:
    return json.loads((FIXTURES / "statements_pit.json").read_text(encoding="utf-8"))


def frame(pit: dict, company: str) -> pd.DataFrame:
    df = pd.DataFrame(pit["companies"][company]["rows"]).set_index("period_end")
    numeric = [c for c in df.columns if c != "filed"]
    df[numeric] = df[numeric].astype(float)
    return df


@pytest.fixture()
def alfa(pit) -> pd.DataFrame:
    return frame(pit, "ALFA")


@pytest.fixture()
def bravo(pit) -> pd.DataFrame:
    return frame(pit, "BRAVO")


@pytest.fixture()
def charlie(pit) -> pd.DataFrame:
    return frame(pit, "CHARLIE")


@pytest.fixture(scope="session")
def quarterly(pit) -> pd.DataFrame:
    return pd.DataFrame(pit["quarterly_revenue_ALFA"]["facts"])


@pytest.fixture(scope="session")
def returns_panel() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "returns_panel.csv", index_col=0, parse_dates=True)


@pytest.fixture(scope="session")
def candles() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "ohlcv_daily_spy.csv", index_col=0, parse_dates=True)


def call(name: str, **kwargs):
    return registry.call(name, **kwargs)


def piece_ids(name: str) -> tuple[str, ...]:
    return registry.describe(name).piece_ids


def latest(result, key: str):
    return result.value["latest"][key]


def with_last(df: pd.DataFrame, **changes) -> pd.DataFrame:
    """A copy of ALFA style statements with some columns of the last period that sits inside the cut-off changed."""
    out = df.copy()
    row = out[out["filed"] <= AS_OF].index[-1]
    for k, v in changes.items():
        out.loc[row, k] = v
    return out


@pytest.fixture(scope="session")
def rng() -> np.random.Generator:
    return np.random.default_rng(7)
