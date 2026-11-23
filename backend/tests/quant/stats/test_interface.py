"""Interface of the stats module: registry inventory, library isolation (CC-07), limits, language and wording rules."""
import ast
import re
from pathlib import Path

import pytest

from hokma.quant import limits, registry
from hokma.quant.errors import BadInput

from .conftest import call

ROOT = Path(__file__).resolve().parents[4]
STATS = ROOT / "backend" / "hokma" / "quant" / "stats"
TESTS = Path(__file__).parent
SKILLS = ROOT / "kit" / "template" / "desk" / "skills"
EM_DASH = chr(0x2014)
TOOLKIT = "fin" + "ancetoolkit"                      # the dependency behind the range estimators; named only through its import
RESTRICTED = {"scipy", "sklearn", "skfolio", TOOLKIT, "statsmodels", "arch", "linearmodels", "matplotlib", "plotly", "joblib", "numba", "cvxpy"}

EXPECTED = {
    "stats.range_estimators": {"FT-063"}, "stats.yang_zhang": {"FM-09", "FT-063"}, "stats.rogers_satchell": {"FM-10", "FT-063"}, "stats.har_rv": {"FT-064"},
    "stats.gjr_garch": {"FT-061"}, "stats.egarch": {"FT-062"}, "stats.hurst": {"FT-059"}, "stats.unit_root": {"FT-082"},
    "stats.stationarity_check": {"FT-082", "ECT-015"}, "stats.autocorrelation": {"ECT-015"}, "stats.models": {"FT-083", "ECT-015"}, "stats.granger": {"FT-087"},
    "stats.half_life": {"ECT-015"}, "stats.structural_breaks": {"ECT-015"}, "stats.intraday_seasonality": {"ECT-015"},
    "stats.engle_granger": {"FT-081"}, "stats.johansen": {"FT-081"}, "stats.hedge_amounts": {"FT-081"},
    "stats.kalman_filter": {"ECT-035"}, "stats.kalman_calibrate": {"ECT-035"}, "stats.kalman_smoother": {"ECT-035"},
    "stats.regress": {"FT-091"}, "stats.fama_macbeth": {"FT-090"}, "stats.panel": {"FT-092"},
    "stats.iv_2sls": {"FT-093"}, "stats.diff_in_diff": {"FT-093"}, "stats.regression_discontinuity": {"FT-093"}, "stats.propensity_score": {"FT-093"},
    "stats.synthetic_control": {"FT-093"},
    "stats.event_study": {"AH-57", "FT-089"}, "stats.event_stats": {"AH-58"}, "stats.event_plot": {"AH-60"},
    "stats.earnings_reaction": {"AFS-027"}, "stats.reaction_failure": {"CTS-037"},
    "stats.sentiment_score": {"ECT-070"}, "stats.mention_volume_zscore": {"ECT-070"}, "stats.call_tone_pead": {"ECT-070"}, "stats.sentiment_index_zscore": {"ECT-070"},
}


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_every_function_of_the_block_is_registered_in_the_stats_module_with_its_pieces():
    registered = {s.name: s for s in registry.list("stats")}
    assert set(EXPECTED) <= set(registered), sorted(set(EXPECTED) - set(registered))
    for name, pieces in EXPECTED.items():
        spec = registered[name]
        assert pieces <= set(spec.piece_ids) and spec.module == "stats" and spec.summary and spec.fn is not None, name
        assert not spec.blocked
    # every row id of the block is behind at least one function, or is a type, a chart or the library isolation row (tested elsewhere)
    covered = {pid for s in registered.values() for pid in s.piece_ids}
    assert {"FM-09", "FM-10", "AH-57", "AH-58", "AH-60", "ECT-015", "ECT-035", "FT-059", "FT-061", "FT-062", "FT-063", "FT-064", "FT-081", "FT-082", "FT-083", "FT-087", "FT-089",
            "FT-090", "FT-091", "FT-092", "FT-093", "ECT-070", "AFS-027", "CTS-037"} <= covered


