"""AH-16: blend_signals mixes the views of a desk, leaves abstentions out and merges profiles that say the same thing."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from hokma.desks.core.signal import Criterion, Signal
from hokma.quant.errors import BadInput

from .conftest import call


def _sig(source: str, value: float | None, confidence: int, symbol: str = "SYM1") -> dict:
    return {"source": source, "symbol": symbol, "value": value, "confidence": confidence, "abstained": value is None}


SIGNALS = [_sig("A16", 0.6, 80), _sig("A17", 0.4, 60), _sig("A18", -0.2, 50), _sig("A19", None, 0)]


def test_ah16_without_history_each_profile_counts_and_the_abstention_is_excluded():
    result = call("portfolio.blend_signals", signals=SIGNALS)
    # weights 0.8, 0.6, 0.5: (0.8*0.6 + 0.6*0.4 + 0.5*(-0.2)) / 1.9 = 0.62 / 1.9
    assert result.value["value"] == pytest.approx(0.62 / 1.9, abs=1e-12)
    assert result.value["n_used"] == 3 and result.value["abstained"] == ["A19"]
    assert any("excluded abstentions" in w for w in result.warnings)
    assert result.piece_ids == ("AH-16",)


def test_ah16_two_profiles_above_point_eight_count_as_one_and_the_result_says_so(history):
    result = call("portfolio.blend_signals", signals=SIGNALS, history=history)
    # A16 and A17 merge: value (0.8*0.6 + 0.6*0.4) / 1.4 = 0.72 / 1.4, weight (0.8 + 0.6) / 2 = 0.7; A18 weight 0.5
    # blend = (0.7 * 0.72 / 1.4 + 0.5 * (-0.2)) / 1.2 = 0.26 / 1.2
    assert result.value["value"] == pytest.approx(0.26 / 1.2, abs=1e-12)
    assert result.value["n_groups"] == 2
    merged = [g for g in result.value["groups"] if len(g["sources"]) > 1][0]
    assert merged["sources"] == ["A16", "A17"] and merged["weight"] == pytest.approx(0.7)
    assert any("merged profiles" in w and "A16" in w and "A17" in w for w in result.warnings)


def test_ah16_the_merge_follows_the_correlation_of_the_history(history):
    matrix = history.assign(value=pd.to_numeric(history["value"])).pivot_table(index=["symbol", "as_of"], columns="source", values="value")
    corr = matrix.corr()
    assert corr.loc["A16", "A17"] > 0.8 and abs(corr.loc["A16", "A18"]) < 0.8
    # a threshold above the observed correlation leaves them apart
    apart = call("portfolio.blend_signals", signals=SIGNALS, history=history, correlation_threshold=0.999)
    assert apart.value["n_groups"] == 3
    assert apart.value["value"] == pytest.approx(0.62 / 1.9, abs=1e-12)


def test_ah16_weights_per_source_change_the_merged_group(history):
    result = call("portfolio.blend_signals", signals=SIGNALS, history=history, weights={"A16": 0.0})
    # A16 weight 0, A17 weight 0.6: group value 0.4, group weight 0.3; A18 weight 0.5: (0.3 * 0.4 + 0.5 * (-0.2)) / 0.8
    assert result.value["value"] == pytest.approx(0.02 / 0.8, abs=1e-12)


def test_ah16_accepts_real_signal_objects():
    at = datetime(2024, 5, 1, tzinfo=timezone.utc)
    crit = (Criterion("roe_10y_min", 0.2, 0.15, True, "fundamentals.criteria"),)

    def make(source: str, value: float, confidence: int) -> Signal:
        return Signal(source=source, desk_id=1, line="SWO", symbol="SYM1", as_of=at, horizon="months", value=value, confidence=confidence,
                      abstained=False, criteria=crit, data_missing=(), reason="The company earns high returns. Its debt is low.", snapshot_hash="h")

    gone = Signal.abstain("A19", 1, "SWO", "SYM1", at, "months", ("filings",), "No filings arrived. The view is skipped.", "h")
    result = call("portfolio.blend_signals", signals=[make("A16", 0.5, 100), make("A17", -0.5, 50), gone])
    assert result.value["value"] == pytest.approx((1.0 * 0.5 + 0.5 * -0.5) / 1.5, abs=1e-12)
    assert result.value["abstained"] == ["A19"]


def test_ah16_empty_nan_and_out_of_domain_give_none_with_the_reason():
    assert call("portfolio.blend_signals", signals=[]).value is None
    all_out = call("portfolio.blend_signals", signals=[_sig("A19", None, 0), _sig("A20", None, 0)])
    assert all_out.value is None and "abstained" in all_out.warnings[0]
    nan = call("portfolio.blend_signals", signals=[{**_sig("A16", 0.1, 50), "value": float("nan")}])
    assert nan.value is None and "finite" in nan.warnings[0]
    assert call("portfolio.blend_signals", signals=[_sig("A16", 1.5, 50)]).value is None
    assert call("portfolio.blend_signals", signals=[_sig("A16", 0.5, 150)]).value is None
    assert call("portfolio.blend_signals", signals=[_sig("A16", 0.5, 0)]).value is None            # zero weight everywhere
    assert call("portfolio.blend_signals", signals=[_sig("A16", 0.5, 50), _sig("A16", 0.1, 50)]).value is None
    assert call("portfolio.blend_signals", signals=[_sig("A16", 0.5, 50, "X"), _sig("A17", 0.1, 50, "Y")]).value is None
    assert call("portfolio.blend_signals", signals=SIGNALS, correlation_threshold=2.0).value is None


def test_ah16_wrong_type_and_shape_raise_bad_input(history):
    with pytest.raises(BadInput):
        call("portfolio.blend_signals", signals="A16")
    with pytest.raises(BadInput):
        call("portfolio.blend_signals", signals=[42])
    with pytest.raises(BadInput):
        call("portfolio.blend_signals", signals=SIGNALS, weights={"nobody": 1.0})
    with pytest.raises(BadInput):
        call("portfolio.blend_signals", signals=SIGNALS, history=history[["source", "value"]])
    with pytest.raises(BadInput):
        call("portfolio.blend_signals", signals=SIGNALS, history=[1, 2])


def test_ah16_a_blend_stays_inside_the_range():
    result = call("portfolio.blend_signals", signals=[_sig("A16", 1.0, 100), _sig("A17", 1.0, 100)])
    assert result.value["value"] == pytest.approx(1.0) and result.value["confidence"] == pytest.approx(100.0)
    low = call("portfolio.blend_signals", signals=[_sig("A16", -1.0, 40)])
    assert low.value["value"] == pytest.approx(-1.0)
    assert not np.isnan(low.value["confidence"])
