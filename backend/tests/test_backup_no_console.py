"""Every subprocess call in hokma/backup/ must carry **spawn_kwargs(), or Windows opens a console per call."""
import ast
from pathlib import Path

import hokma.backup

LAUNCHERS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput", "system",
             "create_subprocess_exec", "create_subprocess_shell"}


def _launch_calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            owner = ast.unparse(func.value) if isinstance(func, ast.Attribute) else ""
            if name in LAUNCHERS and owner.split(".")[0] in {"subprocess", "os", "asyncio", ""}:
                yield node


def test_no_subprocess_in_backup_is_missing_spawn_kwargs():
    folder = Path(hokma.backup.__file__).parent
    found, bad = 0, []
    for path in sorted(folder.glob("*.py")):
        for call in _launch_calls(ast.parse(path.read_text(encoding="utf-8"))):
            found += 1
            if not any(k.arg is None and ast.unparse(k.value).startswith("spawn_kwargs(") for k in call.keywords):
                bad.append(f"{path.name}:{call.lineno}")
    assert found, "the scan found no subprocess call at all: the test is broken"
    assert not bad, f"subprocess without **spawn_kwargs(): {bad}"