def test_CC_07_only_the_lib_file_imports_libraries_and_the_missing_packages_are_not_used():
    for path in sorted(STATS.glob("*.py")):
        found = _imports(path) & RESTRICTED
        if path.name == "_lib.py":
            assert {"scipy", "sklearn", TOOLKIT} <= found
            assert not found & {"statsmodels", "arch", "linearmodels"}, "statsmodels, arch and linearmodels are not dependencies"
        else:
            assert not found, f"{path.name} imports {sorted(found)}"
    for path in sorted(TESTS.glob("test_*.py")):
        assert not (_imports(path) & RESTRICTED), path.name


def test_the_wrappers_reach_every_library_through_lib_only():
    from hokma.quant.stats import _lib

    for attr in ("ft_realized", "ft_garch", "ft_risk_model", "sp_stats", "sp_optimize", "sp_linalg", "sp_signal", "sp_sparse"):
        assert hasattr(_lib, attr), attr


def test_new_size_caps_exist_and_are_enforced_by_the_registry():
    for key in ("max_lag", "weekly_window", "monthly_window", "k_ar_diff", "nw_lags", "hac_lags", "max_breaks"):
        assert key in limits.PARAM_CAPS and limits.PARAM_CAPS[key] > 0, key
    series = __import__("pandas").Series(range(200), dtype=float)
    with pytest.raises(BadInput):
        call("stats.hurst", series=series, max_lag=limits.PARAM_CAPS["max_lag"] + 1)
    with pytest.raises(BadInput):
        call("stats.structural_breaks", series=series, max_breaks=limits.PARAM_CAPS["max_breaks"] + 1)
    with pytest.raises(BadInput):
        call("stats.har_rv", realized_variance=series, monthly_window=limits.PARAM_CAPS["monthly_window"] + 1)
    with pytest.raises(BadInput):
        call("stats.unit_root", series=series, nonexistent=1)                    # an unknown argument is refused
    with pytest.raises(BadInput):
        call("stats.event_stats")                                                # a missing required argument is refused


def test_no_agent_reachable_input_is_a_path_or_a_pattern():
    for spec in registry.list("stats"):
        for key in spec.inputs:
            assert key not in {"path", "file", "filename", "pattern", "regex", "glob", "url"}, (spec.name, key)


def _stats_sources() -> list[Path]:
    return sorted(STATS.glob("*.py")) + sorted(TESTS.glob("*.py")) + sorted(SKILLS.glob("*/SKILL.md"))


def test_no_long_dash_anywhere_in_the_block():
    offenders = [p.name for p in _stats_sources() if EM_DASH in p.read_text(encoding="utf-8")]
    fixtures = [p.name for p in (TESTS / "fixtures").glob("*") if EM_DASH in p.read_text(encoding="utf-8")]
    assert not offenders and not fixtures, (offenders, fixtures)


def test_no_source_repository_authors_or_origin_paths_in_code_or_tests():
    tokens = ["ai" + "-hedge", "hedge" + "_fund", "everything" + "-claude", "financial" + "-services", "stock" + "bee", "Fin" + "anceToolkit", "Fin" + "ance Toolkit", "hokma" + "-kit", "fich" + "as"]
    pattern = re.compile("|".join(re.escape(t) for t in tokens), re.IGNORECASE)
    offenders = []
    for path in sorted(STATS.glob("*.py")) + sorted(TESTS.glob("*.py")) + sorted(SKILLS.glob("*/SKILL.md")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not (path.name == "_lib.py" and line.startswith("from " + TOOLKIT)):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, offenders
    for path in sorted(STATS.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if TOOLKIT in line.lower():
                assert path.name == "_lib.py" and line.startswith("from " + TOOLKIT), f"{path.name}:{number}"      # only the imports


def test_the_three_skills_load_with_the_format_of_the_desk():
    from hokma.core import processguard

    processguard._role = processguard.SERVER
    from hokma.desks.core.skills import load_skill

    for name in ("sentiment-scoring", "earnings-preview", "news-reaction-failure"):
        skill = load_skill(SKILLS / name / "SKILL.md")
        assert skill.name == name and skill.version == "1.0.0" and skill.patterns and skill.avoid and skill.description and skill.identity
        body = skill.body
        assert "quant_call" in body and "stats." in body
