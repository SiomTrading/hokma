"""Fidelity test of investor profiles against 13F positions (SPEC-017 row INV-008; design only, no real run in H1).

Phase A (historical, anonymized) can only REJECT a profile; it never confirms one. Only a prospective Phase B confirms.
Thresholds are fixed here, before any run (frozen dataclass):

  H1 selection:     AUC of the profile value for "held" versus "control" >= 0.65 and the lower 95% bound (bootstrap by
                    quarter) >= 0.55.
  H2 change:        directional accuracy on INCREASE and DECREASE events only >= 0.60 and the Wilson lower bound > 0.50.
  H3 specificity:   the diagonal AUC beats every off-diagonal cell by at least 0.05 (needs the cross matrix).
  H4 calibration:   hit rate non decreasing across confidence bands, and the 70-100 band above the 10-39 band.
  Coverage:         share of decisions without abstention >= 0.60, otherwise the result is reported as partial.
  Sample:           at least 150 directional events per manager, otherwise INCONCLUSIVE (not failed).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult

DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
INCONCLUSIVE = "INCONCLUSIVE"
REJECTED = "REJECTED"
NOT_REJECTED = "NOT_REJECTED"

SIGNAL_COLUMNS = ("quarter", "ticker", "signal", "confidence")
HOLDING_COLUMNS = ("quarter", "ticker", "shares")
CONFIDENCE_BANDS = ((90, 100), (70, 89), (40, 69), (10, 39))


@dataclass(frozen=True)
class FidelityThresholds:
    auc_min: float = 0.65
    auc_lower_min: float = 0.55
    direction_accuracy_min: float = 0.60
    direction_lower_min: float = 0.50
    specificity_margin: float = 0.05
    coverage_min: float = 0.60
    min_directional_events: int = 150
    change_threshold: float = 0.25
    bootstrap_samples: int = 1000
    seed: int = 20261121


@dataclass(frozen=True)
class FidelityReport:
    status: str
    score: float | None
    hypotheses: dict
    n_events: int
    coverage: float | None
    notes: tuple[str, ...] = ()
    phase: str = "A"
    can_confirm: bool = False

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["notes"] = list(self.notes)
        return data


def auc(scores: np.ndarray, labels: np.ndarray) -> float | None:
    """Area under the ROC curve (rank form); labels are booleans, True is the positive class."""
    pos, neg = scores[labels], scores[~labels]
    if len(pos) == 0 or len(neg) == 0:
        return None
    ranks = pd.Series(np.concatenate([pos, neg])).rank(method="average").to_numpy()
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg)))


def wilson_lower(successes: int, n: int, z: float = 1.6448536269514722) -> float:
    """Lower bound of the one sided 95% Wilson interval of a proportion."""
    if n == 0:
        return 0.0
    p = successes / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (centre - margin) / denom


def _check(frame: Any, columns: tuple[str, ...], name: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise BadInput(f"{name} must be a DataFrame")
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise BadInput(f"{name} is missing column(s) {missing}")
    return frame


def label_changes(holdings: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """Per ticker and quarter: INCREASE, HOLD or DECREASE from the share count change against the previous quarter."""
    pivot = holdings.groupby(["quarter", "ticker"])["shares"].sum().unstack("ticker").sort_index()
    rows = []
    quarters = list(pivot.index)
    for i in range(1, len(quarters)):
        prev, cur = pivot.loc[quarters[i - 1]].fillna(0.0), pivot.loc[quarters[i]].fillna(0.0)
        for ticker in pivot.columns:
            before, after = float(prev[ticker]), float(cur[ticker])
            if before == 0.0 and after == 0.0:
                continue
            if before == 0.0:
                label = "INCREASE"
            elif after == 0.0:
                label = "DECREASE"
            else:
                change = (after - before) / before
                label = "INCREASE" if change > threshold else "DECREASE" if change < -threshold else "HOLD"
            rows.append({"quarter": quarters[i], "ticker": ticker, "label": label})
    return pd.DataFrame(rows, columns=["quarter", "ticker", "label"])


def _bootstrap_auc_lower(frame: pd.DataFrame, thresholds: FidelityThresholds) -> float | None:
    rng = np.random.default_rng(thresholds.seed)
    groups = {q: g for q, g in frame.groupby("quarter")}
    keys = list(groups)
    values = []
    for _ in range(thresholds.bootstrap_samples):
        picked = pd.concat([groups[keys[i]] for i in rng.integers(0, len(keys), len(keys))])
        a = auc(picked["signal"].to_numpy(), picked["held"].to_numpy(dtype=bool))
        if a is not None:
            values.append(a)
    return float(np.quantile(values, 0.05)) if values else None


def fidelity_score(signals: pd.DataFrame, holdings: pd.DataFrame | None, *, cross_auc: dict | None = None,
                   manager: str | None = None, thresholds: FidelityThresholds | None = None) -> FidelityReport:
    """Measure how well a profile's signals match a manager's 13F positions (Phase A). Never returns a confirmation.

    signals:  columns quarter, ticker, signal in [-1, 1], confidence in [0, 100], optional abstained (bool). One row per
              profile decision, covering held names and controls.
    holdings: columns quarter, ticker, shares (13F positions, one row per position and quarter).
    cross_auc: optional {profile_manager: {manager: auc}} to evaluate H3; without it H3 is not evaluated.
    """
    th = thresholds or FidelityThresholds()
    if holdings is None or not isinstance(holdings, pd.DataFrame) or holdings.empty:
        return FidelityReport(DATA_UNAVAILABLE, None, {}, 0, None, ("no 13F positions were provided",))
    _check(signals, SIGNAL_COLUMNS, "signals")
    _check(holdings, HOLDING_COLUMNS, "holdings")
    if signals.empty:
        return FidelityReport(DATA_UNAVAILABLE, None, {}, 0, None, ("no profile signals were provided",))
    sig = signals.copy()
    sig["abstained"] = sig["abstained"].astype(bool) if "abstained" in sig.columns else False
    coverage = float(1.0 - sig["abstained"].mean())
    decided = sig[~sig["abstained"]].copy()
    notes: list[str] = []
    held = holdings.groupby(["quarter", "ticker"])["shares"].sum()
    held = held[held > 0]
    decided["held"] = [(q, t) in held.index for q, t in zip(decided["quarter"], decided["ticker"])]
    hypotheses: dict[str, Any] = {}

    # H1: selection
    h1_auc = auc(decided["signal"].to_numpy(dtype=float), decided["held"].to_numpy(dtype=bool)) if len(decided) else None
    lower = _bootstrap_auc_lower(decided, th) if h1_auc is not None and decided["quarter"].nunique() > 1 else None
    hypotheses["H1"] = {"auc": h1_auc, "auc_lower_95": lower,
                        "passed": None if h1_auc is None or lower is None else bool(h1_auc >= th.auc_min and lower >= th.auc_lower_min)}

    # H2: change, directional events only
    labels = label_changes(holdings, th.change_threshold)
    events = decided.merge(labels[labels["label"] != "HOLD"], on=["quarter", "ticker"], how="inner")
    events = events[events["signal"] != 0]
    hits = int(((events["signal"] > 0) & (events["label"] == "INCREASE")).sum() + ((events["signal"] < 0) & (events["label"] == "DECREASE")).sum())
    n_events = int(len(events))
    accuracy = hits / n_events if n_events else None
    hypotheses["H2"] = {"accuracy": accuracy, "n_events": n_events, "lower_bound": wilson_lower(hits, n_events) if n_events else None,
                        "passed": None if accuracy is None else bool(accuracy >= th.direction_accuracy_min and wilson_lower(hits, n_events) > th.direction_lower_min)}

    # H3: specificity (needs the cross matrix)
    if cross_auc and manager and manager in cross_auc:
        row = cross_auc[manager]
        diagonal = row.get(manager)
        others = [v for k, v in row.items() if k != manager]
        hypotheses["H3"] = {"diagonal_auc": diagonal, "best_off_diagonal": max(others) if others else None,
                            "passed": None if diagonal is None or not others else bool(diagonal - max(others) >= th.specificity_margin)}
    else:
        hypotheses["H3"] = {"passed": None, "note": "not evaluated: cross matrix not provided"}

    # H4: calibration of confidence
    if n_events:
        events = events.assign(hit=((events["signal"] > 0) & (events["label"] == "INCREASE")) | ((events["signal"] < 0) & (events["label"] == "DECREASE")))
        bands = {}
        for low, high in CONFIDENCE_BANDS:
            part = events[(events["confidence"] >= low) & (events["confidence"] <= high)]
            bands[f"{low}-{high}"] = {"n": int(len(part)), "hit_rate": float(part["hit"].mean()) if len(part) else None}
        ordered = [bands[f"{lo}-{hi}"]["hit_rate"] for lo, hi in reversed(CONFIDENCE_BANDS)]
        present = [r for r in ordered if r is not None]
        monotonic = all(b >= a for a, b in zip(present, present[1:])) if len(present) > 1 else None
        top = events[events["confidence"] >= 70]["hit"]
        bottom = events[events["confidence"] <= 39]["hit"]
        top_beats = bool(top.mean() > bottom.mean()) if len(top) and len(bottom) else None
        hypotheses["H4"] = {"bands": bands, "monotonic": monotonic,
                            "passed": None if monotonic is None or top_beats is None else bool(monotonic and top_beats)}
    else:
        hypotheses["H4"] = {"passed": None, "note": "no directional events"}

    evaluated = [h["passed"] for h in hypotheses.values() if h["passed"] is not None]
    score = float(np.mean(evaluated)) if evaluated else None
    if coverage < th.coverage_min:
        notes.append(f"coverage {coverage:.2f} is below {th.coverage_min:.2f}: the result is partial")
    if n_events < th.min_directional_events:
        notes.append(f"{n_events} directional events, fewer than {th.min_directional_events}: the result is inconclusive")
        status = INCONCLUSIVE
    elif any(p is False for p in evaluated):
        status = REJECTED
    else:
        status = NOT_REJECTED
        notes.append("Phase A cannot confirm a profile; only the prospective Phase B can")
    return FidelityReport(status, score, hypotheses, n_events, coverage, tuple(notes))


@registry.register(
    "validation.fidelity_13f", module="validation",
    summary="Phase A fidelity of a profile against 13F positions: H1 selection, H2 change, H4 calibration (H3 with a cross matrix). Rejects, never confirms.",
    inputs={"signals": {"type": "dataframe", "required": True}, "holdings": {"type": "dataframe", "required": False, "default": None},
            "manager": {"type": "str", "required": False, "default": None}, "cross_auc": {"type": "dict", "required": False, "default": None},
            "change_threshold": {"type": "float", "required": False, "default": 0.25}},
    piece_ids=("INV-008",),
)
def fidelity_13f(signals: pd.DataFrame, holdings: pd.DataFrame | None = None, manager: str | None = None, cross_auc: dict | None = None,
                 change_threshold: float = 0.25) -> QuantResult:
    name = "validation.fidelity_13f"
    report = fidelity_score(signals, holdings, cross_auc=cross_auc, manager=manager, thresholds=FidelityThresholds(change_threshold=change_threshold))
    if report.status == DATA_UNAVAILABLE:
        return QuantResult(name=name, value={"status": DATA_UNAVAILABLE}, unit="score", method="fidelity_phase_a",
                           warnings=report.notes, piece_ids=("INV-008",))
    return QuantResult(name=name, value=report.to_dict(), unit="score", method="fidelity_phase_a", params={"change_threshold": change_threshold},
                       inputs_hash=helpers.hash_inputs(signals.assign(**{c: signals[c].astype(float) for c in ("signal", "confidence")})[["signal", "confidence"]]),
                       warnings=report.notes, piece_ids=("INV-008",))
