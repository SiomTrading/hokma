"""V-29: the strategies of the factory ask `quant.indicators` for averages, RSI, MACD, ATR and OBV; no file of the factory imports an indicator library."""
from __future__ import annotations

import ast
from pathlib import Path

from hokma.quant import registry

ROOT = Path(__file__).resolve().parents[4]
FACTORY = ROOT / "backend" / "hokma" / "factory"
BASICS = ("sma", "ema", "wma", "rsi", "macd", "atr", "obv")
# technical analysis libraries, whatever their name, and the file of this package that wraps them
LIBRARIES = {"financetoolkit", "talib", "ta", "pandas_ta", "tulipy", "stockstats", "finta", "bta", "vectorbt", "quantstats", "scipy", "statsmodels", "numba"}
WRAPPERS = {"hokma.quant.indicators._lib", "hokma.quant.indicators._common", "hokma.quant.stats._lib", "hokma.quant.options._lib"}


def banned_imports(path: Path) -> list[str]:
    """Indicator libraries and the wrapper files of the quant modules that a file imports (names are matched on the module path)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        found += [n for n in names if n.split(".")[0] in LIBRARIES or n in WRAPPERS]
    return found


def test_the_basic_indicators_are_registered_in_quant_indicators():
    registry.load_all()
    registered = {s.name for s in registry.list("indicators")}
    assert {f"indicators.{name}" for name in BASICS} <= registered, sorted({f"indicators.{n}" for n in BASICS} - registered)


def test_no_file_of_the_factory_imports_an_indicator_library():
    offenders = {path.relative_to(ROOT).as_posix(): banned_imports(path) for path in sorted(FACTORY.rglob("*.py"))}
    assert not {k: v for k, v in offenders.items() if v}, offenders
    assert FACTORY.is_dir() and any(FACTORY.rglob("*.py"))                       # the scan has files to read


def test_the_factory_reads_its_indicators_through_the_registry():
    text = (FACTORY / "indicator_library.py").read_text(encoding="utf-8")
    assert "registry.describe" in text and "hokma.quant.indicators" in text


def test_the_scan_catches_a_factory_file_that_imports_a_library(tmp_path):
    bad = tmp_path / "strategy.py"
    bad.write_text("import talib\nfrom financetoolkit.technicals import momentum_model\nfrom hokma.quant.indicators import _lib\nfrom hokma.quant.indicators._lib import ta_momentum\n", encoding="utf-8")
    assert set(banned_imports(bad)) == {"talib", "financetoolkit.technicals", "financetoolkit.technicals.momentum_model", "hokma.quant.indicators._lib"}
    ok = tmp_path / "ok.py"
    ok.write_text("from hokma.quant import registry\nimport numpy as np\nimport pandas as pd\n", encoding="utf-8")
    assert banned_imports(ok) == []
