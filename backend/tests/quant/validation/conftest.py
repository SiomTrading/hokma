"""Shared fixtures for the validation tests: fixed data only, no network."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _load_validation():
    import hokma.quant.validation  # noqa: F401  (registers every function)


@pytest.fixture(scope="session")
def returns() -> pd.Series:
    frame = pd.read_csv(FIXTURES / "returns_daily.csv", parse_dates=["date"], index_col="date")
    return frame["return"]


@pytest.fixture(scope="session")
def benchmark() -> pd.Series:
    frame = pd.read_csv(FIXTURES / "benchmark_daily.csv", parse_dates=["date"], index_col="date")
    return frame["return"]


@pytest.fixture(scope="session")
def asset_matrix() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    index = pd.bdate_range("2020-01-01", periods=900)
    return pd.DataFrame(rng.normal(0.0004, 0.01, (900, 4)), index=index, columns=list("abcd"))


def assert_registered(name: str, piece_id: str) -> None:
    spec = registry.describe(name)
    assert not spec.blocked
    assert piece_id in spec.piece_ids
    assert name in [s.name for s in registry.list("validation")]
