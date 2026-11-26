"""Only `hokma.exec` sends an order to a broker, and the order libraries are imported only in their connector file
(SPEC-018, rule 4 and criterion 7). An AST scan of every Python file of the backend: any call to an order method of a broker or of a
broker SDK outside the allowed files fails the test."""
import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ORDER_METHODS = {"place_order", "replace_order", "cancel_order", "submit_order", "create_order", "create_orders", "edit_order",
                 "cancel_order_by_id", "replace_order_by_id", "cancel_orders", "cancel_all_orders", "close_position",
                 "close_all_positions", "place_orders", "submit_orders"}
# The only door and the connectors, whose own methods call their broker.
SEND_FILES = {
    "backend/hokma/exec/submit.py",
    "backend/hokma/broker/schwab.py",
    "backend/hokma/broker/alpaca.py",
    "backend/hokma/broker/crypto.py",
    "backend/hokma/broker/ibkr/__init__.py",
    "backend/hokma/broker/sim.py",
}
# The backtest engine wrapper calls the order methods of the simulated engine inside a simulation: no broker, no connector and no
# money are reachable from it. test_no_live_path (backend/tests/backtest) proves that file imports nothing from the connectors.
SIMULATED_ENGINE_FILES = {"backend/hokma/backtest/_lib.py"}
LIBRARY_FILES = {"alpaca": {"backend/hokma/broker/alpaca.py"}, "ccxt": {"backend/hokma/broker/crypto.py"}}


def _python() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "backend/hokma/*.py"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return [ROOT / line for line in out.splitlines() if line and (ROOT / line).is_file()]


def order_calls(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in ORDER_METHODS:
                found.append(f"{name} at line {node.lineno}")
            if name in {"getattr", "__import__"} and node.args and isinstance(node.args[-1], ast.Constant) and node.args[-1].value in ORDER_METHODS:
                found.append(f"{name}({node.args[-1].value!r}) at line {node.lineno}")
    return found


def _imports(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "import_module":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names.append(node.args[0].value.split(".")[0])
    return names


def test_no_module_outside_exec_and_the_connectors_sends_an_order():
    offenders = []
    for path in _python():
        rel = path.relative_to(ROOT).as_posix()
        if rel in SEND_FILES or rel in SIMULATED_ENGINE_FILES:
            continue
        offenders += [f"{rel}: {hit}" for hit in order_calls(path)]
    assert not offenders, "\n".join(offenders)


def test_the_new_order_libraries_are_imported_only_in_their_connector_file():
    offenders = []
    for path in _python():
        rel = path.relative_to(ROOT).as_posix()
        for lib, allowed in LIBRARY_FILES.items():
            if lib in _imports(path) and rel not in allowed:
                offenders.append(f"{rel}: imports {lib}")
    assert not offenders, "\n".join(offenders)


def test_the_scan_catches_an_offender(tmp_path):
    bad = tmp_path / "agent_tool.py"
    bad.write_text("def go(broker):\n    broker.place_order(1, {})\n    client.submit_order(req)\n    getattr(b, 'cancel_order')\n", encoding="utf-8")
    assert len(order_calls(bad)) == 3
    lib = tmp_path / "x.py"
    lib.write_text("import alpaca.trading\nimport importlib\nimportlib.import_module('ccxt')\n", encoding="utf-8")
    assert _imports(lib) == ["alpaca", "importlib", "ccxt"]
