"""Shape of the whole module: scan scope (CC-10), registry, caps, the process that runs the calls and the recipes (SK-119)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _lib

from .conftest import call

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / "backend" / "hokma" / "quant" / "portfolio"
RECIPES = ROOT / "docs" / "quant" / "portfolio-recipes.md"
RESTRICTED = {"scipy", "sklearn", "skfolio", "cvxpy", "plotly", "matplotlib", "statsmodels", "financetoolkit"}
EM_DASH = chr(0x2014)

# One piece id per row of the block. CC-10 and SK-119 are not functions: the first is the import scope, the second the recipes.
PIECES = {"AH-16", "SK-001", "SK-002", "SK-003", "SK-006", "SK-014", "SK-015", "SK-051", "SK-053", "SK-055", "SK-056", "SK-058", "SK-059",
          "SK-060", "SK-077", "SK-079", "SK-080", "SK-085", "SK-086", "SK-087", "SK-088", "SK-089", "SK-106", "SK-108", "SK-109", "SK-111",
          "SK-112", "SK-115", "SK-116", "L-53", "P-09", "P-10"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_cc10_the_library_is_imported_in_lib_only_and_alpha_descriptors_and_panel_are_there():
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name != "_lib.py":
            assert not (_imports(path) & RESTRICTED), f"{path.name} imports a restricted library"
    assert {"scipy", "skfolio", "sklearn", "cvxpy"} <= _imports(PACKAGE / "_lib.py")
    for attribute in ("sk_alpha", "sk_descriptor", "SkAssetPanel", "sk_uncertainty", "sk_prior", "sk_optimization", "SkPortfolio", "SkPopulation"):
        assert hasattr(_lib, attribute), attribute


def test_every_row_of_the_block_has_a_registered_function_with_its_piece_id():
    registered = {pid for spec in registry.list("portfolio") for pid in spec.piece_ids}
    assert PIECES <= registered, sorted(PIECES - registered)
    assert registered <= PIECES, sorted(registered - PIECES)


def test_every_function_is_named_in_the_portfolio_module_and_describes_itself():
    specs = registry.list("portfolio")
    assert len(specs) >= 30
    for spec in specs:
        assert spec.name.startswith("portfolio.") and spec.module == "portfolio" and spec.summary and spec.piece_ids, spec.name
        assert not spec.blocked and spec.fn is not None
        assert spec.fn.__module__.startswith("hokma.quant.portfolio."), spec.name
        assert EM_DASH not in spec.summary
    assert len({s.name for s in specs}) == len(specs)


def test_the_registry_limits_apply_to_the_sizing_parameters(panel):
    with pytest.raises(BadInput):
        call("portfolio.rand_weights", n_assets=10 ** 6)
    with pytest.raises(BadInput):
        call("portfolio.select", returns=panel, method="k_extremes", k=10 ** 6)
    with pytest.raises(BadInput):
        call("portfolio.mean_risk_constrained", returns=panel, cardinality=10 ** 6)
    with pytest.raises(BadInput):
        call("portfolio.select", returns=panel, method="non_expiring", expirations={"AAA": "2030-01-01"}, lookahead_days=10 ** 6)
    with pytest.raises(BadInput):
        call("portfolio.mean_risk", returns=panel, secret_path="/etc/passwd")                    # an unknown argument is refused
    with pytest.raises(BadInput):
        call("portfolio.mean_risk")                                                                 # a required argument is missing


def test_no_function_takes_a_path_or_a_pattern():
    for spec in registry.list("portfolio"):
        for name in spec.inputs:
            assert not re.search(r"(path|file|regex|pattern|glob)", name), (spec.name, name)


def test_a_call_runs_in_the_worker_process_and_comes_back_whole(panel, monkeypatch):
    monkeypatch.setattr(registry, "IN_PROCESS", False)
    result = call("portfolio.baselines", returns=panel.iloc[:100], kind="equal_weighted")
    assert result.value["weights"] == {c: 0.2 for c in panel.columns}
    out = call("portfolio.cs_rank", a=[3, 1, 2])
    assert out.value["ranks"] == [[3.0, 1.0, 2.0]]


def test_the_fixtures_exist_and_are_shaped_as_the_acceptance_tests_say(history, panel):
    assert {"source", "symbol", "as_of", "value", "abstained"} == set(history.columns)
    assert history["source"].nunique() == 4 and history["abstained"].isin([0, 1]).all() and panel.shape == (1000, 5)
    assert history.loc[history["abstained"] == 1, "value"].isna().all()


def test_sk119_the_recipes_exist_in_english_with_a_recipe_per_optimization_type():
    text = RECIPES.read_text(encoding="utf-8")
    assert EM_DASH not in text and not re.search(r"[\u00c0-\u017f]", text)
    headings = re.findall(r"^## (\d+)\. (.+)$", text, flags=re.MULTILINE)
    assert len(headings) >= 10
    for needle in ("Baselines", "Minimum risk", "Tail risk", "Views", "Factors", "Alpha", "Mixing the views of a desk"):
        assert any(needle in title for _, title in headings), needle
    registered = {s.name for s in registry.list("portfolio")}
    mentioned = set(re.findall(r"`(portfolio\.[a-z_]+)`", text))
    assert mentioned and mentioned <= registered, sorted(mentioned - registered)
    assert {"portfolio.mean_risk", "portfolio.robust_cvar", "portfolio.black_litterman", "portfolio.blend_signals", "portfolio.baselines"} <= mentioned
