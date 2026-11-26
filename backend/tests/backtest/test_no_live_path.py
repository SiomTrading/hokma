"""The backtest has no road to a broker or to real money (SPEC-020 section 4.7, criterion 4 of the plan).

`backend/hokma/backtest/` and `backend/hokma/factory/` may import from the broker only the cost functions and the fee tables, and from
the execution service only the pure functions of the pre trade filter (`guards`), the cost analysis model (`tca`, the impact formula
that the costs share) and the money helpers it is built on. `_lib.py` imports only the backtest side of the engine. The tests scan the
imports of every file, so a connector added later fails here before it can run."""
import ast
import sys
from pathlib import Path

import pytest

from backtest.support import ROOT, imports_of, is_engine_module

HOKMA = ROOT / "backend" / "hokma"
SCANNED = [HOKMA / "backtest", HOKMA / "factory"]
BROKER_ALLOWED = {"costs", "fees"}
EXEC_ALLOWED = {"guards", "tca", "money", "algos"}
PURE_ALGOS = {"ALGOS", "MAX_SLICES", "AlgoError", "allocate", "slice_order", "parent_of"}     # the slicing that reads nothing and sends nothing: `execute` is not among them
FORBIDDEN_PACKAGES = ("market", "schwab", "mail", "telegram", "connections", "github_connect", "rpc")
DYNAMIC = ("import_module", "__import__")
ALGOS_MODULE = "hokma.exec.algos"
# The two files that load by name: the registry refuses any class outside its allowed prefixes before it imports (CLASS_PREFIXES) and
# the H3 adapter refuses any module outside hokma.quant before it imports (ALLOWED_PREFIX).
DYNAMIC_OK = {"factory/registry.py", "factory/studies/_h3.py"}
ENGINE_SIDES_ALLOWED = {"backtest", "analysis", "config", "model", "trading"}       # the backtest side of the engine and its value types
ENGINE_BACKTEST_MODULES_ALLOWED = {"engine", "models"}
ENGINE_SIDES_FORBIDDEN = {"adapters", "live", "system", "persistence", "serialization"}


def files() -> list[Path]:
    return sorted(p for root in SCANNED if root.is_dir() for p in root.rglob("*.py"))


def road(module: str) -> str | None:
    parts = module.split(".")
    if parts[0] != "hokma" or len(parts) < 2:
        return None
    area, leaf = parts[1], parts[2] if len(parts) > 2 else None
    if area == "broker" and leaf is not None and leaf not in BROKER_ALLOWED:
        return "a broker module other than costs and fees"
    if area == "exec" and leaf is not None and leaf not in EXEC_ALLOWED:
        return "an execution module other than guards, tca and money"
    return "a connector or a channel" if area in FORBIDDEN_PACKAGES else None


def violations(source: str, package: str = "hokma.backtest", *, dynamic_ok: bool = False) -> list[str]:
    out = []
    for line, module in imports_of(source, package):
        why = road(module)
        if why:
            out.append(f"{line}: {module} ({why})")
    by_line: dict[int, list[str]] = {}
    for line, module in imports_of(source, package):
        if module == ALGOS_MODULE or module.startswith(ALGOS_MODULE + "."):
            by_line.setdefault(line, [])
            if module != ALGOS_MODULE:
                by_line[line].append(module.rsplit(".", 1)[1])
    for line, names in sorted(by_line.items()):       # a bare import of the module reaches `execute`, the function that sends the child orders
        if not names or set(names) - PURE_ALGOS:
            out.append(f"{line}: {ALGOS_MODULE} imported whole or with a name that sends orders ({', '.join(sorted(set(names) - PURE_ALGOS)) or 'the module'})")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in DYNAMIC:
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    why = road(arg.value)
                    if why:
                        out.append(f"{node.lineno}: {arg.value} loaded by name ({why})")
                elif not dynamic_ok:
                    out.append(f"{node.lineno}: a dynamic import of a name that is not written in the file hides what is loaded")
    return out


def test_there_is_something_to_scan():
    assert (HOKMA / "backtest" / "_lib.py").is_file() and len(files()) >= 5


@pytest.mark.parametrize("path", files(), ids=lambda p: str(p.relative_to(HOKMA)))
def test_no_file_of_the_backtest_or_the_factory_has_a_road_to_a_broker(path):
    package = "hokma." + ".".join(path.relative_to(HOKMA).with_suffix("").parts[:-1])
    assert violations(path.read_text(encoding="utf-8"), package, dynamic_ok=str(path.relative_to(HOKMA)) in DYNAMIC_OK) == []


@pytest.mark.parametrize("line", [
    "from hokma.exec.submit import submit", "from hokma.exec import approve", "import hokma.exec.approve",
    "from hokma.broker.service import send", "from hokma.broker import schwab", "from hokma.broker.sim import SimBroker",
    "from hokma.broker.alpaca import Client", "from hokma.broker.ibkr import gateway", "from hokma.schwab import trading",
    "from hokma.market import service", "from hokma.mail import send", "from ..exec.submit import submit", "from ..broker import service",
    "importlib.import_module('hokma.exec.submit')", "__import__('hokma.broker.service')", "importlib.import_module(name)", "__import__(name)",
    "from hokma.exec.algos import execute", "from hokma.exec.algos import allocate, execute", "from hokma.exec import algos", "import hokma.exec.algos",
    "from ..exec.algos import run_slices"])
