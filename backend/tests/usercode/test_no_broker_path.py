"""The isolated code has no road to a broker, to the database or to a second process of its own (SPEC-031 section 10).

The files that RUN generated code (worker, static check, user_api) import from Hokma only what is listed here; the files of the parent never import the
execution service or a broker either. The packages that hold orders and capital never import the isolated code."""
import ast
from pathlib import Path

import pytest

from backtest.support import ROOT

HOKMA = ROOT / "backend" / "hokma"
USERCODE = HOKMA / "factory" / "usercode"
FORBIDDEN_AREAS = {"broker", "exec", "desks", "schwab", "connections", "db", "kit", "llm", "market", "mail", "telegram", "rpc", "github_connect", "backtest"}
CHILD_SIDE = {USERCODE / "worker.py": {"hokma.factory.params", "hokma.factory.user_api", "hokma.factory.usercode.static_check"},
              USERCODE / "static_check.py": {"hokma.factory.user_api"},
              HOKMA / "factory" / "user_api.py": {"hokma.factory.params"}}


def imported(path: Path) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                out.append((node.lineno, "." * node.level + (node.module or "")))
            else:
                out += [(node.lineno, node.module or "")] + [(node.lineno, f"{node.module}.{a.name}") for a in node.names]
    return out


def usercode_files() -> list[Path]:
    return sorted([*USERCODE.rglob("*.py"), HOKMA / "factory" / "user_api.py"])


def test_the_isolated_code_package_exists():
    assert (USERCODE / "worker.py").is_file() and (USERCODE / "sandbox.py").is_file() and (USERCODE / "static_check.py").is_file()
    assert (HOKMA / "factory" / "user_api.py").is_file()


@pytest.mark.parametrize("path", usercode_files(), ids=lambda p: str(p.relative_to(HOKMA)))
def test_no_file_of_the_isolated_code_imports_a_sensitive_area_or_multiprocessing(path):
    bad = []
    for line, module in imported(path):
        parts = module.split(".")
        if module.startswith("."):
            bad.append(f"{line}: relative import {module}")
        elif parts[0] == "hokma" and len(parts) > 1 and parts[1] in FORBIDDEN_AREAS and not (path.parent == USERCODE and parts[1] == "db"):
            bad.append(f"{line}: {module}")
        elif parts[0] in ("multiprocessing", "concurrent", "importlib", "pickle", "marshal", "shelve"):
            bad.append(f"{line}: {module}")
    assert not bad, bad


@pytest.mark.parametrize("path", sorted(CHILD_SIDE), ids=lambda p: str(p.relative_to(HOKMA)))
def test_what_runs_inside_the_process_imports_from_hokma_only_its_short_list(path):
    allowed = CHILD_SIDE[path]
    bad = [f"{line}: {module}" for line, module in imported(path) if module.split(".")[0] == "hokma" and not any(module == a or module.startswith(a + ".") or a.startswith(module + ".") or module == "hokma" for a in allowed)]
    assert not bad, bad


def test_the_worker_loads_in_a_clean_process_without_a_single_sensitive_module():
    import subprocess
    import sys

    code = ("import sys\nimport hokma.factory.usercode.worker\n"
            "bad = [m for m in sys.modules if m.startswith(tuple('hokma.' + a for a in %r))]\nprint(bad)\nassert not bad, bad" % sorted(FORBIDDEN_AREAS))
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_every_process_the_parent_starts_spreads_spawn_kwargs_and_none_uses_fork_or_a_shell():
    for path in (USERCODE / "sandbox.py",):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in [n for n in ast.walk(tree) if isinstance(n, ast.Call)]:
            name = ast.unparse(call.func)
            if name.endswith("Popen"):
                assert any(k.arg is None and ast.unparse(k.value).startswith("spawn_kwargs(") for k in call.keywords), "Popen without **spawn_kwargs()"
                assert not any(k.arg == "shell" for k in call.keywords)
                assert any(k.arg == "close_fds" and ast.unparse(k.value) == "True" for k in call.keywords)
            assert not name.startswith(("os.fork", "os.system", "multiprocessing"))


@pytest.mark.parametrize("area", ["desks", "exec", "broker"])
def test_the_packages_that_hold_orders_and_capital_never_import_the_isolated_code(area):
    bad = []
    for path in sorted((HOKMA / area).rglob("*.py")):
        for line, module in imported(path):
            if module.startswith(("hokma.factory.usercode", "hokma.factory.user_api")):
                bad.append(f"{path.relative_to(HOKMA)}:{line}: {module}")
    assert not bad, bad
