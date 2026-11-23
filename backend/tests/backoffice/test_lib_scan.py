"""The portfolio and spreadsheet libraries are imported only in backoffice/_lib.py (SPEC-018: CC-05)."""
import ast
from decimal import Decimal as D
from pathlib import Path

from hokma.backoffice import _lib

ROOT = Path(_lib.__file__).parent
RESTRICTED = {"financetoolkit", "financedatabase", "openpyxl", "skfolio", "quantstats", "ffn", "scipy", "sklearn", "edgar", "edgartools"}


def restricted_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in {"import_module", "__import__"}:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names = [node.args[0].value]
        found += [n for n in names if n.split(".")[0] in RESTRICTED]
    return found


def test_no_file_of_the_back_office_but_lib_imports_the_libraries_CC_05():
    offenders = [f"{p.relative_to(ROOT)}: {n}" for p in sorted(ROOT.rglob("*.py")) if p.name != "_lib.py" for n in restricted_imports(p)]
    assert offenders == []
    assert sorted(restricted_imports(ROOT / "_lib.py")) == ["financetoolkit.portfolio", "openpyxl"]             # and _lib does hold them


def test_the_scan_catches_an_offender_CC_05(tmp_path):
    bad = tmp_path / "report.py"
    bad.write_text("import openpyxl\nfrom financetoolkit.portfolio import portfolio_model\nimport importlib\nimportlib.import_module('quantstats')\n", encoding="utf-8")
    assert sorted(restricted_imports(bad)) == ["financetoolkit.portfolio", "openpyxl", "quantstats"]


def test_lib_gives_the_portfolio_module_and_the_spreadsheet_door_CC_05(tmp_path):
    assert callable(getattr(_lib.portfolio_model, "__call__", None)) or _lib.portfolio_model is not None
    path = _lib.write_xlsx(tmp_path / "book.xlsx", {"Book": [["symbol", "qty", "price", "note"], ["EQ:AAPL", 90, D("204.00"), None]]})
    assert _lib.read_xlsx(path) == {"Book": [["symbol", "qty", "price", "note"], ["EQ:AAPL", 90, "204.00", None]]}      # a Decimal goes in as its exact text
