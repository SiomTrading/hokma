"""The ledger of trials (SPEC-020: V-22, L-45). Every run of one hypothesis is a trial, the failed ones included.

The deflated Sharpe ratio asks how many things were tried before the one that is shown. This module is where that number comes from:
`count` is the `n_trials` of any result, never a number that the caller of a backtest passes. The table `backtest_trials` accepts only
inserts (a trigger refuses UPDATE and DELETE in SQLite and in Postgres), so deleting a run does not lower the count.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hokma.db import Database, get_database

MAX_ID = 80
MAX_PARAMS_CHARS = 20_000
STATUSES = ("done", "failed")


class TrialError(ValueError):
    """The trial cannot be recorded. `code` is stable: bad_trial."""

    code = "bad_trial"


@dataclass(frozen=True)
class FailedTrial:
    id: int
    strategy_id: str
    hypothesis_id: str
    run_id: int | None
    params: Mapping[str, Any]
    error: str | None


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_ID:
        raise TrialError(f"{name} is text of 1 to {MAX_ID} characters")
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float) and not math.isfinite(value):
        raise TrialError("a parameter is a finite number")
    raise TypeError(f"{type(value).__name__} is not a parameter value")


def record(strategy_id: str, hypothesis_id: str, run_id: int | None, params: Mapping[str, Any], status: str, objective: float | None, *,
           error: str | None = None, db: Database | None = None) -> int:
    """Add one trial and return its id. `status` is done or failed. A failed trial keeps its error: in the run it names, or (with no
    run) inside the stored parameters. Nothing here updates or deletes a row."""
    _text("strategy_id", strategy_id)
    _text("hypothesis_id", hypothesis_id)
    if status not in STATUSES:
        raise TrialError(f"status is {' or '.join(STATUSES)}")
    if not isinstance(params, Mapping):
        raise TrialError("params is a mapping")
    if objective is not None and (isinstance(objective, bool) or not isinstance(objective, (int, float)) or not math.isfinite(objective)):
        objective = None                                     # a NaN or an infinity is no objective: the trial still counts
    body = dict(params)
    if run_id is None and error is not None:
        body = {**body, "_error": str(error)[:2000]}
    try:
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=_plain, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TrialError(f"the parameters cannot be stored: {exc}") from exc
    if len(text) > MAX_PARAMS_CHARS:
        raise TrialError(f"the parameters are at most {MAX_PARAMS_CHARS} characters")
    with (db or get_database()).write() as u:
        return u.session.insert("backtest_trials", {"strategy_id": strategy_id, "hypothesis_id": hypothesis_id, "run_id": run_id,
                                                    "params_json": text, "status": status, "objective": objective})


def count(strategy_id: str, *, hypothesis_id: str | None = None, db: Database | None = None) -> int:
    """The `n_trials` of the deflated Sharpe ratio: every trial of the strategy (or of one hypothesis of it), the failed ones included."""
    _text("strategy_id", strategy_id)
    sql, params = "SELECT COUNT(*) FROM backtest_trials WHERE strategy_id = :s", {"s": strategy_id}
    if hypothesis_id is not None:
        sql, params["h"] = f"{sql} AND hypothesis_id = :h", _text("hypothesis_id", hypothesis_id)
    with (db or get_database()).read() as u:
        return int(u.session.scalar(sql, params) or 0)


def failed(strategy_id: str, *, hypothesis_id: str | None = None, db: Database | None = None) -> tuple[FailedTrial, ...]:
    """The failed trials with their error, oldest first, for whoever designs the strategy. They are part of `count`."""
    _text("strategy_id", strategy_id)
    sql = ("SELECT t.id, t.strategy_id, t.hypothesis_id, t.run_id, t.params_json, r.error AS run_error FROM backtest_trials t "
           "LEFT JOIN backtest_runs r ON r.id = t.run_id WHERE t.strategy_id = :s AND t.status = 'failed'")
    params: dict[str, Any] = {"s": strategy_id}
    if hypothesis_id is not None:
        sql, params["h"] = f"{sql} AND t.hypothesis_id = :h", _text("hypothesis_id", hypothesis_id)
    with (db or get_database()).read() as u:
        rows = u.session.query(f"{sql} ORDER BY t.id", params)
    out = []
    for r in rows:
        body = json.loads(r["params_json"])
        stored = body.pop("_error", None)
        out.append(FailedTrial(int(r["id"]), r["strategy_id"], r["hypothesis_id"], r["run_id"], body, r["run_error"] or stored))
    return tuple(out)
