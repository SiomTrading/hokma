"""Every process launch in backend/hokma must spread `spawn_kwargs(...)` (no console window on Windows)."""
import ast
import pathlib

PKG = pathlib.Path(__file__).resolve().parents[1] / "hokma"
SUBPROCESS_CALLS = {"Popen", "run", "check_output", "check_call", "call", "getoutput", "getstatusoutput"}
ASYNCIO_CALLS = {"create_subprocess_exec", "create_subprocess_shell"}
# Files allowed to launch without spawn_kwargs, each with its reason. Keep this list minimal.
EXEMPT: dict[str, str] = {}


def _aliases(tree: ast.AST) -> tuple[set[str], set[str], dict[str, str]]:
    """Names bound to the subprocess module, to the asyncio module, and to imported callables."""
    sub_mods, aio_mods, funcs = set(), set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess":
                    sub_mods.add(a.asname or "subprocess")
                elif a.name == "asyncio":
                    aio_mods.add(a.asname or "asyncio")
                elif a.name == "asyncio.subprocess":
                    aio_mods.add("asyncio")
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if node.module == "subprocess" and a.name in SUBPROCESS_CALLS:
                    funcs[a.asname or a.name] = a.name
                elif node.module in ("asyncio", "asyncio.subprocess") and a.name in ASYNCIO_CALLS:
                    funcs[a.asname or a.name] = a.name
                elif node.module == "asyncio" and a.name == "subprocess":
                    pass          # `asyncio.subprocess` constants only; its callables are caught as attributes
    return sub_mods, aio_mods, funcs


def _is_launch(call: ast.Call, sub_mods: set[str], aio_mods: set[str], funcs: dict[str, str]) -> bool:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id in funcs
    if isinstance(f, ast.Attribute):
        if isinstance(f.value, ast.Name):
            if f.value.id in sub_mods and f.attr in SUBPROCESS_CALLS:
                return True
            if f.value.id in aio_mods and f.attr in ASYNCIO_CALLS:
                return True
        if (isinstance(f.value, ast.Attribute) and f.value.attr == "subprocess" and isinstance(f.value.value, ast.Name)
                and f.value.value.id in aio_mods and f.attr in ASYNCIO_CALLS):
            return True
    return False


def _uses_spawn_kwargs(call: ast.Call) -> bool:
    """True when some argument expression calls spawn_kwargs (`**spawn_kwargs()` or a variable is not enough)."""
    for node in [*call.args, *(k.value for k in call.keywords)]:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                if (isinstance(fn, ast.Name) and fn.id == "spawn_kwargs") or (
                        isinstance(fn, ast.Attribute) and fn.attr == "spawn_kwargs"):
                    return True
    return False


def find_violations(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source, filename)
    sub_mods, aio_mods, funcs = _aliases(tree)
    return [f"{filename}:{n.lineno}" for n in ast.walk(tree)
            if isinstance(n, ast.Call) and _is_launch(n, sub_mods, aio_mods, funcs) and not _uses_spawn_kwargs(n)]


def test_every_launch_uses_spawn_kwargs():
    bad: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).as_posix()
        if rel in EXEMPT or rel == "core/proc.py":
            continue
        bad += find_violations(path.read_text(encoding="utf-8"), rel)
    assert not bad, "process launches without spawn_kwargs(): " + ", ".join(bad)


def test_detector_catches_aliases_and_direct_imports():
    assert find_violations("import subprocess as sp\nsp.run(['x'])") == ["<src>:2"]
    assert find_violations("from subprocess import Popen as P\nP(['x'])") == ["<src>:2"]
    assert find_violations("import asyncio\nasyncio.create_subprocess_exec('x')") == ["<src>:2"]
    assert find_violations("import asyncio\nasyncio.subprocess.create_subprocess_shell('x')") == ["<src>:2"]
    ok = "import subprocess\nsubprocess.run(['x'], **spawn_kwargs())"
    assert find_violations(ok) == []
    assert find_violations("import subprocess\nsubprocess.run(['x'], **{'a': 1})") == ["<src>:2"]
    assert find_violations("import subprocess\nsubprocess.PIPE\nx = subprocess.CalledProcessError") == []
