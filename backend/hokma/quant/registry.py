"""Registry of quant functions (SPEC-017, section 4.3; ED-042).

A function that is not registered does not exist for agents. `call` is the only entry point the MCP tools use.
Blocked functions are registered with `blocked=True`; calling one raises BlockedFunction naming its replacement.
"""
from __future__ import annotations

import builtins
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from hokma.quant.errors import BadInput, BlockedFunction, UnknownFunction
from hokma.quant.types import QuantResult

_LOCK = threading.RLock()
_FUNCTIONS: dict[str, "FunctionSpec"] = {}


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    module: str                               # "metrics", "risk", "validation", "options", "fundamentals", "indicators", "stats" or "portfolio"
    summary: str
    inputs: dict = field(default_factory=dict)  # name -> {"type": ..., "required": bool, "default": ...}
    piece_ids: tuple[str, ...] = ()
    blocked: bool = False
    replacement: str = ""
    fn: Callable[..., QuantResult] | None = field(default=None, compare=False, repr=False)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "module": self.module, "summary": self.summary, "inputs": dict(self.inputs),
            "piece_ids": builtins.list(self.piece_ids), "blocked": self.blocked, "replacement": self.replacement,
        }


def register(name: str, *, module: str, summary: str, inputs: dict | None = None,
             piece_ids: tuple[str, ...] = (), blocked: bool = False, replacement: str = ""):
    """Decorator. The name is unique: registering it twice is a programming error."""
    def wrap(fn: Callable[..., QuantResult]) -> Callable[..., QuantResult]:
        spec = FunctionSpec(name=name, module=module, summary=summary, inputs=dict(inputs or {}),
                            piece_ids=tuple(piece_ids), blocked=blocked, replacement=replacement, fn=fn)
        with _LOCK:
            if name in _FUNCTIONS and _FUNCTIONS[name].fn is not fn:
                raise ValueError(f"quant function {name!r} is already registered")
            _FUNCTIONS[name] = spec
        return fn
    return wrap


def register_blocked(name: str, *, module: str, summary: str, replacement: str, piece_ids: tuple[str, ...] = ()) -> None:
    """Register a library function the module forbids, with the name of the function that replaces it."""
    def refuse(**_kwargs: Any) -> QuantResult:
        raise BlockedFunction(name, replacement)
    register(name, module=module, summary=summary, piece_ids=piece_ids, blocked=True, replacement=replacement)(refuse)


def list(module: str | None = None, *, include_blocked: bool = False) -> builtins.list[FunctionSpec]:  # noqa: A001
    """What agents and the CLI can call, sorted by name."""
    with _LOCK:
        specs = [s for s in _FUNCTIONS.values() if (module is None or s.module == module) and (include_blocked or not s.blocked)]
    return sorted(specs, key=lambda s: s.name)


def describe(name: str) -> FunctionSpec:
    with _LOCK:
        spec = _FUNCTIONS.get(name)
    if spec is None:
        raise UnknownFunction(name)
    return spec


def call(name: str, **kwargs: Any) -> QuantResult:
    """Run a registered function. Blocked functions raise BlockedFunction; unknown names raise UnknownFunction."""
    spec = describe(name)
    if spec.blocked:
        raise BlockedFunction(name, spec.replacement)
    unknown = builtins.set(kwargs) - builtins.set(spec.inputs) if spec.inputs else builtins.set()
    if unknown:
        raise BadInput(f"{name}: unknown argument(s) {sorted(unknown)}")
    missing = [k for k, v in spec.inputs.items() if isinstance(v, dict) and v.get("required") and k not in kwargs]
    if missing:
        raise BadInput(f"{name}: missing argument(s) {missing}")
    assert spec.fn is not None
    from hokma.quant import limits

    limits.check_inputs(kwargs)
    result = _run_with_timeout(spec.fn, kwargs, name)
    if not isinstance(result, QuantResult):
        raise TypeError(f"{name} returned {type(result).__name__}, not QuantResult")
    return result


IN_PROCESS = False          # tests set it; functions registered outside hokma.quant always run in this process


def _run_with_timeout(fn: Callable[..., QuantResult], kwargs: dict[str, Any], name: str) -> QuantResult:
    """Run the function of the registry in a worker process that is killed when limits.MAX_SECONDS passes.

    Functions that are not part of the package (registered by a test) cannot be rebuilt in a worker: they run here.
    """
    from hokma.quant import limits, runner

    if IN_PROCESS or not str(getattr(fn, "__module__", "")).startswith("hokma.quant."):
        return fn(**kwargs)
    return runner.run(name, kwargs, timeout=limits.MAX_SECONDS)


def load_all() -> None:
    """Import every module so that its functions register. Safe to call more than once."""
    import importlib
    for mod in ("hokma.quant.metrics", "hokma.quant.risk", "hokma.quant.validation", "hokma.quant.options",
                "hokma.quant.fundamentals", "hokma.quant.indicators", "hokma.quant.stats", "hokma.quant.portfolio"):
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as exc:      # a module that is not built yet is simply absent
            if exc.name != mod:
                raise


def _reset_for_tests() -> None:
    with _LOCK:
        _FUNCTIONS.clear()
