"""Scans of the verdict of the optimizer (SPEC-032, OPT-32). They read files and git, not the engine: they run in a second and fail before a bad change is merged.

1. A road to a broker is closed to every new file: see `backtest/test_no_live_path.py` (VERDICT_FILES).
2. No own statistic: the new files calculate nothing that `hokma.quant` already calculates. They import no library of calculation, define no formula and the one
   function that names a statistic (`_sharpe` of the bridge) only calls `metrics.compute`.
3. The verdict stops nothing: only the three files of the verdict read the table of verdicts, and nothing of exec, broker, desks or backtest imports the verdict.
4. No long dash (U+2014) in a file or a commit message of this branch.
5. The five tools do not take a check out, edit a limit or answer a question (the names, see also `test_verdict_tools.py`).
6. What writes the limits, the answers and the verdicts of the owner is reached only from the routes that ask for the owner confirmation (X-Hokma-Owner, see
   `test_verdict_api.py::test_a_route_that_writes_needs_the_confirmation_of_the_owner`), never from a tool of an agent.
"""
import ast
import re
import subprocess
from pathlib import Path

import pytest
from backtest.support import imports_of
from backtest.test_no_live_path import VERDICT_FILES

from hokma.kit import hokma_tools, verdict_tools

ROOT = Path(__file__).resolve().parents[2]
HOKMA = ROOT / "backend" / "hokma"
BASE = "5942a05b"                                  # the base of the branch (H4)
DASH = "\u2014"
THREE = {"factory/verdict.py", "verdict/api.py", "verdict/service.py"}
NEW_AND_TOUCHED_STATS = [f for f in VERDICT_FILES]  # factory/audit.py is H4's own and keeps its chance hurdle: it is the audit, not the verdict


def tree_of(rel: str) -> ast.AST:
    return ast.parse((HOKMA / rel).read_text(encoding="utf-8"))


# ---- 2. no own statistic ----------------------------------------------------------------------------------------------------------------------------

CALC_LIBRARIES = {"scipy", "numpy", "statsmodels", "sklearn", "statistics", "numba", "arch", "quantstats", "empyrical", "ffn", "talib", "optuna"}
STAT_WORDS = re.compile(r"sharpe|sortino|calmar|dsr|psr|pbo|deflated|volatility|drawdown|cagr|variance|stdev|skew|kurt", re.I)
# What a function that names a statistic may call: the module of H1 that calculates it, the wrapper of the bridge that asks for it, and the readers of the ledger of H4.
DELEGATES = {"metrics.compute", "_sharpe", "_stat", "_saved", "audit.parameter_neighbors"}
MATH_ALLOWED = {"isfinite", "isnan", "isinf", "floor", "ceil", "copysign"}               # no sqrt, log, exp, erf or fsum: they are the start of a formula
STAT_METHODS = {"std", "var", "skew", "kurt", "kurtosis", "corr", "cov", "rolling", "ewm", "expanding", "pct_change", "cumprod", "cumsum", "cummax", "quantile",
                "percentile", "median", "sem"}


def stat_violations(source: str) -> list[str]:
    out: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        out += [f"{node.lineno}: imports {n}" for n in names if n.split(".")[0] in CALC_LIBRARIES]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "math" and node.func.attr not in MATH_ALLOWED:
                out.append(f"{node.lineno}: math.{node.func.attr}")
            if node.func.attr in STAT_METHODS:
                out.append(f"{node.lineno}: .{node.func.attr}( is a statistic")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and STAT_WORDS.search(node.name):
            calls = {f"{c.func.value.id}.{c.func.attr}" if isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name) else getattr(c.func, "id", "")
                     for c in ast.walk(node) if isinstance(c, ast.Call)}
            if not calls & DELEGATES:
                out.append(f"{node.lineno}: def {node.name} names a statistic and does not ask hokma.quant for it")
    return out


@pytest.mark.parametrize("rel", NEW_AND_TOUCHED_STATS)
def test_the_new_files_calculate_no_statistic_of_their_own(rel):
    assert stat_violations((HOKMA / rel).read_text(encoding="utf-8")) == []


