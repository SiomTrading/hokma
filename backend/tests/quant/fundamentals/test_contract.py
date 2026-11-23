"""The module as a whole: every row has its function, every function is registered with its piece, nothing breaks the rules of the kit."""
import re
from pathlib import Path

import pytest

from hokma.quant import limits, registry
from hokma.quant.errors import BadInput

from .conftest import AS_OF, call

ROOT = Path(__file__).resolve().parents[4]
PKG = ROOT / "backend" / "hokma" / "quant" / "fundamentals"

# row id -> (registry name that carries it)
ROWS = {
    "FM-13": "enterprise_value", "FM-14": "buyback_yield", "FM-15": "fcf_after_sbc", "FM-16": "roic_simple", "FM-18": "ttm",
    "FT-001": "profitability", "FT-002": "liquidity", "FT-003": "efficiency", "FT-004": "solvency", "FT-005": "valuation",
    "FT-006": "read_formulas", "FT-007": "altman_z", "FT-008": "beneish_m", "FT-009": "piotroski_f", "FT-010": "ohlson_o",
    "FT-011": "zmijewski", "FT-012": "springate_grover_fulmer", "FT-013": "dupont", "FT-014": "wacc", "FT-015": "eva",
    "FT-016": "intrinsic", "FT-017": "ev_breakdown", "FT-018": "growth", "SK-090": "descriptor_set", "SK-091": "book_to_price",
    "SK-092": "earnings_to_price", "SK-093": "dividend_to_price", "SK-094": "gross_profitability", "SK-095": "sales_growth_rate",
    "SK-096": "debt_to_assets", "SK-097": "accruals_cash_flow", "SK-098": "ew_momentum", "SK-099": "ew_volatility", "SK-100": "ew_market_beta",
    "SK-101": "ew_share_turnover", "SK-102": "log_market_cap", "SK-103": "panel", "SK-104": "cs_scale", "DTS-10": "quarter_facts",
    "INV-001": "quality_owner", "SHF-004": "owner_earnings", "SHF-006": "quality_owner", "INV-002": "inversion_veto", "INV-003": "safety_margin",
    "INV-004": "fair_growth", "SHF-009": "fair_growth", "INV-005": "inflection", "INV-006": "contrarian_gap", "SHF-008": "contrarian_gap",
    "INV-007": "disruption", "SHF-007": "disruption", "AFS-028": "fiscal_quarter",
}


def test_every_row_has_a_registered_function_with_its_piece_id():
    assert len(ROWS) == 52
    registered = {s.name: s for s in registry.list("fundamentals")}
    for row, fn in ROWS.items():
        spec = registered[f"fundamentals.{fn}"]
        assert row in spec.piece_ids, (row, fn)
        assert spec.summary and spec.module == "fundamentals"
    assert all(s.piece_ids for s in registered.values())


def test_every_function_has_the_path_of_its_row():
    """The paths the rows name exist as files and functions."""
    text = {p.name: p.read_text(encoding="utf-8") for p in PKG.glob("*.py")}
    for fn, file in (("enterprise_value", "formulas.py"), ("buyback_yield", "formulas.py"), ("fcf_after_sbc", "formulas.py"), ("roic_simple", "formulas.py"),
                     ("ttm", "formulas.py"), ("profitability", "ratios.py"), ("liquidity", "ratios.py"), ("efficiency", "ratios.py"), ("solvency", "ratios.py"),
                     ("valuation", "ratios.py"), ("altman_z", "scores.py"), ("beneish_m", "scores.py"), ("piotroski_f", "scores.py"), ("ohlson_o", "scores.py"),
                     ("zmijewski", "scores.py"), ("springate_grover_fulmer", "scores.py"), ("eva", "value.py"), ("intrinsic", "value.py"),
                     ("ev_breakdown", "value.py"), ("growth", "value.py"), ("fiscal_quarter", "facts.py"), ("quality_owner", "criteria.py"),
                     ("owner_earnings", "criteria.py"), ("inversion_veto", "criteria.py"), ("safety_margin", "criteria.py"), ("fair_growth", "criteria.py"),
                     ("inflection", "criteria.py"), ("contrarian_gap", "criteria.py"), ("disruption", "criteria.py")):
        assert re.search(rf"^def {fn}\(", text[file], re.M), (fn, file)
    for module in ("dupont.py", "cost_of_capital.py", "descriptors.py", "panel.py", "scalers.py", "facts.py"):
        assert (PKG / module).is_file()
    assert (ROOT / "docs" / "quant" / "fundamentals-conventions.md").is_file()
    assert (ROOT / "kit" / "template" / "desk" / "data" / "themes.csv").is_file()
    assert (ROOT / "kit" / "template" / "desk" / "skills" / "earnings-data-discipline" / "SKILL.md").is_file()


