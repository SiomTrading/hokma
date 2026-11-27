"""A strategy in pandas as an ordinary Python module (1.3.0, task 3433): it is loaded and run in the same process, like any Python code, in its own module.

There is no list of what the code may say, no process apart, no limit of time or memory and no audit hook: the owner decided that the strategy runs as Python runs in Claude Code.
What is read here is the format and nothing else: the text is Python, it is not too long for the base (32 KB, 600 lines), it has a literal `PARAMS` and a `generate_signals(df, params)` with two arguments.

`load(source)` gives a `Strategy`; `Strategy.run(...)` gives the columns of the answer as arrays. A failure of the strategy comes out as a `StrategyError` with its line.
"""
from __future__ import annotations

import ast
import types
from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from hokma.factory.pandas_strategy import contract, sessions
from hokma.factory.pandas_strategy.contract import ContractError, Problem

MAX_CHARS = 30_000                      # the base keeps at most 32 KB of code and 600 lines for a version
MAX_LINES = 600
MODULE_NAME = "hokma_user_strategy"
USER_FILENAME = "<strategy>"
ERROR_TEXT = 400
NEED = ("open", "high", "low", "close", "volume")


class StrategyError(Exception):
    """The code of the strategy failed in its own terms: it raised, or it answered with something that is not a table of signals."""

    def __init__(self, kind: str, message: str, *, line: int | None = None, field: str | None = None, fix: str | None = None) -> None:
        super().__init__(message)
        self.kind, self.message, self.line, self.field, self.fix = kind, message, line, field, fix

    def to_problems(self) -> list[Problem]:
        rule = {"rule_error": "raised", "bad_return": "bad_return", "contract": "generate_signals"}.get(self.kind, self.kind)
        fix = self.fix or ("Read the message: it names the error and the line of your code where it happened." if rule == "raised" else "Return a DataFrame with the index of df and the signal columns.")
        return [Problem(rule, self.message, fix, self.line, self.field)]


def normalize(source: str) -> str:
    """The text as it is hashed and run: Unix line ends and no final blank lines beyond one."""
    return source.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def _literal(node: ast.AST) -> bool:
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False


def read(source: str) -> tuple[list[Problem], tuple[contract.ParamDef, ...]]:
    """The problems of the format of a strategy and, when PARAMS reads, its definitions. An empty list is the only way through. Nothing is run."""
    if not isinstance(source, str):
        return [Problem("source", "the code is not text.", "Send the code as text.")], ()
    text = normalize(source)
    if "\x00" in text:
        return [Problem("source", "the code has a null character.", "Remove it.")], ()
    if len(text) > MAX_CHARS or text.count("\n") > MAX_LINES:
        return [Problem("size", f"the code is over {MAX_CHARS // 1000} KB or {MAX_LINES} lines.", "A strategy is a short function: keep what it needs.")], ()
    try:
        tree = ast.parse(text, USER_FILENAME)
    except SyntaxError as exc:
        return [Problem("syntax", f"{exc.msg}.", "Fix the syntax at that line.", exc.lineno or None)], ()
    except (ValueError, MemoryError, RecursionError) as exc:
        return [Problem("syntax", f"the code cannot be read ({type(exc).__name__}).", "Simplify the code.")], ()
    problems: list[Problem] = []
    params: ast.Assign | None = None
    func: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PARAMS" for t in node.targets):
            params = node
        elif isinstance(node, ast.FunctionDef) and node.name == "generate_signals":
            func = node
    if func is None:
        problems.append(Problem("generate_signals", "there is no generate_signals(df, params).", "Define def generate_signals(df, params): and return a DataFrame with the signal columns."))
    else:
        a = func.args
        if len(a.args) != 2 or a.posonlyargs or a.vararg or a.kwarg or a.kwonlyargs:
            problems.append(Problem("generate_signals", "generate_signals takes exactly two arguments, df and params.", "Write def generate_signals(df, params):.", func.lineno))
    defs: tuple[contract.ParamDef, ...] = ()
    if params is None:
        problems.append(Problem("params", "there is no PARAMS.", 'Write PARAMS = {"fast": {"default": 9, "min": 3, "max": 30, "step": 1, "type": "int"}} before the function.'))
    elif not _literal(params.value):
        problems.append(Problem("params", "PARAMS must be written as plain numbers and texts, not computed.", "Write the numbers: PARAMS = {\"fast\": {\"default\": 9, ...}}.", params.lineno, "PARAMS"))
    else:
        try:
            defs = contract.parse_params(ast.literal_eval(params.value))
        except ContractError as exc:
            problems += [Problem(p.rule, p.message, p.fix, params.lineno, p.field) for p in exc.problems]
    problems.sort(key=lambda p: (p.line or 0, p.rule))
    return problems, defs