def test_the_scan_catches_every_road_to_an_order(line):
    assert violations(line + "\n", "hokma.backtest") != []


@pytest.mark.parametrize("line", [
    "from hokma.broker import costs", "from hokma.broker import fees", "from hokma.broker.costs import fill_price", "from hokma.exec import guards",
    "from hokma.exec.algos import allocate", "from hokma.exec.algos import ALGOS, AlgoError, allocate", "from hokma.exec.guards import limits", "from hokma.exec import tca", "from hokma.contracts import identity", "from hokma.quant import metrics",
    "from hokma.backtest.spec import FillRules", "from . import fills", "import os", "importlib.import_module('hokma.exec.guards')"])
def test_the_scan_lets_the_allowed_imports_through(line):
    assert violations(line + "\n", "hokma.backtest") == []


def engine_imports(source: str) -> list[str]:
    return [m for _, m in imports_of(source) if is_engine_module(m)]


def test_the_lib_imports_only_the_backtest_side_of_the_engine():
    found = engine_imports((HOKMA / "backtest" / "_lib.py").read_text(encoding="utf-8"))
    assert found, "_lib.py is the one file that imports the engine"
    for module in found:
        parts = module.split(".")
        second = parts[1] if len(parts) > 1 else None
        assert second not in ENGINE_SIDES_FORBIDDEN, f"{module}: an adapter or the live node of the engine"
        assert second is None or second in ENGINE_SIDES_ALLOWED, f"{module}: a part of the engine the backtest does not need"
        if second == "backtest" and len(parts) > 2:
            assert parts[2] in ENGINE_BACKTEST_MODULES_ALLOWED, f"{module}: only the engine and its models are used"


@pytest.mark.parametrize("tail", ["adapters.interactive_brokers.client", "live.node", "live.execution_client", "adapters.binance", "system.kernel"])
def test_the_scan_of_the_lib_catches_an_adapter_or_the_live_node(tail, tmp_path):
    name = next(m for m in engine_imports((HOKMA / "backtest" / "_lib.py").read_text(encoding="utf-8")))
    top = name.split(".")[0]
    bad = engine_imports(f"import {top}.{tail}\n")
    assert bad and bad[0].split(".")[1] in ENGINE_SIDES_FORBIDDEN


def test_a_dynamic_import_of_a_name_is_accepted_only_where_the_file_guards_it():
    assert violations("importlib.import_module(name)\n", dynamic_ok=True) == []
    assert violations("importlib.import_module('hokma.exec.submit')\n", dynamic_ok=True) != []


# ---- SPEC-032: the files of the verdict (OPT-32) -------------------------------------------------------------------------------------------
# The factory directory above already holds thresholds, questions, the evidence bridge, the verdict and the library catalog; the tool file and the routes are not in it.
VERDICT_FILES = ["factory/thresholds.py", "factory/questions.py", "factory/evidence_bridge.py", "factory/verdict.py", "factory/library_catalog.py",
                 "kit/verdict_tools.py", "verdict/__init__.py", "verdict/api.py", "verdict/service.py"]
DESK_ALLOWED = {"factory/evidence_bridge.py": {"hokma.desks.swo.regime"}}                      # the one regime label that the bridge reads from a desk
BARRED_AREAS = ("exec", "broker", "desks")


def verdict_roads(rel: str) -> list[str]:
    path = HOKMA / rel
    package = "hokma." + ".".join(path.relative_to(HOKMA).with_suffix("").parts[:-1])
    source = path.read_text(encoding="utf-8")
    out = violations(source, package)
    for line, module in imports_of(source, package):
        parts = module.split(".")
        if parts[:1] == ["hokma"] and len(parts) > 1 and parts[1] in BARRED_AREAS and not any(module == ok or module.startswith(ok + ".") or ok.startswith(module + ".") for ok in DESK_ALLOWED.get(rel, ())):
            out.append(f"{line}: {module} (exec, broker and desks are closed to the verdict; only {sorted(DESK_ALLOWED.get(rel, ())) or 'nothing'} is open here)")
    return out


@pytest.mark.parametrize("rel", VERDICT_FILES)
def test_no_file_of_the_verdict_has_a_road_to_a_broker_an_execution_or_a_desk(rel):
    assert (HOKMA / rel).is_file()
    assert verdict_roads(rel) == []


def test_the_desk_that_the_bridge_reads_is_the_regime_label_and_nothing_else():
    assert {m for _, m in imports_of((HOKMA / "factory" / "evidence_bridge.py").read_text(encoding="utf-8"), "hokma.factory") if m.startswith("hokma.desks")} == {
        "hokma.desks.swo.regime", "hokma.desks.swo.regime.derive_regime"}


@pytest.mark.parametrize("line", ["from hokma.exec.submit import submit", "from hokma.broker import costs", "from hokma.desks.swo import graph", "from hokma.desks import service",
                                  "import hokma.desks.swo.regime_other", "from ..exec import guards"])
def test_the_verdict_scan_catches_what_it_closes(line, tmp_path, monkeypatch):
    sample = tmp_path / "sample.py"
    sample.write_text(line + "\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "HOKMA", tmp_path)
    assert verdict_roads("sample.py") != []
