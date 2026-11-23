"""`hokma quant list` and `hokma quant run`: the quant registry from the terminal.

JSON goes to stdout and a one line summary to stderr, so the output can be piped. The numbers come from the same registry
the agents call, so a result here equals the result an agent gets for the same file.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

MODULES = ("metrics", "risk", "validation")


def register(sub: Any) -> None:
    q = sub.add_parser("quant", help="List and run the quant functions (metrics, risk, validation)")
    qsub = q.add_subparsers(dest="quant_cmd", required=True)
    ls = qsub.add_parser("list", help="List the registered functions")
    ls.add_argument("--module", choices=MODULES)
    ls.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ls.set_defaults(fn=cmd_list)
    run = qsub.add_parser("run", help="Run one function over a CSV file of returns")
    run.add_argument("name", help="registry name, or a short name such as sharpe")
    run.add_argument("--file", required=True, help="CSV with an optional date column and one or more value columns")
    run.add_argument("--column", help="value column to use (default: the first one)")
    run.add_argument("--param", action="append", default=[], metavar="KEY=VALUE", help="extra argument; VALUE is read as JSON when it can be")
    run.set_defaults(fn=cmd_run)


def _registry():
    from hokma.quant import registry

    registry.load_all()
    return registry


def cmd_list(args: argparse.Namespace) -> int:
    specs = _registry().list(args.module)
    if args.json:
        print(json.dumps([s.describe() for s in specs], ensure_ascii=False))
    else:
        for s in specs:
            print(f"{s.name:<44} {s.summary}")
    print(f"{len(specs)} function(s)", file=sys.stderr)
    return 0


def resolve_name(registry: Any, name: str) -> str:
    if "." in name:
        return name
    for module in MODULES:
        candidate = f"{module}.{name}"
        try:
            registry.describe(candidate)
            return candidate
        except KeyError:
            continue
    return name


def read_series(path: str, column: str | None):
    import pandas as pd

    frame = pd.read_csv(Path(path))
    first = frame.columns[0]
    try:
        dates = pd.to_datetime(frame[first])
        frame = frame.drop(columns=[first]).set_index(dates)
    except (ValueError, TypeError):
        pass
    if column is not None:
        return frame[column].astype(float)
    return frame.iloc[:, 0].astype(float) if frame.shape[1] == 1 else frame.astype(float)


def _param(text: str) -> tuple[str, Any]:
    key, sep, raw = text.partition("=")
    if not sep or not key:
        raise ValueError(f"--param needs KEY=VALUE, got {text!r}")
    try:
        return key, json.loads(raw)
    except ValueError:
        return key, raw


def cmd_run(args: argparse.Namespace) -> int:
    from hokma.quant.errors import BadInput, BlockedFunction, UnknownFunction

    registry = _registry()
    try:
        name = resolve_name(registry, args.name)
        spec = registry.describe(name)
        data = read_series(args.file, args.column)
        kwargs = dict(_param(p) for p in args.param)
        first_input = next(iter(spec.inputs), "returns")
        kwargs.setdefault(first_input, data)
        result = registry.call(name, **kwargs)
    except UnknownFunction:
        print(f"no quant function named {args.name!r}; run: hokma quant list", file=sys.stderr)
        return 2
    except BlockedFunction as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (BadInput, ValueError, OSError, KeyError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False, default=str))
    print(f"{result.name} = {result.value if not isinstance(result.value, dict) else '(table)'} [{result.unit}] {'; '.join(result.warnings)}", file=sys.stderr)
    return 0 if result.value is not None else 1
