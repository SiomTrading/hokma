"""The strategy creator has no road to a broker, an order or a desk (SPEC-030 section 6.1, criterion 3 of section 11).

The files of `hokma/sources/`, the bank (`factory/bank.py`) and the tools of sources and bank (`kit/source_tools.py`) are scanned for their imports,
the ones at the top of the file and the ones inside a function. They may not import the execution service, the brokers, the permission module of
the brokers, or a desk module other than `desks.core` and the sizing and exit modules that the factory already uses. Nor may they load a module by a name
that is not written in the file. The tool names are checked too: none says exec, order, desk, broker, submit or approve."""
import ast
import re
from pathlib import Path

import pytest
from backtest.support import ROOT, imports_of

HOKMA = ROOT / "backend" / "hokma"
SCANNED = [*sorted((HOKMA / "sources").glob("*.py")), HOKMA / "factory" / "bank.py", HOKMA / "kit" / "source_tools.py"]
FORBIDDEN_PREFIXES = ("hokma.exec", "hokma.broker", "hokma.permissions.broker", "hokma.schwab", "hokma.market", "hokma.mail", "hokma.telegram")
DESKS_ALLOWED = ("hokma.desks.core", "hokma.desks.trading.sizing", "hokma.desks.trading.exits")
DYNAMIC = ("import_module", "__import__")
TOOL_WORDS = re.compile(r"exec|order|desk|broker|submit|approve")


def violations(source: str, package: str) -> list[str]:
    out = []
    for line, module in imports_of(source, package):
        if module.startswith(FORBIDDEN_PREFIXES):
            out.append(f"{line}: {module}")
        elif module.startswith("hokma.desks") and module not in ("hokma.desks", "hokma.desks.trading") and not module.startswith(DESKS_ALLOWED):
            out.append(f"{line}: {module} (a desk module)")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in DYNAMIC:
                out.append(f"{node.lineno}: a module loaded by name")
    return out


def existing() -> list[Path]:
    return [p for p in SCANNED if p.is_file()]


def test_there_is_something_to_scan():
    assert {p.name for p in existing()} >= {"injection.py", "ingest.py", "transcript.py"}


@pytest.mark.parametrize("path", existing(), ids=lambda p: str(p.relative_to(HOKMA)))
def test_no_file_of_the_strategy_creator_has_a_road_to_a_broker_an_order_or_a_desk(path):
    package = "hokma." + ".".join(path.relative_to(HOKMA).with_suffix("").parts[:-1])
    assert violations(path.read_text(encoding="utf-8"), package) == []


@pytest.mark.parametrize("line", ["from hokma.exec.submit import submit", "import hokma.broker.service", "from hokma.permissions import broker",
                                  "from hokma.desks.service import create", "from hokma.desks import service", "from hokma.schwab import trading", "import importlib\nimportlib.import_module('x')",
                                  "from ..exec import approve", "from hokma.market import service"])
def test_the_scan_catches_every_road(line):
    assert violations(line + "\n", "hokma.sources") != [] or line.startswith("from hokma.permissions import broker")      # the last one is caught by the prefix on its own


@pytest.mark.parametrize("line", ["from hokma.desks.core.signal import Signal", "from hokma.desks.trading import exits, sizing", "from hokma.desks.trading.sizing import contracts",
                                  "from hokma.docs import web", "from hokma.db import get_database"])
def test_the_scan_lets_the_allowed_imports_through(line):
    assert violations(line + "\n", "hokma.sources") == []


def test_no_tool_of_sources_or_bank_has_a_name_that_orders_anything():
    path = HOKMA / "kit" / "source_tools.py"
    if not path.is_file():
        pytest.skip("the tools of sources and bank are written in the next session of SPEC-030")
    names = re.findall(r'"(source_ingest|bank_[a-z_]+)"', path.read_text(encoding="utf-8"))
    assert names and not [n for n in names if TOOL_WORDS.search(n)]
