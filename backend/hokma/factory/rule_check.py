"""The checks of a strategy written as code (SPEC-031 R5 and R7): the static check, the tests the strategy brings and the fixed tests.

    python -I -m hokma.factory.rule_check <folder>        (the folder holds strategy.py and test_strategy.py)

It uses the standard library, numpy, the library of indicators and hokma.factory.user_api, and loads nothing of the desks, the database or the brokers: it
runs in a process that is not the server of Hokma. The tests are plain functions named test_ with assert statements, no framework. Two tests are fixed and
nobody has to write them: the decision on a bar does not change when the later bars are cut (a strategy that looks at the future fails), and the same
bars give the same decisions twice. The exit code is 0 only when everything passes; the last line of the output is one JSON object with the result.
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
import traceback
import types
from pathlib import Path
from typing import Any

import numpy as np

from hokma.factory import strategy_replay, user_api
from hokma.factory.usercode import static_check

STRATEGY_FILE = "strategy.py"
TEST_FILE = "test_strategy.py"
SYNTHETIC_BARS = 400
CUTS = (120, 211, 399)
MAX_MESSAGE = 300


class CheckError(Exception):
    """The folder cannot be checked at all (a file is missing, too large or not text)."""


def read_sources(folder: Path) -> tuple[str, str]:
    """The text of the strategy and of its test, as the parent stores and hashes them: line ends as LF."""
    texts = []
    for name in (STRATEGY_FILE, TEST_FILE):
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise CheckError(f"{name} is missing")
        raw = path.read_bytes()
        if len(raw) > static_check.MAX_BYTES:
            raise CheckError(f"{name} is over {static_check.MAX_BYTES // 1024} KB")
        try:
            texts.append(static_check.normalize_source(raw.decode("utf-8")))
        except UnicodeDecodeError:
            raise CheckError(f"{name} is not UTF-8 text") from None
    return texts[0], texts[1]


def synthetic_bars(n: int = SYNTHETIC_BARS, seed: int = 7) -> dict[str, list[float]]:
    """Bars with trends and reversals, the same on every run: the fixed tests need something the strategy can react to."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 1.0, n) + 0.45 * np.sin(np.arange(n) / 23.0)
    close = 1000.0 + np.cumsum(steps)
    opens = np.concatenate(([close[0]], close[:-1]))
    spread = np.abs(rng.normal(0.0, 0.6, n)) + 0.05
    return {"open": opens.tolist(), "high": (np.maximum(opens, close) + spread).tolist(), "low": (np.minimum(opens, close) - spread).tolist(),
            "close": close.tolist(), "volume": (np.abs(rng.normal(1000.0, 200.0, n)) + 1.0).tolist()}


def _load(name: str, source: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, f"<{name}>", "exec"), module.__dict__)     # the text passed the static check before it got here
    return module


