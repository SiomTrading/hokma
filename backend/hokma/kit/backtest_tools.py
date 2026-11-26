"""Tools of the backtest service for the agents (SPEC-020, section 4.11): `backtest_run`, `backtest_status`, `backtest_result`, `backtest_optimize`,
`backtest_walkforward`, `backtest_montecarlo` and `backtest_evidence`, and the two of the premium threshold: `premium_calibrate` (only the owner and the monthly
routine; the code decides from the evidence of a backtest and nothing the caller passes opens the switch) and `premium_switch_state` (read only).

Every tool validates its sizes with `hokma.quant.limits` before it does anything, takes ids and values and never a path or a pattern, and answers with
a classified error (`no_data`, `unavailable`, `stale`, `bad_input`, `blocked` plus `forbidden_fill`, `bar_not_allowed_for_line`, `missing_tests`,
`attempt_budget_spent` and `evaluator_changed`). What decides a run is derived from real state: the line of a strategy is the one of its saved spec, the
cut in time of a run is its end, the trial count of a deflated Sharpe ratio is the ledger. No tool here sends an order or reaches a broker or a
connector: the service only runs history (spec 4.7); the two premium tools name a desk only to measure or read its switch, and the test of the scan reads this file with the same rules as `backtest/`.

When the caller is Ian (the strategy researcher) every call goes through his `ResearchRun`: the budget of attempts, the tests of the spec and the list
of his tools are checked first, and the run ids that come back are remembered as fresh for his claim guard.
"""
from __future__ import annotations

import functools
import json
import re
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from hokma.kit.tool_registry import CODES, ToolError, ToolSet

TOOLSET = ToolSet()
EXTRA_CODES = ("forbidden_fill", "bar_not_allowed_for_line", "missing_tests", "attempt_budget_spent", "evaluator_changed")
MAX_RANGE_DAYS = 3_660              # ten years of calendar days in one run
MAX_PARAMS = 32
MAX_TRADES = 500
MAX_TOP = 20
MAX_WINDOWS = 100
MAX_SESSIONS = 64
_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")
_CONTRACT = re.compile(r"^[A-Z]{2,6}:[A-Za-z0-9._=/:-]{1,54}$")
_PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_CODE_PREFIX = re.compile(r"^([a-z][a-z_]*):")
_PREFIX_CLASS = {"no_run": "no_data", "no_walk_forward": "no_data", "data_changed": "stale", "ledger_missing": "unavailable",
                 "cluster_unavailable": "unavailable", "budget_too_small": "bad_input"}
_STAGE_CLASS = {"no_data": "no_data", "no_bars": "no_data"}


class ClassifiedError(ToolError):
    """A `ToolError` that may also carry one of the codes of the backtest tools. The payload is the same JSON the MCP layer already reads."""

    def __init__(self, code: str, message: str, suggestions: list[str] | None = None) -> None:
        if code in CODES:
            super().__init__(code, message, suggestions)
            return
        if code not in EXTRA_CODES:
            raise ValueError(f"unknown error code {code!r}")
        self.code, self.message, self.suggestions = code, message, list(suggestions or [])
        ValueError.__init__(self, self.payload())


def _s(desc: str = "", **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def _i(desc: str = "") -> dict[str, Any]:
    return {"type": "integer", "description": desc}


def _b(desc: str = "") -> dict[str, Any]:
    return {"type": "boolean", "description": desc}


def _o(desc: str = "") -> dict[str, Any]:
    return {"type": "object", "description": desc}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# ------------------------------------------------------------------------------------------------------------------------- classes of error
def _stated(code: str, exc: Exception) -> str:
    """The message of `exc` with its stable code first, once."""
    text = str(exc).strip("'\"")[:400]
    return text if _CODE_PREFIX.match(text) else f"{code}: {text}"


def classify(exc: Exception) -> ToolError | None:
    """The classified error of an exception of the service, or None when it is not one of the known failures (it is then a bug and propagates)."""
    from hokma.backtest import _lib
    from hokma.backtest.runner import StageError
    from hokma.backtest.spec import BarNotAllowed, ForbiddenFill
    from hokma.factory import researcher
    from hokma.quant.errors import BadInput

    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, ForbiddenFill):
        return ClassifiedError("forbidden_fill", str(exc)[:400], ["Use the next open plus one tick or more, stop first, gap at the open"])
    if isinstance(exc, BarNotAllowed):
        return ClassifiedError("bar_not_allowed_for_line", str(exc)[:400])
    if isinstance(exc, (researcher.MissingTests, researcher.AttemptBudgetSpent, researcher.EvaluatorChanged)):
        return ClassifiedError(exc.code, str(exc)[:400])
    if isinstance(exc, researcher.ToolNotAllowed):
        return ClassifiedError("blocked", str(exc)[:400])
    if isinstance(exc, researcher.ResearchError):
        return ClassifiedError("bad_input", _stated(exc.code, exc))
    if isinstance(exc, (_lib.EngineUnavailable, _lib.SearchUnavailable)):
        return ClassifiedError("unavailable", _stated(exc.code, exc), ["Backtests need the engine of this machine"])
    if isinstance(exc, StageError):
        return ClassifiedError(_STAGE_CLASS.get(exc.code, "unavailable" if exc.stage == "data" else "bad_input"), str(exc)[:400])
    if isinstance(exc, (BadInput, KeyError)):
        return ClassifiedError("bad_input", str(exc).strip("'\"")[:400])
    if isinstance(exc, (ValueError, LookupError)):
        found = _CODE_PREFIX.match(str(exc))
        return ClassifiedError(_PREFIX_CLASS.get(found.group(1) if found else "", "bad_input"), _stated(str(getattr(exc, "code", "bad_input")), exc))
    return None


