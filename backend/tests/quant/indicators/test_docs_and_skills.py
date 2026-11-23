"""AS-007: the indicators document lists every registered indicator with its backing and measured lag. The seven skills of the block load and name real functions.

Pieces covered here: AS-007, ECT-016, FT-077, FT-078, FT-079, FT-080, TA-74, CTS-004, CTS-005, CTS-020, CTS-028, CTS-001, CTS-002, CTS-017.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.desks.core.skills import load_skill, parse_skill
from hokma.quant import registry
from hokma.quant.indicators import momentum, overlap, signal_processing as sp, volatility

ROOT = Path(__file__).resolve().parents[4]
DOC = ROOT / "docs" / "quant" / "indicators.md"
SKILLS = ROOT / "kit" / "template" / "desk" / "skills"
EXPECTED_SKILLS = {"momentum-burst": "indicators.screens.momentum_burst", "weekly-structure": "indicators.screens.weekly_structure",
                   "vcp": "indicators.screens.vcp", "distribution-days": "indicators.screens.distribution_days", "pead-screen": "indicators.screens.pead",
                   "earnings-reaction-score": "indicators.screens.earnings_reaction", "episodic-pivot": "indicators.screens.episodic_pivot"}


def doc_rows() -> dict[str, list[str]]:
    rows = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0].startswith("`indicators."):
            rows[cells[0].strip("`")] = cells
    return rows


def own_functions():
    return [s for s in registry.list("indicators") if str(getattr(s.fn, "__module__", "")).startswith("hokma.quant.indicators.")]


def test_the_document_is_english_and_lists_every_registered_indicator():
    text = DOC.read_text(encoding="utf-8")
    assert chr(0x2014) not in text and not re.search("[" + "".join(chr(c) for c in (0xE1, 0xE9, 0xED, 0xF3, 0xFA, 0xF1, 0xBF, 0xA1)) + "]", text)
    rows = doc_rows()
    functions = own_functions()
    assert len(functions) >= 60
    for spec in functions:
        assert spec.name in rows, f"{spec.name} is not in docs/quant/indicators.md"
        cells = rows[spec.name]
        assert cells[2] and cells[2] != "MISSING" and cells[3] and cells[4]
        assert sorted(p.strip() for p in cells[1].split(",")) == sorted(spec.piece_ids), spec.name
    assert set(rows) <= {s.name for s in registry.list("indicators")}, "the document lists a function that is not registered"


def test_the_two_sided_filters_are_marked_research_only_in_the_document():
    rows = doc_rows()
    for name in sp.RESEARCH_ONLY:
        assert "research only" in rows[name][3]
    assert all(rows[n][3] == "causal" for n in rows if n not in sp.RESEARCH_ONLY)


def _ramp_bars(n: int = 600) -> pd.DataFrame:
    close = 100.0 + np.arange(n, dtype=float)
    return pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 1000.0}, index=pd.bdate_range("2020-01-01", periods=n))


def _lag(result, column: str) -> float:
    close = 100.0 + np.arange(600, dtype=float)
    out = pd.Series([np.nan if v is None else v for v in result.value["values"][column]]).to_numpy()
    return float(np.median(close[-50:] - out[-50:]))


def test_the_measured_lags_in_the_document_are_the_lags_measured_now():
    bars = _ramp_bars()
    rows = doc_rows()

    def listed(name: str, which: int = 0) -> float:
        return float(re.findall(r"-?\d+\.\d+", rows[name][4])[which])

    checks = [
        ("indicators.sma", _lag(overlap.sma(bars, last_n=50), "sma")), ("indicators.ema", _lag(overlap.ema(bars, last_n=50), "ema")),
        ("indicators.wma", _lag(overlap.wma(bars, last_n=50), "wma")), ("indicators.kama", _lag(overlap.kama(bars, last_n=50), "kama")),
        ("indicators.hull", _lag(overlap.hull(bars, last_n=50), "hull")), ("indicators.vwma", _lag(overlap.vwma(bars, last_n=50), "vwma")),
        ("indicators.bollinger", _lag(volatility.bollinger(bars, last_n=50), "middle")), ("indicators.keltner", _lag(volatility.keltner(bars, last_n=50), "middle")),
        ("indicators.donchian", _lag(volatility.donchian(bars, last_n=50), "middle")),
        ("indicators.butterworth", sp.ramp_lag("butterworth", cutoff_period=10.0, order=2)), ("indicators.savgol_causal", sp.ramp_lag("savgol", window=11, polyorder=2)),
        ("indicators.wavelet_denoise", sp.ramp_lag("wavelet", window=64, level=3)),
    ]
    for name, measured in checks:
        assert listed(name) == pytest.approx(measured, abs=6e-5), name
    ich = momentum.ichimoku(bars, last_n=50)
    assert listed("indicators.ichimoku", 0) == pytest.approx(_lag(ich, "conversion"), abs=6e-5)
    assert listed("indicators.ichimoku", 1) == pytest.approx(_lag(ich, "base"), abs=6e-5)
    theory = {"indicators.sma": 24.5, "indicators.ema": 10.0, "indicators.wma": 20 / 3 - 1 / 3, "indicators.bollinger": 9.5}
    for name, value in theory.items():
        assert listed(name) == pytest.approx(value, abs=6e-5), name
    flagged = {n for n, c in rows.items() if re.search(r"\d+\.\d+", c[4])}
    assert flagged == {n for n, _ in checks} | {"indicators.ichimoku"}


@pytest.mark.parametrize("name,function", sorted(EXPECTED_SKILLS.items()))
def test_each_skill_loads_in_the_skill_format_and_names_a_registered_function(name, function):
    skill = load_skill(SKILLS / name / "SKILL.md")
    assert skill.name == name and skill.patterns and skill.avoid
    assert function in skill.body or function in " ".join(skill.patterns)
    assert registry.describe(function).fn is not None
    text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
    assert chr(0x2014) not in text
    assert parse_skill(text).digest() == skill.digest()


def test_the_skills_never_ask_an_agent_to_compute_or_send_an_order():
    for name in EXPECTED_SKILLS:
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8").lower()
        assert "order_place" not in text and "import " not in text
        assert "quant_call" in text
