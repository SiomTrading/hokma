"""V-30: an indicator is registered with its measured lag in bars; one without a lag is not registered."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators import catalog

from .helpers import tiny

PIECE = ("V-30",)


@pytest.fixture
def scratch_registry(monkeypatch):
    """A copy of the registry and of the lag table: what a test registers is gone when it ends."""
    monkeypatch.setattr(registry, "_FUNCTIONS", dict(registry._FUNCTIONS))
    monkeypatch.setattr(catalog, "LAGS", dict(catalog.LAGS))


def _mean(frame: pd.DataFrame, window: int) -> pd.Series:
    return frame["close"].rolling(window).mean()


def test_a_registered_indicator_appears_in_the_registry_with_its_lag(scratch_registry):
    catalog.register_indicator("scratch_mean", _mean, pieces=PIECE, needed=("close",), lag_bars=2, summary="Mean of the close.", defaults={"window": 5})
    names = [s.name for s in registry.list("indicators")]
    assert "indicators.scratch_mean" in names
    spec = registry.describe("indicators.scratch_mean")
    assert spec.summary == "Mean of the close. Lag: 2 bars." and spec.piece_ids == PIECE and spec.module == "indicators"
    assert catalog.lag_of("indicators.scratch_mean") == 2.0
    assert catalog.lag_of("indicators.sma") is None                     # an indicator registered another way has no declared lag
    res = registry.call("indicators.scratch_mean", bars=tiny(), window=5)
    assert res.value["latest"]["scratch_mean"] == pytest.approx((14 + 13 + 15 + 16 + 15) / 5)      # the last five closes of the fixed bars
    assert res.piece_ids == PIECE and res.params["window"] == 5


def test_one_lag_unit_is_one_bar_and_a_fraction_is_written_plainly(scratch_registry):
    catalog.register_indicator("scratch_one", _mean, pieces=PIECE, needed=("close",), lag_bars=1, summary="One.", defaults={"window": 3})
    catalog.register_indicator("scratch_half", _mean, pieces=PIECE, needed=("close",), lag_bars=0.5, summary="Half.", defaults={"window": 3})
    assert registry.describe("indicators.scratch_one").summary.endswith("Lag: 1 bar.")
    assert registry.describe("indicators.scratch_half").summary.endswith("Lag: 0.5 bars.")


def test_an_indicator_without_a_declared_lag_fails_to_register(scratch_registry):
    with pytest.raises(BadInput, match="lag_missing"):
        catalog.register_indicator("scratch_none", _mean, pieces=PIECE, needed=("close",), lag_bars=None, summary="No lag.", defaults={"window": 5})
    with pytest.raises(TypeError):                                      # the keyword is required: leaving it out is refused too
        catalog.register_indicator("scratch_omitted", _mean, pieces=PIECE, needed=("close",), summary="No lag.")       # type: ignore[call-arg]
    assert "indicators.scratch_none" not in {s.name for s in registry.list("indicators")}
    assert catalog.lag_of("indicators.scratch_none") is None


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, "2"])
def test_a_lag_that_is_not_a_number_of_bars_is_refused(scratch_registry, bad):
    with pytest.raises(BadInput, match="lag_invalid"):
        catalog.register_indicator("scratch_bad", _mean, pieces=PIECE, needed=("close",), lag_bars=bad, summary="Bad lag.", defaults={"window": 5})
    assert "indicators.scratch_bad" not in {s.name for s in registry.list("indicators")}


def test_a_name_is_not_registered_twice_and_must_be_plain(scratch_registry):
    catalog.register_indicator("scratch_dup", _mean, pieces=PIECE, needed=("close",), lag_bars=1, summary="Dup.", defaults={"window": 5})
    with pytest.raises(ValueError):
        catalog.register_indicator("scratch_dup", lambda frame, window: frame["close"], pieces=PIECE, needed=("close",), lag_bars=1, summary="Other.",
                                   defaults={"window": 5})
    with pytest.raises(BadInput):
        catalog.register_indicator("bad name!", _mean, pieces=PIECE, needed=("close",), lag_bars=1, summary="x")
    with pytest.raises(BadInput):
        catalog.register_indicator("scratch_out", _mean, pieces=PIECE, needed=("close",), lag_bars=1, summary="x", output="table")


def test_the_lag_is_measured_on_a_unit_ramp():
    assert catalog.measure_ramp_lag(_mean, window=5) == pytest.approx(2.0)          # (window - 1) / 2 for a simple average
    wma = lambda frame, window: frame["close"].rolling(window).apply(lambda v: np.dot(v, np.arange(1, window + 1)) / (window * (window + 1) / 2), raw=True)  # noqa: E731
    assert catalog.measure_ramp_lag(wma, window=4) == pytest.approx(1.0)            # (window - 1) / 3 for the weighted average
    two = lambda frame: pd.DataFrame({"a": frame["close"], "b": frame["close"].shift(3)})  # noqa: E731
    assert catalog.measure_ramp_lag(two, column="b") == pytest.approx(3.0)
    with pytest.raises(BadInput):
        catalog.measure_ramp_lag(two)
    with pytest.raises(BadInput):
        catalog.measure_ramp_lag(_mean, n=10, window=3)


def test_a_value_output_returns_one_result_and_bad_bars_give_no_value(scratch_registry):
    def spread(frame: pd.DataFrame, window: int) -> dict:
        tail = frame.iloc[-window:]
        return {"high": float(tail["high"].max()), "low": float(tail["low"].min())}

    catalog.register_indicator("scratch_range", spread, pieces=PIECE, needed=("high", "low"), lag_bars=0, output="value", summary="Range.", defaults={"window": 3})
    res = registry.call("indicators.scratch_range", bars=tiny())
    assert res.value == {"high": 17.0, "low": 13.0}                                                         # last three closes 15, 16, 15: highs close + 1, lows close - 2
    assert "last_n" not in registry.describe("indicators.scratch_range").inputs
    assert registry.call("indicators.scratch_range", bars=tiny().assign(low=-1.0)).value is None
    with pytest.raises(BadInput):
        registry.call("indicators.scratch_range", bars=tiny(), as_of="2024-01-10")                 # as_of exists only for closed-bar indicators


def test_a_closed_only_indicator_reads_only_closed_bars(scratch_registry):
    catalog.register_indicator("scratch_last", lambda frame: frame["close"], pieces=PIECE, needed=("close",), lag_bars=0, closed_only=True, summary="Close.")
    bars = tiny()
    open_day = bars.index[-1]
    res = registry.call("indicators.scratch_last", bars=bars, as_of=f"{open_day.date()}T12:00")
    assert res.value["latest"]["scratch_last"] == 16.0 and res.params["dropped_open_bars"] == 1            # the last bar is still open at noon
    assert registry.call("indicators.scratch_last", bars=bars, as_of=f"{open_day.date()}T16:00").value["latest"]["scratch_last"] == 15.0