def test_the_bridge_asks_the_quant_module_for_every_figure_it_reads():
    found = {n.name: n for n in ast.walk(tree_of("factory/evidence_bridge.py")) if isinstance(n, ast.FunctionDef)}
    assert "_sharpe" in found
    calls = [c for c in ast.walk(found["_sharpe"]) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "compute"]
    assert calls and isinstance(calls[0].func.value, ast.Name) and calls[0].func.value.id == "metrics"
    imported = {m for _, m in imports_of((HOKMA / "factory" / "evidence_bridge.py").read_text(encoding="utf-8"), "hokma.factory")}
    assert "hokma.quant.metrics" in imported or "hokma.quant.metrics.compute" in imported or any(m.startswith("hokma.quant") for m in imported)


@pytest.mark.parametrize("source", [
    "import scipy.stats\n", "import numpy as np\n", "from statistics import stdev\n", "import math\nx = math.sqrt(2)\n", "x = series.std()\n",
    "def deflated_sharpe(a):\n    return a * 2\n", "def my_pbo(a):\n    return sorted(a)\n", "x = returns.rolling(20).mean()\n"])
def test_the_statistics_scan_catches_a_formula_a_library_and_a_statistic_by_its_name(source):
    assert stat_violations(source) != []


@pytest.mark.parametrize("source", ["import math\nok = math.isfinite(1.0)\n", "import pandas as pd\nx = pd.Series([1.0])\n",
                                    "def _sharpe(s):\n    return metrics.compute(s, ['sharpe'])\n"])
def test_the_statistics_scan_lets_the_data_types_and_the_calls_to_quant_through(source):
    assert stat_violations(source) == []


# ---- 3. the verdict stops nothing -------------------------------------------------------------------------------------------------------------------

def test_only_the_three_files_of_the_verdict_and_the_database_files_name_the_table_of_verdicts():
    holders = {p.relative_to(HOKMA).as_posix() for p in HOKMA.rglob("*") if p.suffix in (".py", ".sql") and "strategy_verdicts" in p.read_text(encoding="utf-8")}
    assert holders == {"factory/verdict.py", "verdict/api.py", "db/migrations/0035_verdict.sql", "db/transfer.py"}
    assert THREE >= {h for h in holders if not h.startswith("db/")}
    # the backup list only names the table to copy; it never reads the column
    transfer = (HOKMA / "db" / "transfer.py").read_text(encoding="utf-8")
    assert not re.search(r"strategy_verdicts\W+(SELECT|WHERE)|\.verdict\b", transfer)


VERDICT_MODULES = ("hokma.factory.verdict", "hokma.verdict", "hokma.factory.questions", "hokma.factory.thresholds", "hokma.factory.evidence_bridge",
                   "hokma.factory.library_catalog", "hokma.kit.verdict_tools")


def importers(area: str) -> list[str]:
    out = []
    for path in sorted((HOKMA / area).rglob("*.py")):
        package = "hokma." + ".".join(path.relative_to(HOKMA).with_suffix("").parts[:-1])
        for line, module in imports_of(path.read_text(encoding="utf-8"), package):
            if any(module == m or module.startswith(m + ".") for m in VERDICT_MODULES):
                out.append(f"{path.relative_to(HOKMA).as_posix()}:{line}: {module}")
    return out


@pytest.mark.parametrize("area", ["exec", "broker", "desks", "backtest", "market"])
def test_nothing_that_places_an_order_or_runs_a_backtest_reads_the_verdict(area):
    assert (HOKMA / area).is_dir()
    assert importers(area) == []


def test_the_verdict_is_read_only_by_the_routes_the_tools_and_the_registration():
    found = {}
    for path in sorted(HOKMA.rglob("*.py")):
        rel = path.relative_to(HOKMA).as_posix()
        if rel.startswith(("verdict/", "factory/")) or rel == "kit/verdict_tools.py":
            continue
        package = "hokma." + ".".join(path.relative_to(HOKMA).with_suffix("").parts[:-1])
        hits = [m for _, m in imports_of(path.read_text(encoding="utf-8"), package) if any(m == v or m.startswith(v + ".") for v in VERDICT_MODULES)]
        if hits:
            found[rel] = sorted(set(hits))
    assert set(found) == {"core/app.py", "kit/hokma_tools.py"}, found