def safe(fn: Callable[[Any, Any, dict], str]) -> Callable[[Any, Any, dict], str]:
    """A tool handler whose known failures come back as classified errors (the rest propagate: they are bugs, not answers)."""
    @functools.wraps(fn)
    def wrapper(u: Any, c: Any, a: dict) -> str:
        try:
            return fn(u, c, a)
        except ToolError:
            raise
        except Exception as exc:
            error = classify(exc)
            if error is None:
                raise
            raise error from None
    return wrapper


# ------------------------------------------------------------------------------------------------------------------------- what the agent gives
def _text_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ToolError("bad_input", f"{name} must be text of 1 to 80 printable characters")
    return value


def _whole(value: Any, name: str, low: int = 1, high: int = 1_000_000) -> int:
    from hokma.quant import limits

    number = limits.as_int(name, value)
    if not low <= number <= high:
        raise ToolError("bad_input", f"{name} must be a whole number from {low} to {high}")
    return number


def _when(value: Any, name: str):
    from hokma.data.sentinels import parse_time

    try:
        return parse_time(value)
    except ValueError as exc:
        raise ToolError("bad_input", f"{name}: {exc}", ["Use an ISO date such as 2025-01-31"]) from None


def _money(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ToolError("bad_input", f"{name} must be a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ToolError("bad_input", f"{name} must be a number") from None
    if not number.is_finite() or not 0 < number <= Decimal(10) ** 12:
        raise ToolError("bad_input", f"{name} must be above 0 and at most 1e12")
    return number


def _params(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_PARAMS:
        raise ToolError("bad_input", f"params is an object of at most {MAX_PARAMS} values")
    for key, item in value.items():
        if not isinstance(key, str) or not _PARAM_NAME.match(key) or isinstance(item, (dict, list, tuple)) or (isinstance(item, str) and len(item) > 128):
            raise ToolError("bad_input", f"params: {key!r} must be a lower case name with a number, text or true or false as its value")
    return dict(value)


FILL_TYPES = {"next_open_ticks": int, "stop_first": bool, "gap_fills_at_open": bool, "model": str, "reject_prob": (int, float), "trade_on_close": bool}


def _fill(value: Any, ticks: int):
    """The fill rules. The keys are the ones of `FillRules`: a forbidden choice reaches it and comes back as `forbidden_fill`."""
    from hokma.backtest.spec import FillRules

    raw = {} if value is None else value
    if not isinstance(raw, dict) or set(raw) - set(FILL_TYPES):
        raise ToolError("bad_input", f"fill is an object with the keys {', '.join(FILL_TYPES)}")
    for key, item in raw.items():
        wrong = isinstance(item, bool) != (FILL_TYPES[key] is bool) or not isinstance(item, FILL_TYPES[key])
        if wrong:
            raise ToolError("bad_input", f"fill.{key} has the wrong type")
    return FillRules(**{"next_open_ticks": ticks, **raw})


def _line_of(strategy_id: str, version: int, asked: Any) -> str:
    """The line of the strategy: the one of its saved spec. A strategy with no saved spec needs the line given."""
    from hokma.desks.core.signal import LINES
    from hokma.factory import spec as factory_spec

    try:
        saved = factory_spec.load(strategy_id, version).line
    except factory_spec.BadSpec:
        saved = None
    if saved is not None:
        if asked not in (None, saved):
            raise ToolError("bad_input", f"line_mismatch: the saved spec of {strategy_id} version {version} is for the line {saved}")
        return saved
    if asked not in LINES:
        raise ToolError("bad_input", f"{strategy_id} version {version} has no saved spec: give line, one of {', '.join(LINES)}")
    return asked


def build_spec(a: Mapping[str, Any]):
    """The `BacktestSpec` of the arguments. The cut in time is the end of the run, the line comes from the saved spec and the fill rules are checked
    by `FillRules` itself."""
    from hokma.backtest.spec import BARS, CostModel, BacktestSpec
    from hokma.broker import fees
    from hokma.quant import limits

    strategy_id, version = _text_id(a["strategy_id"], "strategy_id"), _whole(a["version"], "version", 1, 100_000)
    instruments = a["instruments"]
    if not isinstance(instruments, list) or not instruments:
        raise ToolError("bad_input", "instruments is a list of contract ids")
    limits.check_param("n_instruments", len(instruments))
    if not all(isinstance(x, str) and _CONTRACT.match(x) and ".." not in x for x in instruments):
        raise ToolError("bad_input", "an instrument is a canonical contract id such as CONT:NQ or FUT:NQ:2026-12")
    start, end = _when(a["start"], "start"), _when(a["end"], "end")
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        raise ToolError("bad_input", f"a run covers at most {MAX_RANGE_DAYS} days")
    if a["bar"] not in BARS:
        raise ToolError("bad_input", f"bar is one of {', '.join(BARS)}")
    if a["fee_table"] not in fees.tables():
        raise ToolError("bad_input", f"fee_table is one of {', '.join(fees.tables())}")
    ticks = _whole(a.get("slippage_ticks", 1), "slippage_ticks", 0, 100)
    return BacktestSpec(strategy_id, version, _params(a.get("params")), _line_of(strategy_id, version, a.get("line")), tuple(instruments), a["bar"], start, end,
                        end, _money(a["capital_usd"], "capital_usd"), CostModel(a["fee_table"], ticks, a.get("impact", "none")), _fill(a.get("fill"), ticks),
                        finalize=a.get("finalize", "mark_to_market"), session=a.get("session"), seed=_whole(a.get("seed", 0), "seed", 0, 2**31 - 1))


def build_space(raw: Any):
    """The search space of the arguments: axes and constraints as data, never code."""
    from hokma.backtest.optimize import MAX_AXES, Axis, BadSpace, Constraint, ParamSpace

    if not isinstance(raw, dict) or set(raw) - {"axes", "constraints"} or not isinstance(raw.get("axes"), list):
        raise ToolError("bad_input", "space is an object with axes and optionally constraints")
    axes, rules = raw["axes"], raw.get("constraints") or []
    if not 1 <= len(axes) <= MAX_AXES or not isinstance(rules, list) or len(rules) > MAX_AXES:
        raise ToolError("bad_input", f"a space has 1 to {MAX_AXES} axes and at most {MAX_AXES} constraints")
    try:
        built = []
        for axis in axes:
            if not isinstance(axis, dict) or set(axis) - {"name", "kind", "low", "high", "step", "choices"}:
                raise BadSpace("an axis is an object with name, kind, low, high, step and choices")
            built.append(Axis(axis["name"], axis.get("kind", "int"), axis.get("low"), axis.get("high"), axis.get("step"), tuple(axis.get("choices") or ())))
        checks = []
        for rule in rules:
            if not isinstance(rule, dict) or set(rule) != {"left", "op", "right"}:
                raise BadSpace("a constraint is an object with left, op and right")
            checks.append(Constraint(rule["left"], rule["op"], rule["right"]))
        return ParamSpace(tuple(built), tuple(checks))
    except (BadSpace, KeyError, TypeError) as exc:
        raise ToolError("bad_input", f"space: {exc}") from None


_SLOTS: threading.BoundedSemaphore | None = None
_SLOTS_LOCK = threading.Lock()


@contextmanager
def _slot():
    """At most `limits.MAX_OPEN_JOBS` backtests of one process run at once; one more is told to wait, never queued without a bound."""
    global _SLOTS
    from hokma.quant import limits

    with _SLOTS_LOCK:
        if _SLOTS is None:
            _SLOTS = threading.BoundedSemaphore(limits.MAX_OPEN_JOBS)
    if not _SLOTS.acquire(blocking=False):
        raise ToolError("unavailable", f"{limits.MAX_OPEN_JOBS} backtests are already running: try again in a minute")
    try:
        yield
    finally:
        _SLOTS.release()


def services():
    """The services the tools run on: the registry of the kit and the default database, bars and curve folder. A test replaces this function."""
    from hokma.backtest import runner
    from hokma.factory.spec import default_registry

    return runner.Services(registry=default_registry())


# ------------------------------------------------------------------------------------------------------------------------- the work of each tool
def _summary(result: Any) -> dict[str, Any]:
    return {"run_id": result.run_id, "status": result.status, "spec_hash": result.spec_hash, "data_hash": result.data_hash,
            "engine_version": result.engine_version, "seed": result.seed, "trades": len(result.trades),
            "realized_pnl_usd": str(result.realized_pnl_usd), "mtm_pnl_usd": str(result.mtm_pnl_usd),
            "costs_usd": {k: str(v) for k, v in result.costs_usd.items()}, "ambiguous_bars": result.ambiguous_bars, "rejected": dict(result.rejected),
            "stats": {k: v.to_dict() for k, v in result.stats.items()}, "warnings": list(result.warnings)}


def _run(**a: Any) -> dict[str, Any]:
    from hokma import backtest

    spec = build_spec(a)
    with _slot():
        return _summary(backtest.run(spec, services=services()))


def _row(run_id: int) -> dict[str, Any]:
    with services().database().read() as u:
        row = u.session.one("SELECT id, parent_id, kind, status, strategy_id, strategy_version, spec_hash, data_hash, engine_version, seed, error, "
                            "summary_json, started_at, finished_at FROM backtest_runs WHERE id = :i", {"i": run_id})
    if row is None:
        raise ToolError("no_data", f"there is no run {run_id}", ["backtest_run"])
    return dict(row)


def _status(**a: Any) -> dict[str, Any]:
    import dataclasses

    if ("run_id" in a) == ("job_id" in a):
        raise ToolError("bad_input", "give run_id or job_id, one of them")
    if "job_id" in a:
        from hokma import backtest

        found = backtest.status(_whole(a["job_id"], "job_id"), db=services().database())
        return {"job_id": found.job_id, "kind": found.kind, "state": found.state.value, "total": found.total, "done": found.done, "failed": found.failed,
                "reason": found.reason, "workers_lost": found.workers_lost, "status": found.state.value, "packages": [dataclasses.asdict(p) for p in found.results[:MAX_TOP]]}
    row = _row(_whole(a["run_id"], "run_id"))
    return {"run_id": row["id"], "kind": row["kind"], "status": row["status"], "strategy_id": row["strategy_id"], "strategy_version": row["strategy_version"],
            "parent_id": row["parent_id"], "spec_hash": row["spec_hash"], "data_hash": row["data_hash"], "engine_version": row["engine_version"], "seed": row["seed"],
            "error": row["error"], "started_at": row["started_at"], "finished_at": row["finished_at"]}


def _result(**a: Any) -> dict[str, Any]:
    from hokma.backtest import results, walkforward

    run_id = _whole(a["run_id"], "run_id")
    wanted = bool(a.get("report", False))
    limit = _whole(a.get("max_trades", 50), "max_trades", 0, MAX_TRADES)
    row = _row(run_id)
    if row["kind"] != "run":
        if wanted:
            raise ToolError("bad_input", f"run {run_id} is a {row['kind']}: only a backtest run has a report")
        return {"run_id": run_id, "kind": row["kind"], "status": row["status"], "summary": json.loads(row["summary_json"] or "null")}
    if row["status"] not in ("done", "account_blown"):
        return {"run_id": run_id, "kind": "run", "status": row["status"], "error": row["error"]}
    svc = services()
    result = walkforward.load_result(svc.database(), run_id, curve_dir=svc.curves())
    if wanted:
        return {"run_id": run_id, "format": "html", "html": results.report_html(result, title=f"{row['strategy_id']} version {row['strategy_version']} run {run_id}")}
    saved = results.to_dict(result)
    equity = result.equity.equity_usd
    return {**_summary(result), "kind": "run", "trade_rows": saved["trades"][:limit], "trades_total": len(result.trades),
            "equity": {"points": len(equity), "first": str(equity[0]) if equity else None, "last": str(equity[-1]) if equity else None,
                       "lowest": str(min(equity)) if equity else None}}


def _optimize(**a: Any) -> dict[str, Any]:
    from hokma import backtest
    from hokma.backtest.optimize import METHODS, OBJECTIVES
    from hokma.quant import limits

    spec, space = build_spec(a), build_space(a["space"])
    method, objective = a["method"], a["objective"]
    if method not in METHODS or objective not in OBJECTIVES:
        raise ToolError("bad_input", f"method is one of {', '.join(METHODS)} and objective one of {', '.join(OBJECTIVES)}")
    trials = _whole(a["n_trials"], "n_trials", 1, 1_000_000)
    limits.check_param("n_combinations" if method == "grid" else "opt_trials", trials)
    presweep = a.get("presweep", True)
    if not isinstance(presweep, bool):
        raise ToolError("bad_input", "presweep is true or false")
    with _slot():
        found = backtest.optimize(spec, space, method=method, n_trials=trials, objective=objective, presweep=presweep, services=services())
    scored = sorted((t for t in found.trials if t.objective is not None), key=lambda t: -t.objective)
    return {"run_id": found.run_id, "status": "done", "strategy_id": found.strategy_id, "method": found.method, "objective": found.objective,
            "best": None if found.best is None else found.best.to_dict(), "n_trials": found.n_trials, "trials_run": len(found.trials),
            "failed_trials": sum(t.status == "failed" for t in found.trials), "cache_hits": found.cache_hits, "edge_of_space": list(found.edge_of_space),
            "warnings": list(found.warnings), "presweep": found.presweep, "seed": found.seed, "top": [t.to_dict() for t in scored[:MAX_TOP]]}


def _walkforward(**a: Any) -> dict[str, Any]:
    from hokma import backtest
    from hokma.quant import limits

    spec, space = build_spec(a), build_space(a["space"])
    scheme = a["scheme"]
    train, test = _whole(a["train"], "train"), _whole(a["test"], "test")
    purge, embargo = _whole(a["purge"], "purge", 0), _whole(a["embargo"], "embargo", 0)
    limits.check_inputs({"train_size": train, "test_size": test, "window": max(purge, embargo)})
    if scheme not in ("rolling", "anchored", "cpcv"):
        raise ToolError("bad_input", "scheme is one of rolling, anchored, cpcv")
    with _slot():
        found = backtest.walk_forward(spec, space, scheme=scheme, train=train, test=test, purge=purge, embargo=embargo, services=services())
    body = found.to_dict()
    windows = body.pop("windows")
    return {**body, "status": "done", "windows_total": len(windows), "windows": windows[:MAX_WINDOWS]}


def _montecarlo(**a: Any) -> dict[str, Any]:
    from hokma import backtest
    from hokma.quant import limits

    run_id, n = _whole(a["run_id"], "run_id"), _whole(a["n"], "n")
    limits.check_param("runs", n)
    if a["method"] not in ("bootstrap", "permutation", "placebo"):
        raise ToolError("bad_input", "method is one of bootstrap, permutation, placebo")
    row = _row(run_id)
    if row["kind"] != "run":
        raise ToolError("bad_input", f"run {run_id} is a {row['kind']}: a Monte Carlo resamples a backtest run")
    if a.get("strategy_id") not in (None, row["strategy_id"]) or a.get("version") not in (None, row["strategy_version"]):
        raise ToolError("bad_input", f"run {run_id} belongs to {row['strategy_id']} version {row['strategy_version']}")
    with _slot():
        found = backtest.monte_carlo(run_id, n=n, method=a["method"], seed=_whole(a["seed"], "seed", 0, 2**31 - 1), services=services())
    body = found.to_dict()
    return {**{k: v for k, v in body.items() if k != "run_id"}, "of_run_id": body["run_id"], "status": "done"}


def _evidence(**a: Any) -> dict[str, Any]:
    from hokma import backtest

    with _slot():
        return backtest.evidence(_text_id(a["strategy_id"], "strategy_id"), _whole(a["version"], "version", 1, 100_000), services=services()).judge_payload()


# ------------------------------------------------------------------------------------------------------------------------- Ian
_HANDLERS: dict[str, Callable[..., Any]] = {"backtest_run": _run, "backtest_status": _status, "backtest_result": _result, "backtest_optimize": _optimize,
                                            "backtest_walkforward": _walkforward, "backtest_montecarlo": _montecarlo, "backtest_evidence": _evidence}
_SESSIONS: dict[tuple[Any, ...], Any] = {}


def is_researcher(caller: Any) -> bool:
    from hokma.factory.researcher import AGENT_SLUG

    return getattr(caller, "agent", None) == AGENT_SLUG


def research_session(caller: Any):
    """The `ResearchRun` of this session of Ian: his tools, his budget of attempts and the run ids that are fresh for his claim guard. The server
    process lives as long as the session, so the table is only trimmed, never read across processes."""
    from hokma.factory.researcher import ResearchRun

    key = (caller.agent, caller.conversation_id, caller.task_id)
    found = _SESSIONS.get(key)
    if found is None:
        if len(_SESSIONS) >= MAX_SESSIONS:
            _SESSIONS.pop(next(iter(_SESSIONS)))
        found = _SESSIONS[key] = ResearchRun(handlers=_HANDLERS)
    return found


def _bank_target(args: dict) -> dict:
    """A run tool names its strategy by strategy_id and version, or by bank_id: the strategy of the bank, with the id and the saved spec of its last version. A bank
    strategy with no saved spec (no code yet, or a rule pending) has nothing to run and the answer says so."""
    if args.get("code") is not None:
        found = re.match(r"^STR-(\d{3,})$", str(args["code"]))
        if found is None:
            raise ToolError("bad_input", "code is like STR-001")
        args = {**{k: v for k, v in args.items() if k != "code"}, "bank_id": int(found.group(1))}
    if args.get("bank_id") is None:
        if "strategy_id" not in args or "version" not in args:
            raise ToolError("bad_input", "give strategy_id and version, or bank_id")
        return args
    bank_id = _whole(args["bank_id"], "bank_id")
    with services().database().read() as u:
        row = u.session.one("SELECT strategy_id, spec_version FROM strategy_bank_versions WHERE bank_id = :b AND strategy_id IS NOT NULL ORDER BY version DESC LIMIT 1", {"b": bank_id})
        exists = u.session.one("SELECT 1 AS x FROM strategy_bank WHERE id = :b", {"b": bank_id}) is not None
    if row is None:
        raise ToolError("no_data" if exists else "bad_input", f"the strategy {bank_id} of the bank has no saved spec to run: it needs its code approved or its spec saved" if exists
                        else f"there is no strategy {bank_id} in the bank")
    given = {"strategy_id": args.get("strategy_id"), "version": args.get("version")}
    if any(v is not None and v != found for v, found in ((given["strategy_id"], row["strategy_id"]), (given["version"], int(row["spec_version"])))):
        raise ToolError("bad_input", f"bank_id {bank_id} is {row['strategy_id']} version {row['spec_version']}: give them or leave them out")
    rest = {k: v for k, v in args.items() if k != "bank_id"}
    return {**rest, "strategy_id": row["strategy_id"], "version": int(row["spec_version"])}


def _call(caller: Any, name: str, args: dict) -> str:
    """Run the tool. Ian's calls pass through his `ResearchRun` (the gate with the tests, the budget, the fresh run ids); every other caller runs it directly."""
    if name in ("backtest_run", "backtest_optimize", "backtest_walkforward"):
        args = _bank_target(args)
    if is_researcher(caller):
        if name == "backtest_montecarlo":                         # the guard knows a run by its strategy and version: the ones of the run itself
            row = _row(_whole(args["run_id"], "run_id"))
            args = {**args, "strategy_id": row["strategy_id"], "version": row["strategy_version"]}
        return _dump(research_session(caller).tool(name, **args))
    return _dump(_HANDLERS[name](**args))


def loop_score(objective: str = "sharpe", *, services_of: Callable[[], Any] | None = None):
    """The judge function of `factory.research_loop.ResearchLoop`: one engine run of the spec through the same evaluator that `optimize` uses
    (`optimize.engine_evaluator` over the services), which records no trial. The loop writes the trial of each experiment with its decision, so a
    score that went through `backtest.run` would count every experiment twice in the deflated Sharpe ratio."""
    from hokma.backtest import optimize
    from hokma.factory.research_loop import LoopEvaluation

    def score(spec: Any) -> Any:
        svc = (services_of or services)()
        data = svc.load(spec)
        found = optimize.engine_evaluator(spec, lambda params: svc.driver(spec, data, params), data.bars, objective)({})
        return LoopEvaluation(found.objective, spec_hash=spec.fingerprint())
    return score


# ------------------------------------------------------------------------------------------------------------------------- the tools
RUN_PROPS: dict[str, Any] = {
    "strategy_id": _s("Id of the strategy (the one of its saved spec, or of the registry). Not needed with bank_id."), "version": _i("Version of the saved spec; 1 for a registry strategy. Not needed with bank_id."),
    "bank_id": _i("Instead of strategy_id and version: the strategy of the bank, run with the id and the saved spec of its last version."),
    "code": _s("Or the code of the strategy, like STR-001 (its bank id): the last approved version."),
    "line": _s("Only for a strategy with no saved spec: DT, SWC, SWO, LP or RF. A saved spec fixes it."),
    "instruments": {"type": "array", "items": {"type": "string"}, "description": "Canonical contract ids, for example CONT:NQ."},
    "bar": _s("Bar size; the line decides which are allowed.", enum=["1m", "5m", "15m", "1h", "1d", "1w"]),
    "start": _s("ISO date of the first bar."), "end": _s("ISO date of the end; nothing after it is read."), "capital_usd": {"type": ["number", "string"], "description": "Starting capital in USD."},
    "fee_table": _s("Commission table of a broker, the one the simulated broker reads, for example schwab."),
    "slippage_ticks": _i("Ticks against on every fill (default 1); equal to fill.next_open_ticks."),
    "impact": _s("Market impact model.", enum=["none", "volume_share", "sqrt"]),
    "params": _o("Parameters of the strategy that override its defaults."), "seed": _i("Seed of everything random; the same spec and seed give the same result."),
    "finalize": _s("What happens to a position still open at the end.", enum=["mark_to_market", "close_at_end"]),
    "session": _s("Trade only in this session.", enum=["ETH", "RTH"]),
    "fill": _o("Fill rules: next_open_ticks, stop_first, gap_fills_at_open, model, reject_prob. A best price fill, a fill at the close of the signal bar or "
               "the stop after the target are forbidden and come back as forbidden_fill."),
}
RUN_REQUIRED = ["instruments", "bar", "start", "end", "capital_usd", "fee_table"]
SPACE = _o("{axes: [{name, kind int|float|choice, low, high, step, choices}], constraints: [{left, op lt|le|gt|ge|ne|eq, right}]}. Data only.")


@TOOLSET.tool("backtest_run", "Run a backtest of a strategy over history and save it. The order fills at the next open plus ticks against, the stop first when stop and target "
              "share a bar. Returns the run id, the result of the run and its statistics from the quant modules. For Ian the tests of the saved spec run first.",
              RUN_PROPS, RUN_REQUIRED, writes=True)
@safe
def backtest_run(u, c, a: dict) -> str:
    return _call(c, "backtest_run", a)


@TOOLSET.tool("backtest_status", "The state of a run (queued, running, done, failed, account_blown) or of a queued job, with its error when it failed.", {
    "run_id": _i("Run id."), "job_id": _i("Job id of a queued job; give run_id or job_id.")})
@safe
def backtest_status(u, c, a: dict) -> str:
    return _call(c, "backtest_status", a)


@TOOLSET.tool("backtest_result", "A finished run: result, costs, statistics and its first trades. With report true it returns the full report as HTML with every text escaped.", {
    "run_id": _i("Run id."), "report": _b("True for the HTML report."), "max_trades": _i(f"Trades to list, 0 to {MAX_TRADES} (default 50).")}, ["run_id"])
@safe
def backtest_result(u, c, a: dict) -> str:
    return _call(c, "backtest_result", a)


@TOOLSET.tool("backtest_optimize", "Search the parameters of a strategy: grid, TPE or refine, with a pre-sweep that ranks the points fast and confirms the best in the engine. "
              "Every try, failed ones included, is one row of the ledger of trials that the deflated Sharpe ratio counts.", {
    **RUN_PROPS, "space": SPACE, "method": _s("Search method.", enum=["grid", "tpe", "refine"]), "n_trials": _i("Tries; a grid needs at least its number of points."),
    "objective": _s("What to maximize.", enum=["net_pnl", "mtm_pnl", "sharpe"]), "presweep": _b("Rank first with the fast sweep (default true).")},
              [*RUN_REQUIRED, "space", "method", "n_trials", "objective"], writes=True)
@safe
def backtest_optimize(u, c, a: dict) -> str:
    out = _call(c, "backtest_optimize", a)
    _leave_summary(a, out)
    return out


def _leave_summary(a: dict, out: str) -> None:
    """An optimization of a strategy of the creator leaves its summary in the folder of the strategy: results/OPT-{n}-{date}.md with the run id and the numbers. The verdict of the
    optimizer is bound to the same strategy by its id (u_str{NNN}_v{N}) and version. A problem here never changes the answer of the tool."""
    try:
        from datetime import datetime, timezone

        from hokma.factory import user_rules

        bank_id = a.get("bank_id")
        if bank_id is None and a.get("code") is not None:
            bank_id = int(str(a["code"]).split("-")[1])
        if bank_id is None:
            return
        data = json.loads(out)
        with services().database().read() as u:
            bank = u.session.one("SELECT slug FROM strategy_bank WHERE id = :b", {"b": int(bank_id)})
        folder = None if bank is None else user_rules.strategy_folder(int(bank_id), bank["slug"])
        if folder is None:
            return
        results = folder / "results"
        results.mkdir(exist_ok=True)
        number = len(list(results.glob("OPT-*.md"))) + 1
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [f"# OPT-{number}: optimization of {user_rules.code_of(int(bank_id))}", "", f"- Code: {user_rules.code_of(int(bank_id))}", f"- Bank id: {int(bank_id)}", f"- Date: {stamp}",
                 f"- Strategy id: {data.get('strategy_id')}", f"- Run id: {data.get('run_id')}", f"- Status: {data.get('status')}", f"- Method: {a.get('method')}, objective {a.get('objective')}, trials {data.get('n_trials')}",
                 f"- Best: {json.dumps(data.get('best'), default=str)}", "- Verdict: not given yet; optimizer_verdict on this strategy id and version writes it, bound to the bank id and the version", ""]
        (results / f"OPT-{number}-{stamp}.md").write_text("\n".join(lines), encoding="utf-8")
    except Exception:                                                     # noqa: BLE001
        pass


@TOOLSET.tool("backtest_walkforward", "Walk forward (rolling or anchored) or combinatorial purged validation of the points of a space: every window picks its point on its "
              "train bars and is measured on its test bars. Sizes are in bars.", {
    **RUN_PROPS, "space": SPACE, "scheme": _s("Validation scheme.", enum=["rolling", "anchored", "cpcv"]), "train": _i("Train window in bars."),
    "test": _i("Test window in bars."), "purge": _i("Bars removed between train and test."), "embargo": _i("Bars removed after the test.")},
              [*RUN_REQUIRED, "space", "scheme", "train", "test", "purge", "embargo"], writes=True)
@safe
def backtest_walkforward(u, c, a: dict) -> str:
    return _call(c, "backtest_walkforward", a)


@TOOLSET.tool("backtest_montecarlo", "Resample a finished run: bootstrap or permutation of its daily returns, or a placebo of random runs with the same trades. Seeded.", {
    "run_id": _i("Run id of a finished backtest run."), "n": _i("Paths."), "method": _s("Resampling method.", enum=["bootstrap", "permutation", "placebo"]),
    "seed": _i("Seed.")}, ["run_id", "n", "method", "seed"], writes=True)
@safe
def backtest_montecarlo(u, c, a: dict) -> str:
    return _call(c, "backtest_montecarlo", a)


@TOOLSET.tool("backtest_evidence", "The evidence bundle of a strategy version for the judge of its line: PBO, deflated Sharpe ratio (with the trials of the ledger, failed "
              "ones included), walk forward, cost in units of Sharpe and ambiguous bars. It needs a finished run and a walk forward. The backtest approves nothing.", {
    "strategy_id": _s("Strategy id."), "version": _i("Version.")}, ["strategy_id", "version"])
@safe
def backtest_evidence(u, c, a: dict) -> str:
    return _call(c, "backtest_evidence", a)


# ------------------------------------------------------------------------------------------------------------------------- premium threshold
_UNDERLYING = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,39}$")


def _underlying(value: Any) -> str:
    text = value.strip().upper() if isinstance(value, str) else ""
    if not _UNDERLYING.match(text):
        raise ToolError("bad_input", "underlying is a plain symbol such as SPY")
    return text


@TOOLSET.tool("premium_calibrate", "Measure the threshold for selling option premium with a backtest of the recorded chain, for one desk. Without underlying it measures again every "
              "underlying of the desk that has a calibrated threshold and closes the switch of the ones that no longer pass; with an underlying it calibrates it. The code decides "
              "from the evidence of the backtest: nothing you pass opens the switch. Only the owner and the monthly routine can ask. Answers calibrated, not_passed (with each "
              "reason), data_missing (no history of the chain) or unavailable.", {
    "desk_id": _i("Desk id of a SWO desk."), "underlying": _s("Symbol of the underlying, for example SPY. Leave it out to measure the calibrated ones again.")},
              ["desk_id"], writes=True)
@safe
def premium_calibrate(u, c, a: dict) -> str:
    from datetime import datetime, timezone

    from hokma.backtest.calibration import premium

    desk_id = _whole(a["desk_id"], "desk_id")
    name = None if a.get("underlying") is None else _underlying(a["underlying"])
    try:
        premium.authorize_calibration(c.agent, c.task_id, name, desk_id)
    except premium.CalibrationDenied as exc:
        raise ToolError("blocked", f"{exc}; only the owner and the running monthly routine can ask for a calibration") from None
    with _slot():
        found = premium.request_calibration(desk_id, name, as_of=datetime.now(timezone.utc))
    rows = []
    for item in found:
        body = item.to_dict()
        body["measures"] = {k: v for k, v in body["measures"].items() if k not in ("walk_forward", "thresholds")}
        rows.append(body)
    return _dump({"results": rows})


@TOOLSET.tool("premium_switch_state", "Read whether the switch for selling option premium is open for a desk: for one underlying, or for every underlying that has a calibrated "
              "threshold. It shows the metric, the threshold and the backtest run that proved it, or the reason it is closed. It only reads.", {
    "desk_id": _i("Desk id."), "underlying": _s("Symbol of one underlying; leave it out for every calibrated one.")}, ["desk_id"])
@safe
def premium_switch_state(u, c, a: dict) -> str:
    from hokma.backtest.calibration import premium
    from hokma.kit import hokma_tools

    desk_id = _whole(a["desk_id"], "desk_id")
    try:
        hokma_tools._own_desk_only(c, desk_id)
    except LookupError as exc:
        raise ToolError("blocked", str(exc)) from None
    names = None if a.get("underlying") is None else [_underlying(a["underlying"])]
    return _dump({"desk_id": desk_id, "switches": premium.switch_overview(desk_id, names)})


TOOLS = TOOLSET.tools
