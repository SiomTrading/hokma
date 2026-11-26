"""Recipes of the service strategies folder (SPEC-020: B-38, B-39, B-40, V-26, FT-102, AS-014, AS-015).

A recipe is a markdown file with a front matter and small code blocks. A block marked `python runnable` is executed here against the real fixtures
(the NQ bars of the backtest fixtures and the daily returns of the quant fixtures); a block marked `python runnable engine` also needs the backtest
engine and a database, and is skipped where the engine has no wheel. A block marked `python illustrative` only has to parse: it names modules that
are built by another block of the plan (H3) and cannot run in this tree. The names a runnable block receives are listed in recipes/README.md.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml
from dbsupport import db, env  # noqa: F401  (fixtures)

from backtest import search_support as ss
from backtest.runner_support import NQ, source_of
from backtest.session_support import multi_bars, session_bars
from hokma.backtest import _lib

ROOT = Path(__file__).resolve().parents[3]
RECIPES = ROOT / "kit" / "template" / "service" / "strategies" / "recipes"
NOTICES = ROOT / ".notices" / "THIRD_PARTY.txt"
QUANT_FIXTURES = Path(__file__).resolve().parents[1] / "quant" / "fixtures"

FILES = sorted(p for p in RECIPES.glob("*.md") if p.name != "README.md")
REQUIRED = {"name", "ref", "rows", "lines", "summary", "runnable", "calls"}
SECTIONS = ("## Idea", "## Steps", "## Example", "## Checks")
FENCE = re.compile(r"^```python (runnable|illustrative)( engine)?\n(.*?)^```", re.S | re.M)
LINES = {"DT", "SWC", "SWO", "LP", "RF"}
# Words that would turn a recipe into a channel that sells or pushes signals to other people: none is allowed in any recipe.
FORBIDDEN_CHANNELS = ("telegram", "discord", "whatsapp", "webhook", "send_message", "chat_id", "bot_token", "giphy")


def parse(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name}: a recipe starts with a front matter"
    head, _, body = text[4:].partition("\n---\n")
    data = yaml.safe_load(head)
    assert isinstance(data, dict), path.name
    return data, body


def blocks(body: str) -> list[tuple[str, bool, str]]:
    """(kind, needs the engine, code) of every code block of a recipe, in order."""
    return [(m.group(1), bool(m.group(2)), m.group(3)) for m in FENCE.finditer(body)]


def notice_refs() -> set[str]:
    return set(re.findall(r"^- (rec-\d{3}):", NOTICES.read_text(encoding="utf-8"), re.M))


def namespace(db, tmp_path) -> dict:
    """What a runnable block receives: the real bars and returns of the fixtures, a database and a folder for the curves of the runs."""
    returns = pd.read_csv(QUANT_FIXTURES / "returns_daily.csv", index_col=0, parse_dates=True).iloc[:, 0]
    benchmark = pd.read_csv(QUANT_FIXTURES / "benchmark_daily.csv", index_col=0, parse_dates=True).iloc[:, 0]
    pit = json.loads((QUANT_FIXTURES / "statements_pit.json").read_text(encoding="utf-8"))
    statements = {}
    for company, body in pit["companies"].items():
        frame = pd.DataFrame(body["rows"]).set_index("period_end")
        numeric = [c for c in frame.columns if c != "filed"]
        statements[company] = frame.astype({c: float for c in numeric})
    return {"STATEMENTS": statements, "QUARTERLY": pd.DataFrame(pit["quarterly_revenue_ALFA"]["facts"]),
            "BARS_DAILY": session_bars("daily"), "BARS_DAY": session_bars("day"), "BARS_NIGHT": session_bars("night"),
            "MULTI": {root: multi_bars(root) for root in ("ES", "MES", "CL")}, "RETURNS": returns, "BENCHMARK": benchmark,
            "CONTRACT": NQ, "source_for": lambda bars: source_of({NQ: bars}), "DB": db, "CURVES": tmp_path, "LEDGER": ss.FakeLedger()}


def run_blocks(path: Path, db, tmp_path) -> dict:
    data, body = parse(path)
    scope = namespace(db, tmp_path)
    for kind, engine, code in blocks(body):
        if kind != "runnable":
            continue
        if engine and not _lib.available():
            pytest.skip("the backtest engine has no wheel on this platform; the service answers engine_unavailable there")
        exec(compile(code, f"{path.name}", "exec"), scope)        # noqa: S102 - the code of a recipe of this repository, run on fixtures
    return scope


# ---------------------------------------------------------------------------------------------------------------- structure
def test_the_folder_holds_the_recipes_of_the_rows_and_an_index():
    assert (RECIPES / "README.md").is_file() and len(FILES) >= 20
    for name in ("multi-timeframe", "optimize-and-surface", "signal-plus-exit", "ml-signal", "time-series-forecast"):
        assert (RECIPES / f"{name}.md").is_file(), name


@pytest.mark.parametrize("path", FILES, ids=lambda p: getattr(p, "stem", "none"))
def test_every_recipe_has_its_front_matter_sections_and_a_notice_ref(path):
    data, body = parse(path)
    assert set(data) == REQUIRED, f"{path.name}: keys {sorted(set(data) ^ REQUIRED)}"
    assert data["name"] == path.stem and re.fullmatch(r"[a-z][a-z0-9-]{2,48}", data["name"])
    assert re.fullmatch(r"rec-\d{3}", data["ref"]) and data["ref"] in notice_refs(), f"{path.name}: {data['ref']} is not a line of .notices/THIRD_PARTY.txt"
    assert data["lines"] and set(data["lines"]) <= LINES and isinstance(data["summary"], str) and 20 < len(data["summary"]) < 400
    assert isinstance(data["rows"], list) and data["rows"] and all(re.fullmatch(r"[A-Z]{1,3}-\d{2,3}", r) for r in data["rows"])
    assert isinstance(data["runnable"], bool) and isinstance(data["calls"], list)
    for heading in SECTIONS:
        assert heading in body, f"{path.name}: no {heading!r} section"
    assert body.lstrip().startswith("# "), path.name


def test_every_ref_is_used_once_and_every_notice_ref_has_a_recipe():
    refs = [parse(p)[0]["ref"] for p in FILES]
    assert len(refs) == len(set(refs)) and set(refs) == notice_refs()


@pytest.mark.parametrize("path", FILES, ids=lambda p: getattr(p, "stem", "none"))
def test_a_recipe_has_runnable_blocks_exactly_when_it_says_so(path):
    data, body = parse(path)
    kinds = [k for k, _, _ in blocks(body)]
    assert "runnable" in kinds if data["runnable"] else "runnable" not in kinds, path.name
    if not data["runnable"]:
        assert data["calls"], f"{path.name}: a recipe that cannot run here names the modules it calls"
    assert all(re.fullmatch(r"hokma(\.[a-z_0-9]+)+", c) for c in data["calls"]), path.name


@pytest.mark.parametrize("path", FILES, ids=lambda p: getattr(p, "stem", "none"))
def test_every_block_parses_and_imports_only_the_modules_of_hokma_and_numpy_and_pandas(path):
    _, body = parse(path)
    found = blocks(body)
    for _, _, code in found:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                assert name.split(".")[0] in {"hokma", "numpy", "pandas", "math", "decimal", "datetime", "dataclasses", "itertools", "__future__"}, \
                    f"{path.name}: a recipe never imports {name}: statistics and prices come from the modules of Hokma"
    assert found or path.stem in ("README",), path.name


@pytest.mark.parametrize("path", FILES, ids=lambda p: getattr(p, "stem", "none"))
def test_a_recipe_never_publishes_signals_or_names_a_channel_for_them(path):
    text = path.read_text(encoding="utf-8").lower()
    for word in FORBIDDEN_CHANNELS:
        assert word not in text, f"{path.name}: {word}"
    assert "publish signals" not in text and "sell signals" not in text


def test_the_index_lists_every_recipe_and_states_the_rule_about_signals():
    text = (RECIPES / "README.md").read_text(encoding="utf-8")
    for path in FILES:
        assert f"({path.name})" in text, f"{path.name} is not in the index"
    assert "stays on the machine of the owner" in text and "sends, shares or sells a signal" in text


def test_the_recipe_files_pass_the_repository_rules():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_repo_rules.py")], capture_output=True, text=True, cwd=ROOT)
    mine = [line for line in out.stdout.splitlines() + out.stderr.splitlines()
            if "strategies/recipes/" in line or "broker/fees/" in line or "THIRD_PARTY" in line]
    assert not mine, "\n".join(mine)


# ---------------------------------------------------------------------------------------------------------------- the H3 modules a recipe names
def module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


@pytest.mark.parametrize("path", [p for p in FILES if parse(p)[0]["calls"]], ids=lambda p: getattr(p, "stem", "none"))
def test_the_modules_a_recipe_calls_exist_once_their_block_is_built(path):
    missing = [c for c in parse(path)[0]["calls"] if not module_exists(c)]
    if missing:
        pytest.skip(f"built by another block of the plan (H3): {', '.join(missing)}")


# ---------------------------------------------------------------------------------------------------------------- the runnable blocks
@pytest.mark.parametrize("path", [p for p in FILES if parse(p)[0]["runnable"]], ids=lambda p: getattr(p, "stem", "none"))
def test_the_runnable_blocks_run_on_the_real_fixtures(path, db, tmp_path):
    scope = run_blocks(path, db, tmp_path)
    assert scope.get("OK") is True, f"{path.name}: the last runnable block sets OK = True after its own assertions"
