"""Runtime limit (SPEC-015, security review of 5280c87): the modules that hold capital, limits, orders, keys and the
database refuse to be imported in any process that is not Hokma itself.

The textual locks of the hooks (kit/hardblock.py) read what an agent writes. This is the lock that works when the text is
built in a way no rule foresaw: `python -c "import hokma.broker"` run from the agent's shell is a NEW process, it never
went through the server, and the sensitive packages (`hokma.broker`, `hokma.schwab`, `hokma.connections`,
`hokma.desks`, `hokma.db`) raise `SensitiveModuleError` from their `__init__`.

How it works
------------
* The mark is a plain variable of THIS module, in memory of one process. It is not in the environment, not in an
  argument and not on disk, so no child process inherits it: engines, terminals, the shells of the agents, the MCP
  servers Claude starts and the programs those run all start WITHOUT it (a process only gets memory through
  `fork`, and Hokma starts every child with exec).
* Only the entry points of Hokma put it on, each one for the time it runs and no longer (`allow`): the server
  (`hokma engine`, only once the supervisor's launch proof checks out: core/launchproof.py), the owner's commands (`hokma doctor`, `hokma db migrate`, `hokma start --foreground`...), the
  MCP server of the tools (`kit/trash_mcp.py`, started by Claude from the protected config) and the native probe
  child. `allow` checks who calls it: a caller that is not one of those entry points is refused, so
  `python -c "from hokma.core.processguard import allow; ..."` does not work.
* The MCP server runs as a child of the agent's engine, so it cannot receive a secret by a channel the agent does not
  see. It marks itself, and what it can do is what its tools do: the same tools the agent already has, and the 8
  `order_*` tools keep every rule of the broker service.

What it is not: a sandbox. Code that runs inside a process can always change that process's own variables; the limit
stops the import from a new process, which is what an agent's shell can start. Standard library only and no other
Hokma import: this module must load in every process.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Iterator

# Who may switch the mark on, by module name (a program run by path is matched by its exact file, see _PROGRAMS).
SERVER = "server"
OWNER_CLI = "owner_cli"
MCP = "mcp"
PROBE = "probe"
RULE_CHECK = "rule_check"            # the child that checks a strategy written against the engine (1.2.3): it must import the contract of the engine, and it runs the code only after its static check
ENTRY_POINTS = {
    "hokma_cli.main": (OWNER_CLI,),                     # `hokma engine` is marked by engine_main, after the launch proof
    "hokma.core.engine_main": (SERVER,),
    "hokma.kit.trash_mcp": (MCP,),
    "hokma.core.nativeprobe": (PROBE,),
    "hokma.factory.rule_check": (RULE_CHECK,),
    "scripts.vec_load_smoke": (OWNER_CLI,),            # the developer's scripts of this repository
    "scripts.r1_probe": (OWNER_CLI,),
    "scripts.measure_providers": (OWNER_CLI,),
}
_PACKAGE = Path(__file__).resolve().parents[1]         # the folder of the package `hokma`
# A program run by path is told by its exact file, never by a name an agent could copy into its own folder.
_PROGRAMS = {
    str(_PACKAGE / "kit" / "trash_mcp.py"): "hokma.kit.trash_mcp",
    str(_PACKAGE / "core" / "nativeprobe.py"): "hokma.core.nativeprobe",
    **{str(_PACKAGE.parents[1] / "scripts" / f"{name}.py"): f"scripts.{name}"
       for name in ("vec_load_smoke", "r1_probe", "measure_providers")},
}


class SensitiveModuleError(ImportError):
    """A sensitive module was imported in a process that is not the Hokma server."""


_role: str | None = None          # the mark: None in every process until an entry point sets it


def _caller_id(depth: int) -> str:
    """The module of the frame `depth` levels up (0 is this function), or "" when it cannot be told."""
    try:
        scope = sys._getframe(depth).f_globals
    except ValueError:
        return ""
    spec = scope.get("__spec__")
    name = getattr(spec, "name", None) or scope.get("__name__", "")
    if name == "__main__":                                       # run by path: the file tells which program it is
        try:
            return _PROGRAMS.get(str(Path(str(scope.get("__file__", ""))).resolve()), "")
        except (OSError, ValueError):
            return ""
    return str(name)


def marked() -> bool:
    return _role is not None


def role() -> str | None:
    return _role


@contextlib.contextmanager
def allow(kind: str) -> Iterator[None]:
    """Mark this process as `kind` while the entry point runs, then put the previous state back."""
    global _role
    caller = _caller_id(3)                                       # 0 = _caller_id, 1 = here, 2 = contextlib, 3 = the entry point
    if kind not in ENTRY_POINTS.get(caller, ()):
        raise SensitiveModuleError(f"{caller or 'this code'} may not mark a process as {kind}")
    previous, _role = _role, kind
    try:
        yield
    finally:
        _role = previous


def require(module: str) -> None:
    """First line of every sensitive package: refuse the import in a process without the mark."""
    if _role is None:
        raise SensitiveModuleError(
            f"{module} can only be loaded by the Hokma server. This process is not the server of Hokma, so the "
            f"module that holds capital, limits, orders, keys or the database stays closed here.")
