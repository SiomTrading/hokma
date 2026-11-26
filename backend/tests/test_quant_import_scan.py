"""No agent, tool or kit file imports a quantitative or data library: only the _lib.py of each module does (SPEC-017, rule 4)."""
import ast
import hashlib
import importlib.util
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# numpy and pandas are the data types of every signature and are allowed anywhere in the quant and data packages.
# Every external data or calculation library in pyproject.toml and uv.lock (direct or transitive), plus the usual ones that are not installed.
RESTRICTED = {
    "scipy", "sklearn", "skfolio", "quantstats", "ffn", "financetoolkit", "financedatabase", "edgar", "edgartools",
    "yfinance", "statsmodels", "arch", "cvxpy", "polars", "pyarrow", "matplotlib", "seaborn", "plotly", "sympy", "joblib",
    "yahooquery", "pandas_datareader", "talib", "ta", "empyrical", "pyfolio", "riskfolio", "pypfopt", "vectorbt", "numba",
    "optuna",      # SPEC-020: the search library, only in backtest/_lib.py (the engine name is matched by hash below)
}
ALLOWED_FILES = {
    "backend/hokma/quant/metrics/_lib.py",
    "backend/hokma/quant/risk/_lib.py",
    "backend/hokma/quant/validation/_lib.py",
    "backend/hokma/data/_lib.py",
    "backend/hokma/quant/options/_lib.py",
    "backend/hokma/quant/fundamentals/_lib.py",
    "backend/hokma/quant/indicators/_lib.py",
    "backend/hokma/quant/stats/_lib.py",
    "backend/hokma/quant/portfolio/_lib.py",           # SPEC-019: the five modules of the SWO desk
    "backend/hokma/backoffice/_lib.py",          # SPEC-018: the back office portfolio functions and spreadsheets
    "backend/hokma/backtest/_lib.py",            # SPEC-020: the backtest engine and optuna
}


def _engine_hashes() -> set[str]:
    """The backtest engine is restricted too, but its name may not be written outside four files (N-43): the repository rules
    script keeps it as hashes, and a module is the engine when a word of its name hashes to one of them."""
    spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.ENGINE_HASHES)


ENGINE_HASHES = _engine_hashes()


def _is_restricted(module: str) -> bool:
    top = module.split(".")[0]
    return top in RESTRICTED or any(hashlib.sha256(word.encode()).hexdigest() in ENGINE_HASHES for word in re.findall(r"[a-z0-9]+", top.lower()))


def _tracked_python() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / line for line in out.splitlines() if line and (ROOT / line).is_file()]


def _restricted_imports(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in {"import_module", "__import__"}:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names = [node.args[0].value]
        found += [n for n in names if _is_restricted(n)]
    return found


def test_only_lib_files_import_restricted_libraries() -> None:
    offenders = []
    for path in _tracked_python():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED_FILES:
            continue
        for name in _restricted_imports(path):
            offenders.append(f"{rel}: imports {name}")
    assert not offenders, "\n".join(offenders)


def test_the_scan_catches_an_offender(tmp_path: Path) -> None:
    bad = tmp_path / "agent_tool.py"
    bad.write_text("import quantstats\nfrom scipy import stats\nimport importlib\nimportlib.import_module('ffn')\n", encoding="utf-8")
    assert sorted(_restricted_imports(bad)) == ["ffn", "quantstats", "scipy"]


def test_lib_files_exist() -> None:
    for rel in ALLOWED_FILES:
        assert (ROOT / rel).is_file(), rel