def uses_levels(source: str) -> bool:
    """Whether the strategy ever gives a stop or a target: then the engine must run it with its protective orders."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("long", "short") and (node.args or any(k.arg in ("stop", "target") for k in node.keywords)):
                return True
            if node.func.id == "Decision" and (len(node.args) > 1 or any(k.arg in ("stop", "target") for k in node.keywords)):
                return True
    return False


def _failure(exc: BaseException, sources: dict[str, list[str]]) -> str:
    where = ""
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        name = frame.filename.strip("<>")
        if name in sources and frame.lineno:
            lines = sources[name]
            text = lines[frame.lineno - 1].strip() if 0 < frame.lineno <= len(lines) else ""
            where = f" ({name}.py line {frame.lineno}: {text})"
            break
    detail = "assertion failed" if isinstance(exc, AssertionError) and not str(exc) else f"{type(exc).__name__}: {exc}"
    return (detail + where)[:MAX_MESSAGE]


def _schema_data(cls: Any) -> list[dict[str, Any]]:
    return [{"name": p.name, "kind": p.kind, "default": p.default, "low": p.low, "high": p.high, "choices": list(p.choices)} for p in cls.SCHEMA.params]


def fixed_tests(cls: Any) -> dict[str, dict[str, Any]]:
    bars = synthetic_bars()
    results: dict[str, dict[str, Any]] = {}
    try:
        user_api.build_strategy(cls, None)
        results["builds"] = {"ok": True}
    except Exception as exc:
        results["builds"] = {"ok": False, "message": f"the class does not build with the defaults of its SCHEMA: {type(exc).__name__}: {exc}"[:MAX_MESSAGE]}
        return results
    try:
        first = user_api.replay(cls, bars)
        second = user_api.replay(cls, bars)
    except Exception as exc:
        results["decisions"] = {"ok": False, "message": f"{type(exc).__name__}: {exc}"[:MAX_MESSAGE]}
        return results
    results["decisions"] = {"ok": True, "entries": sum(1 for d in first if d.action in ("long", "short")), "bars": len(first)}
    results["deterministic"] = {"ok": first == second} if first == second else {"ok": False, "message": "the same bars gave different decisions on two runs"}
    for cut in CUTS:
        part = user_api.replay(cls, {k: v[:cut] for k, v in bars.items()})
        bad = next((i for i, (a, b) in enumerate(zip(part, first[:cut])) if a != b), None)
        if bad is not None:
            results["causal"] = {"ok": False, "message": f"the decision on bar {bad + 1} changes when the bars after bar {cut} are cut: the strategy uses the future"}
            return results
    results["causal"] = {"ok": True}
    return results


def engine_fixed_tests(cls: Any) -> dict[str, dict[str, Any]]:
    """The fixed tests of a strategy written against the engine: it builds with the defaults of its SCHEMA, the intents are the same on two runs, and the intents on a bar do not
    change when the later bars are cut."""
    from datetime import datetime, timedelta, timezone

    from hokma.factory.strategy import check_strategy, replay_intents

    bars = synthetic_bars()
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=5 * (i + 1)) for i in range(len(bars["close"]))]
    results: dict[str, dict[str, Any]] = {}

    def play(cut: int | None = None) -> Any:
        n = len(times) if cut is None else cut
        return replay_intents(cls(**check_strategy(cls).defaults()), {k: v[:n] for k, v in bars.items()}, times=times[:n], contract_id="NQ").intents

    try:
        play(10)
        results["builds"] = {"ok": True}
    except Exception as exc:
        results["builds"] = {"ok": False, "message": f"the class does not build or run with the defaults of its SCHEMA: {type(exc).__name__}: {exc}"[:MAX_MESSAGE]}
        return results
    try:
        first, second = play(), play()
    except Exception as exc:
        results["decisions"] = {"ok": False, "message": f"{type(exc).__name__}: {exc}"[:MAX_MESSAGE]}
        return results
    results["decisions"] = {"ok": True, "entries": len(first), "bars": len(times)}
    results["deterministic"] = {"ok": first == second} if first == second else {"ok": False, "message": "the same bars gave different intents on two runs"}
    for cut in CUTS:
        part = play(cut)
        if part != tuple(x for x in first if x[0] <= cut):
            results["causal"] = {"ok": False, "message": f"the intents change when the bars after bar {cut} are cut: the strategy uses the future"}
            return results
    results["causal"] = {"ok": True}
    return results


def run(folder: Path) -> dict[str, Any]:
    """Every check on a folder; the dictionary is the report (ok is true only when all pass)."""
    report: dict[str, Any] = {"ok": False, "static": [], "tests": [], "fixed": {}, "strategy_sha256": None, "test_sha256": None}
    try:
        strategy_text, test_text = read_sources(folder)
    except CheckError as exc:
        report["error"] = str(exc)
        return report
    report["strategy_sha256"] = hashlib.sha256(strategy_text.encode("utf-8")).hexdigest()
    report["test_sha256"] = hashlib.sha256(test_text.encode("utf-8")).hexdigest()
    findings = [{"file": STRATEGY_FILE, **f.to_data()} for f in static_check.check(strategy_text, kind="strategy").findings]
    findings += [{"file": TEST_FILE, **f.to_data()} for f in static_check.check(test_text, kind="test").findings]
    if findings:
        report["static"] = findings
        return report
    assert strategy_replay.replay is not None            # the replay of user_api lives there and is found by its name in the loaded modules
    sys.modules["user_api"] = user_api                  # the spelling an agent can write: the hooks refuse a file that names the package
    sources = {"strategy": strategy_text.split("\n"), "test_strategy": test_text.split("\n")}
    try:
        module = _load("strategy", strategy_text)
    except Exception as exc:
        report["error"] = "strategy.py does not load: " + _failure(exc, sources)
        return report
    classes = [v for v in vars(module).values() if isinstance(v, type) and v.__module__ == "strategy"]
    if len(classes) != 1:
        report["error"] = "strategy.py must define exactly one class"
        return report
    cls = classes[0]
    report["class"] = cls.__name__
    try:
        tests = _load("test_strategy", test_text)
    except Exception as exc:
        report["error"] = "test_strategy.py does not load: " + _failure(exc, sources)
        return report
    for name, fn in vars(tests).items():
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                report["tests"].append({"name": name, "ok": True})
            except Exception as exc:
                report["tests"].append({"name": name, "ok": False, "message": _failure(exc, sources)})
    if static_check.is_engine_source(strategy_text):
        report["fixed"] = engine_fixed_tests(cls)
        if report["fixed"].get("builds", {}).get("ok"):
            report["manifest"] = {"class": cls.__name__, "schema": _schema_data(cls), "indicators": [], "extra_warmup": 0, "protective": True, "engine": True}
        report["ok"] = bool(report["tests"]) and all(t["ok"] for t in report["tests"]) and all(v["ok"] for v in report["fixed"].values()) and "manifest" in report
        return report
    report["fixed"] = fixed_tests(cls)
    if report["fixed"].get("builds", {}).get("ok"):
        probe = user_api.StrategyInit()
        user_api.build_strategy(cls, None).init(probe)
        report["manifest"] = {"class": cls.__name__, "schema": _schema_data(cls), "indicators": [r.to_data() for r in probe.requests],
                              "extra_warmup": probe.extra_warmup, "protective": uses_levels(strategy_text)}
    report["ok"] = bool(report["tests"]) and all(t["ok"] for t in report["tests"]) and all(v["ok"] for v in report["fixed"].values()) and "manifest" in report
    return report


def summary(report: dict[str, Any]) -> str:
    lines = []
    if report.get("error"):
        lines.append(f"ERROR {report['error']}")
    for f in report.get("static", []):
        lines.append(f"STATIC {f['file']} line {f['line']}: {f['message']}")
    for t in report.get("tests", []):
        lines.append(("PASS " if t["ok"] else "FAIL ") + t["name"] + ("" if t["ok"] else ": " + t.get("message", "")))
    for name, value in report.get("fixed", {}).items():
        lines.append(("PASS " if value["ok"] else "FAIL ") + "fixed " + name + ("" if value["ok"] else ": " + value.get("message", "")))
    lines.append("RESULT " + ("green" if report["ok"] else "red"))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -I -m hokma.factory.rule_check <folder>", file=sys.stderr)
        return 2
    from hokma.core import processguard

    with processguard.allow(processguard.RULE_CHECK):          # the contract of the engine lives in packages that only a marked process may import
        report = run(Path(argv[0]))
    print(summary(report))
    print(json.dumps(report, default=str, sort_keys=True, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