def _where(tb: Any) -> int | None:
    """The line, inside the code of the strategy, where an exception went through last."""
    line = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == USER_FILENAME:
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def _column(name: str, series: Any) -> np.ndarray:
    values = np.asarray(series)
    if values.dtype == np.dtype("bool"):
        return values.astype("|b1")
    if values.dtype.kind in "fiu":
        return values.astype("<f8")
    if values.dtype == object:
        try:
            return np.asarray(pd.to_numeric(series, errors="raise"), dtype="<f8")
        except (ValueError, TypeError):
            pass
        flags = pd.Series(series).map(lambda x: x if isinstance(x, (bool, np.bool_)) else (None if x is None or x != x else x))
        if flags.map(lambda x: isinstance(x, (bool, np.bool_)) or x is None).all():
            return flags.fillna(False).astype(bool).to_numpy().astype("|b1")
    raise TypeError(f"the column {name} has the type {values.dtype}")


class Strategy:
    """The code of one strategy, loaded in its own module of this process."""

    def __init__(self, module: types.ModuleType) -> None:
        self.module = module

    def run(self, params: Mapping[str, Any], open_ns: np.ndarray, columns: Mapping[str, np.ndarray], bar_seconds: int) -> dict[str, np.ndarray]:
        """The signals of the table: open_ns (nanoseconds, UTC) and open, high, low, close, volume go in; the columns of the answer come out as arrays. Raises StrategyError for what the code does."""
        frame = sessions.frame_of(np.asarray(open_ns, dtype="<i8"), {k: np.asarray(columns[k], dtype="<f8") for k in NEED}, int(bar_seconds))
        try:
            result = self.module.generate_signals(frame, dict(params))
        except Exception as exc:                                                    # noqa: BLE001  whatever the strategy raises is data, with its line
            raise StrategyError("rule_error", f"{type(exc).__name__}: {exc}"[:ERROR_TEXT], line=_where(exc.__traceback__)) from None
        if not isinstance(result, pd.DataFrame):
            raise StrategyError("bad_return", f"generate_signals returned {type(result).__name__}; it must return a DataFrame", fix="Build it with out = pd.DataFrame(index=df.index) and return out.")
        if len(result) != len(frame) or not result.index.equals(frame.index):
            raise StrategyError("bad_return", f"the answer has {len(result)} rows and a different index than df ({len(frame)} rows)", fix="Build the answer with the index of df: pd.DataFrame(index=df.index).")
        out: dict[str, np.ndarray] = {}
        for name in result.columns:
            if not isinstance(name, str):
                raise StrategyError("bad_return", f"a column of the answer has the name {name!r}, which is not text")
            try:
                out[name] = _column(name, result[name])
            except (TypeError, ValueError) as exc:
                raise StrategyError("bad_return", str(exc), field=name, fix=f"Make {name} a bool column (a comparison) or a float column.") from None
        return out


def load(source: str) -> Strategy:
    """Compile the text and run its top level in a module of its own. Raises StrategyError when the format is wrong or the top level raises."""
    problems, _defs = read(source)
    if problems:
        first = problems[0]
        raise StrategyError(first.rule, first.message, line=first.line, field=first.field, fix=first.fix)
    module = types.ModuleType(MODULE_NAME)
    try:
        exec(compile(normalize(source), USER_FILENAME, "exec"), module.__dict__)          # noqa: S102  the strategy runs as any Python does
    except SyntaxError as exc:
        raise StrategyError("rule_error", f"SyntaxError: {exc.msg}", line=exc.lineno) from None
    except Exception as exc:                                                                # noqa: BLE001  the top level of a strategy is its own
        raise StrategyError("rule_error", f"{type(exc).__name__}: {exc}"[:ERROR_TEXT], line=_where(exc.__traceback__)) from None
    if not isinstance(getattr(module, "generate_signals", None), types.FunctionType) or not isinstance(getattr(module, "PARAMS", None), dict):
        raise StrategyError("contract", "the code needs PARAMS (a dict) and generate_signals(df, params)")
    return Strategy(module)


def provider(source: str, params: Mapping[str, Any], open_ns: np.ndarray, columns: Mapping[str, np.ndarray], bar_seconds: int) -> dict[str, np.ndarray]:
    """The provider of signals that the adapter of the factory uses in a backtest and in the optimizer. A failure of the code stops the run with its reason."""
    from hokma.factory.strategy import BadStrategy

    try:
        return load(source).run(params, open_ns, columns, bar_seconds)
    except StrategyError as exc:
        raise BadStrategy("; ".join(p.text() for p in exc.to_problems()[:3])) from None
