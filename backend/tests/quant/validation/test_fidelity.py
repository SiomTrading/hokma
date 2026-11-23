"""Fidelity of profiles against 13F positions: INV-008 (fixture data, design only)."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.validation import fidelity as fd

from .conftest import assert_registered

QUARTERS = [f"{y}Q{q}" for y in range(2015, 2025) for q in range(1, 5)]
TICKERS = [f"T{i:02d}" for i in range(30)]
NEVER_HELD = [f"C{i:02d}" for i in range(12)]


def _holdings(seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    shares = {t: 1000.0 for t in TICKERS}
    rows = []
    for q in QUARTERS:
        for t in TICKERS:
            shares[t] = max(1.0, shares[t] * float(np.exp(rng.normal(0, 0.6))))
            rows.append({"quarter": q, "ticker": t, "shares": shares[t]})
    return pd.DataFrame(rows)


def _signals(holdings: pd.DataFrame, accuracy: float, seed: int = 2, informed_selection: bool = True) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    labels = fd.label_changes(holdings, 0.25).set_index(["quarter", "ticker"])["label"]
    rows = []
    for q in QUARTERS[1:]:
        for t in TICKERS:
            label = labels.get((q, t), "HOLD")
            truth = {"INCREASE": 1.0, "DECREASE": -1.0, "HOLD": 0.3}[label]
            right = rng.random() < accuracy
            sign = truth if right else -truth
            conf = 85 if right else 25
            base = 0.4 if informed_selection else rng.normal(0, 0.5)
            rows.append({"quarter": q, "ticker": t, "signal": base + 0.5 * sign, "confidence": conf, "abstained": False})
        for t in NEVER_HELD:
            rows.append({"quarter": q, "ticker": t, "signal": (-0.4 if informed_selection else rng.normal(0, 0.5)) + rng.normal(0, 0.05), "confidence": 50,
                         "abstained": False})
    return pd.DataFrame(rows)


def test_auc_and_wilson_known_values_INV_008():
    assert fd.auc(np.array([0.9, 0.8, 0.3, 0.2]), np.array([True, True, False, False])) == 1.0
    assert fd.auc(np.array([0.2, 0.3, 0.8, 0.9]), np.array([True, True, False, False])) == 0.0
    assert fd.auc(np.array([0.5, 0.5, 0.5, 0.5]), np.array([True, True, False, False])) == 0.5
    assert fd.auc(np.array([1.0, 2.0]), np.array([True, True])) is None
    assert fd.wilson_lower(9, 10) == pytest.approx(0.6522813326641418, abs=1e-12)
    assert fd.wilson_lower(0, 0) == 0.0


def test_change_labels_use_the_25_percent_threshold_INV_008():
    holdings = pd.DataFrame({"quarter": ["Q1", "Q1", "Q1", "Q1", "Q2", "Q2", "Q2", "Q2"], "ticker": ["a", "b", "c", "d"] * 2,
                             "shares": [100, 100, 100, 0, 130, 80, 110, 50]})
    labels = fd.label_changes(holdings, 0.25).set_index("ticker")["label"].to_dict()
    assert labels == {"a": "INCREASE", "b": "HOLD" if False else "HOLD", "c": "HOLD", "d": "INCREASE"} or labels["b"] == "DECREASE" or labels["b"] == "HOLD"
    assert labels["a"] == "INCREASE" and labels["c"] == "HOLD" and labels["d"] == "INCREASE" and labels["b"] == "HOLD"


def test_informed_profile_is_not_rejected_and_never_confirmed_INV_008():
    holdings = _holdings()
    report = fd.fidelity_score(_signals(holdings, accuracy=0.85), holdings)
    assert report.status == fd.NOT_REJECTED and report.can_confirm is False
    assert report.n_events >= 150 and report.hypotheses["H1"]["passed"] and report.hypotheses["H2"]["passed"] and report.hypotheses["H4"]["passed"]
    assert report.hypotheses["H3"]["passed"] is None
    assert 0.0 <= report.score <= 1.0
    assert any("Phase B" in n for n in report.notes)


def test_uninformed_profile_is_rejected_INV_008():
    holdings = _holdings()
    report = fd.fidelity_score(_signals(holdings, accuracy=0.5, informed_selection=False), holdings)
    assert report.status == fd.REJECTED
    assert report.hypotheses["H2"]["passed"] is False or report.hypotheses["H1"]["passed"] is False


def test_too_few_events_is_inconclusive_not_failed_INV_008():
    holdings = _holdings()
    few = holdings[holdings["quarter"].isin(QUARTERS[:6])]
    signals = _signals(holdings, accuracy=0.85)
    report = fd.fidelity_score(signals[signals["quarter"].isin(QUARTERS[:6])], few)
    assert report.status == fd.INCONCLUSIVE and report.n_events < 150


def test_h3_specificity_uses_the_cross_matrix_INV_008():
    holdings = _holdings()
    signals = _signals(holdings, accuracy=0.85)
    passed = fd.fidelity_score(signals, holdings, manager="m1", cross_auc={"m1": {"m1": 0.80, "m2": 0.60, "m3": 0.62}}).hypotheses["H3"]
    failed = fd.fidelity_score(signals, holdings, manager="m1", cross_auc={"m1": {"m1": 0.66, "m2": 0.64}}).hypotheses["H3"]
    assert passed["passed"] is True and failed["passed"] is False


def test_abstentions_lower_coverage_INV_008():
    holdings = _holdings()
    signals = _signals(holdings, accuracy=0.85)
    signals.loc[signals.index % 2 == 0, "abstained"] = True
    report = fd.fidelity_score(signals, holdings)
    assert report.coverage == pytest.approx(0.5, abs=0.01)
    assert any("partial" in n for n in report.notes)


def test_without_13f_the_answer_is_data_unavailable_INV_008():
    signals = _signals(_holdings(), accuracy=0.8)
    assert fd.fidelity_score(signals, None).status == "DATA_UNAVAILABLE"
    assert fd.fidelity_score(signals, pd.DataFrame(columns=["quarter", "ticker", "shares"])).status == "DATA_UNAVAILABLE"
    result = registry.call("validation.fidelity_13f", signals=signals)
    assert result.value == {"status": "DATA_UNAVAILABLE"} and result.warnings
    assert fd.fidelity_score(pd.DataFrame(columns=list(fd.SIGNAL_COLUMNS)), _holdings()).status == "DATA_UNAVAILABLE"


def test_registered_function_and_bad_inputs_INV_008():
    holdings = _holdings()
    result = registry.call("validation.fidelity_13f", signals=_signals(holdings, 0.85), holdings=holdings)
    assert result.value["status"] == fd.NOT_REJECTED and result.value["can_confirm"] is False
    assert_registered("validation.fidelity_13f", "INV-008")
    with pytest.raises(BadInput):
        registry.call("validation.fidelity_13f", signals=pd.DataFrame({"quarter": ["a"]}), holdings=holdings)
    with pytest.raises(BadInput):
        registry.call("validation.fidelity_13f", signals=[1, 2], holdings=holdings)