def test_the_scan_of_the_importers_catches_a_desk_that_reads_the_verdict(tmp_path, monkeypatch):
    import sys

    (tmp_path / "desks").mkdir()
    (tmp_path / "desks" / "bad.py").write_text("from hokma.factory import verdict\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "HOKMA", tmp_path)
    assert importers("desks") != []


# ---- 4. no long dash ----------------------------------------------------------------------------------------------------------------------------

def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def branch_files() -> list[Path]:
    try:
        git("cat-file", "-e", BASE + "^{commit}")
    except subprocess.CalledProcessError:
        pytest.skip(f"the base commit {BASE} is not in this clone")
    names = git("diff", "--name-only", "--diff-filter=AM", BASE, "HEAD").splitlines()
    return [ROOT / n for n in names if (ROOT / n).is_file()]


def test_no_file_changed_by_the_branch_has_a_long_dash():
    files = branch_files()
    assert len(files) > 10
    bad = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue                                                # a binary file
        if DASH in text:
            bad.append(path.relative_to(ROOT).as_posix())
    assert bad == []


def test_no_commit_message_of_the_branch_has_a_long_dash():
    branch_files()
    messages = git("log", "--format=%H%x00%B%x00", f"{BASE}..HEAD")
    assert messages.strip() and DASH not in messages


def test_the_dash_scan_finds_one(tmp_path):
    sample = tmp_path / "sample.md"
    sample.write_text("a " + DASH + " b", encoding="utf-8")
    assert DASH in sample.read_text(encoding="utf-8")


# ---- 5. the five tools --------------------------------------------------------------------------------------------------------------------------

FIVE = {"optimizer_verdict", "optimizer_verdict_get", "audit_thresholds_get", "library_list", "library_request"}


def test_the_five_tools_are_the_five_and_none_takes_a_check_out_edits_a_limit_or_answers():
    assert set(verdict_tools.TOOLS) == FIVE
    assert set(hokma_tools.TOOLS) >= FIVE
    assert not [n for n in hokma_tools.TOOLS if re.search(r"verdict|threshold|audit|question|answer", n) and re.search(r"exclude|include|answer|restore|copy|save|edit|set_|recompute", n)]
    for name in FIVE:
        props = hokma_tools.TOOLS[name][1]
        assert not set(props) & {"keys", "values", "answers", "actor", "requested_by", "reason", "set_id", "restore"}, name


# ---- 6. an agent cannot write the limits or the answers -------------------------------------------------------------------------------------------

WRITERS = {("thresholds", "save"), ("thresholds", "restore"), ("thresholds", "copy_line"), ("questions", "answer"), ("factory_verdict", "exclude"),
           ("factory_verdict", "include"), ("factory_verdict", "recompute"), ("verdict", "exclude"), ("verdict", "include"), ("verdict", "recompute")}


def writer_calls(source: str) -> list[tuple[str, str, str]]:
    """(enclosing function, module, writer) of every call of a writer."""
    out = []
    for fn in ast.walk(ast.parse(source)):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name) and (c.func.value.id, c.func.attr) in WRITERS:
                    out.append((fn.name, c.func.value.id, c.func.attr))
    return out


def test_the_writers_of_the_limits_the_answers_and_the_verdicts_of_the_owner_are_reached_only_from_routes_that_ask_for_the_owner():
    callers = {}
    for path in sorted(HOKMA.rglob("*.py")):
        rel = path.relative_to(HOKMA).as_posix()
        if rel in ("factory/thresholds.py", "factory/questions.py", "factory/verdict.py"):
            continue
        found = writer_calls(path.read_text(encoding="utf-8"))
        if found:
            callers[rel] = found
    assert set(callers) == {"verdict/api.py"}, callers                    # no tool, no desk, no other service
    api_tree = tree_of("verdict/api.py")
    owned = set()
    for fn in ast.walk(api_tree):
        if isinstance(fn, ast.FunctionDef):
            for deco in fn.decorator_list:
                if isinstance(deco, ast.Call) and any(k.arg == "dependencies" and isinstance(k.value, ast.Name) and k.value.id == "ASOWNER" for k in deco.keywords):
                    owned.add(fn.name)
    asked = {name for name, _, _ in callers["verdict/api.py"]}
    assert asked <= owned, f"a route that writes and does not ask for the owner: {sorted(asked - owned)}"
    assert {"thresholds_save", "thresholds_restore", "thresholds_copy", "answer", "exclude", "include", "recompute"} <= owned


def test_the_writer_scan_catches_a_tool_that_saves_a_limit():
    assert writer_calls("def tool(x):\n    return thresholds.save('DT', {}, actor='agent')\n") == [("tool", "thresholds", "save")]
    assert writer_calls("def tool(x):\n    return thresholds.current('DT')\n") == []
