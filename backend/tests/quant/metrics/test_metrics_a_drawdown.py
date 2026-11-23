import importlib
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.metrics import _lib

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name: str) -> pd.Series:
    return pd.read_csv(FIXTURES / name, index_col=0, parse_dates=True)["return"]


R = _load("returns_daily.csv")
B = _load("benchmark_daily.csv")
MODULES = ("returns", "volatility", "drawdown", "distribution", "ratios", "locks", "accessor")
BAD_INPUTS = {
    "empty": [],
    "constant": [0.01] * 30,
    "nan": [0.01, -0.02, float("nan"), 0.03, -0.01, 0.02, 0.005, -0.004, 0.01, 0.02],
}


@pytest.fixture(autouse=True)
def registered():
    """Other test files reset the registry; load the modules again when that happened."""
    loaded = [importlib.import_module(f"hokma.quant.metrics.{name}") for name in MODULES]   # a first import registers
    if "metrics.sharpe" not in {s.name for s in registry.list("metrics")}:
        for module in loaded:
            importlib.reload(module)
    yield


def spec_of(name: str):
    return registry.describe(name)


def check_registered(name: str, piece_id: str) -> None:
    assert name in {s.name for s in registry.list("metrics")}
    assert piece_id in registry.describe(name).piece_ids


def as_array(value) -> np.ndarray:
    """Numbers of a scalar, list or date-keyed dict result, in order."""
    if isinstance(value, dict):
        return np.array([as_array(v).ravel()[0] if isinstance(v, (dict, list)) else v for v in value.values()], dtype=float)
    return np.atleast_1d(np.asarray(value, dtype=float))


def lib_series(series) -> np.ndarray:
    return np.asarray(series.dropna(), dtype=float)


def check_degenerate(fn, with_benchmark: bool = False, **kwargs) -> None:
    for label, bad in BAD_INPUTS.items():
        res = fn(bad, B.iloc[: len(bad)] if with_benchmark and bad else B, **kwargs) if with_benchmark else fn(bad, **kwargs)
        assert res.value is None, label
        assert res.warnings, label
        res = fn(R, bad, **kwargs) if with_benchmark else None
        if res is not None:
            assert res.value is None and res.warnings, label

MODULE = importlib.import_module("hokma.quant.metrics.drawdown")
PX = 100.0 * (1.0 + R).cumprod()
N = len(R)


def _q(fn, *args, **kwargs):
    with _lib.quiet():
        return fn(*args, **kwargs)


def _dd(x: pd.Series) -> pd.Series:
    wealth = (1.0 + x).cumprod()
    return wealth / np.maximum(wealth.cummax(), 1.0) - 1.0


def _episodes(dd: pd.Series) -> int:
    """Number of runs of consecutive dates below the previous peak."""
    below = (dd < 0).to_numpy()
    return int(below[0]) + int(np.sum(below[1:] & ~below[:-1]))


def _frame_values(frame: pd.DataFrame, column: str) -> list[float]:
    return [float(v) for v in frame[column]]


def test_max_drawdown_QS_030_and_FF_005():
    for row in ("QS-030", "FF-005"):
        check_registered("metrics.max_drawdown", row)
    res = MODULE.max_drawdown(R)
    assert res.value == pytest.approx(float(_dd(R).min()), rel=1e-9), "hand value"
    assert res.value == pytest.approx(_q(_lib.qstats.max_drawdown, R), rel=1e-9)
    assert res.value == pytest.approx(_q(_lib.ffn.calc_max_drawdown, _lib.ffn.to_price_index(R)), rel=1e-9)
    assert res.value == pytest.approx(-0.26433655595568406, rel=1e-9)


def test_drawdown_series_QS_030():
    check_registered("metrics.drawdown_series", "QS-030")
    res = MODULE.drawdown_series(R)
    assert as_array(res.value) == pytest.approx(_dd(R).to_numpy(), rel=1e-9, abs=1e-12)
    assert as_array(res.value) == pytest.approx(_q(_lib.qstats.to_drawdown_series, R).to_numpy(), rel=1e-9, abs=1e-12)
    assert list(res.value)[0] == "2021-01-04" and res.value["2021-01-04"] == pytest.approx(float(R.iloc[0]), rel=1e-9)


def test_drawdown_details_QS_036():
    check_registered("metrics.drawdown_details", "QS-036")
    res = MODULE.drawdown_details(R)
    assert res.ok, res.warnings
    lib = _q(_lib.qstats.drawdown_details, _q(_lib.qstats.to_drawdown_series, R))
    assert len(res.value) == len(lib) == _episodes(_dd(R))
    assert [e["max drawdown"] for e in res.value] == pytest.approx(_frame_values(lib, "max drawdown"), rel=1e-9)
    assert min(e["max drawdown"] for e in res.value) == pytest.approx(float(_dd(R).min()) * 100.0, rel=1e-9), "depth is in percent"
    deepest = min(res.value, key=lambda e: e["max drawdown"])
    assert deepest["start"] == "2021-01-08" and deepest["valley"] <= deepest["end"] and deepest["days"] > 900


def test_drawdown_series_from_prices_FF_005():
    check_registered("metrics.drawdown_series_from_prices", "FF-005")
    res = MODULE.drawdown_series_from_prices(PX)
    hand = (PX / PX.cummax() - 1.0).to_numpy()
    assert as_array(res.value) == pytest.approx(hand, rel=1e-9, abs=1e-12)
    assert as_array(res.value) == pytest.approx(_q(_lib.ffn.to_drawdown_series, PX).to_numpy(), rel=1e-9, abs=1e-12)


def test_drawdown_details_from_prices_FF_005():
    check_registered("metrics.drawdown_details_from_prices", "FF-005")
    res = MODULE.drawdown_details_from_prices(PX)
    assert res.ok, res.warnings
    lib = _q(_lib.ffn.drawdown_details, _q(_lib.ffn.to_drawdown_series, PX))
    assert len(res.value) == len(lib) == _episodes(PX / PX.cummax() - 1.0)
    assert [e["drawdown"] for e in res.value] == pytest.approx(_frame_values(lib, "drawdown"), rel=1e-9)
    assert min(e["drawdown"] for e in res.value) == pytest.approx(float((PX / PX.cummax() - 1.0).min()), rel=1e-9)


def test_max_drawdown_from_prices_FF_005():
    check_registered("metrics.max_drawdown_from_prices", "FF-005")
    res = MODULE.max_drawdown_from_prices(PX)
    assert res.value == pytest.approx(float((PX / PX.cummax() - 1.0).min()), rel=1e-9)
    assert res.value == pytest.approx(_q(_lib.ffn.calc_max_drawdown, PX), rel=1e-9)


def test_known_drawdown_by_hand():
    x = pd.Series([0.10, -0.50, 0.20, 0.0], index=pd.bdate_range("2024-01-01", periods=4))
    assert MODULE.max_drawdown(x).value == pytest.approx(-0.5, rel=1e-12)
    assert as_array(MODULE.drawdown_series(x).value) == pytest.approx([0.0, -0.5, -0.4, -0.4], rel=1e-12, abs=1e-15)


@pytest.mark.parametrize("name", ["max_drawdown", "drawdown_series", "drawdown_details", "drawdown_series_from_prices",
                                  "drawdown_details_from_prices", "max_drawdown_from_prices"])
def test_empty_constant_and_nan_inputs_return_none_with_warnings(name):
    check_degenerate(getattr(MODULE, name))