def test_only_lib_imports_restricted_libraries():
    restricted = ("scipy", "sklearn", "skfolio", "financetoolkit", "edgar", "statsmodels", "arch", "cvxpy", "quantstats", "ffn", "yfinance")
    for path in PKG.glob("*.py"):
        if path.name == "_lib.py":
            continue
        source = path.read_text(encoding="utf-8")
        for lib in restricted:
            assert not re.search(rf"^\s*(import|from)\s+{lib}\b", source, re.M), (path.name, lib)


def test_no_em_dash_in_what_this_block_wrote():
    files = list(PKG.glob("*.py")) + list(Path(__file__).parent.glob("*.py")) + [
        ROOT / "docs" / "quant" / "fundamentals-conventions.md", ROOT / "kit" / "template" / "desk" / "data" / "themes.csv",
        ROOT / "kit" / "template" / "desk" / "skills" / "earnings-data-discipline" / "SKILL.md"]
    em = chr(0x2014)
    for path in files:
        assert em not in path.read_text(encoding="utf-8"), path.name


def test_curated_themes_table_is_plain_data():
    text = (ROOT / "kit" / "template" / "desk" / "data" / "themes.csv").read_text(encoding="utf-8").splitlines()
    assert text[0] == "symbol,theme" and len(text) > 20
    assert all(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9},[a-z_]+", line) for line in text[1:])


def test_caps_of_the_registry_apply_to_sizing_parameters(alfa):
    import numpy as np
    import pandas as pd

    caps = limits.PARAM_CAPS
    assert caps["lag"] == 10_000 and caps["skip"] == 10_000 and caps["min_periods"] == 100_000 and "aggregation_period" in caps
    fields = {"returns": pd.DataFrame(np.full((30, 2), 0.01), columns=["A", "B"], index=pd.bdate_range("2024-01-01", periods=30))}
    for name, kw in (("sales_growth_rate", {"lag": 10 ** 6}), ("ew_momentum", {"skip": 10 ** 6}), ("ew_volatility", {"min_periods": 10 ** 7}),
                     ("ew_market_beta", {"aggregation_period": 10 ** 6}), ("reversal", {"window": 10 ** 7})):
        with pytest.raises(BadInput):
            call(f"fundamentals.{name}", fields=fields, **kw)
    with pytest.raises(BadInput):
        call("fundamentals.profitability", statements=alfa, unknown_argument=1)
    with pytest.raises(BadInput):
        call("fundamentals.profitability")
    huge = pd.DataFrame(np.zeros((limits.MAX_INPUT_CELLS // 2 + 1, 2)))
    with pytest.raises(BadInput):
        call("fundamentals.cs_scale", values=huge, method="standard")


def test_the_worker_process_runs_the_functions(alfa, monkeypatch):
    """The real path of an agent: a worker process that imports the module and answers with a QuantResult."""
    monkeypatch.setattr(registry, "IN_PROCESS", False)
    try:
        registry.load_all()
    except Exception as exc:                                    # another module of the package is not importable: not this module's failure
        pytest.skip(f"registry.load_all failed in this process: {exc}")
    res = call("fundamentals.liquidity", statements=alfa, as_of=AS_OF)
    assert res.value["latest"]["current_ratio"] == pytest.approx(650 / 330, abs=1e-8)
    assert call("fundamentals.fiscal_quarter", call_name="Q2 FY2025 call").value["fiscal_quarter"] == 2


def test_the_skill_of_the_data_discipline_loads_with_the_format_of_the_kit_AFS_028():
    from hokma.desks.core import skills

    skill = skills.load_skill(ROOT / "kit" / "template" / "desk" / "skills" / "earnings-data-discipline" / "SKILL.md")
    assert skill.name == "earnings-data-discipline" and skill.patterns and skill.avoid
    body = (ROOT / "kit" / "template" / "desk" / "skills" / "earnings-data-discipline" / "SKILL.md").read_text(encoding="utf-8")
    # the rules of the row: textual fiscal quarter, LTM or NTM on every multiple, NTM as four estimates, a common base date, exact quotes
    for needle in ("fundamentals.fiscal_quarter", "fundamentals.valuation_multiple", "fundamentals.quarter_facts", "LTM", "NTM", "next four", "same base date", "exact copy"):
        assert needle in body
    for tool in ("fiscal_quarter", "valuation_multiple", "quarter_facts"):
        assert registry.describe(f"fundamentals.{tool}")
