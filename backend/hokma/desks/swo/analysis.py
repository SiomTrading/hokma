"""Start, read and list the analysis of an asset on a SWO desk (SPEC-028, delivery 4, piece A).

An analysis is one cycle of `graph.run_cycle`. This module is the door the Analysis panel and the chat tools of the chief use:

- `start` (the panel) opens the record and runs the cycle in a thread of the app, answering with its run id at once. `request` (the chat tools)
  only opens the record and answers with its run id: the tool server is a process the engine may kill and it has no socket, so it never runs
  the cycle. The app picks the request up (`drain_requests`, in the tick the scheduler already has) and runs it in its own process.
- Limits, checked in the transaction that opens the record: one live run per desk and asset (`analysis_running`, 409, with the id of the one that
  goes), `MAX_PER_DESK` per desk and `MAX_TOTAL` in the app (`analysis_busy`, 409), and the weekly budget of the desk. At the start of the app the
  runs left `running` by a process that died are closed as failed (`recover_orphans`).
- The progress is pushed, never asked for: every step of the graph that enters or leaves, and the end of the run, is published to the
  subscribers of this module (`subscribe`). The chat socket turns each one into a `type: analysis` message for the panels that
  watch the desk (`engine.ChatEngine`). The report of a run that is still going comes from the same events, or from its checkpoints
  when the run has not started in this process yet.
- `get` and `list_runs` read the record of the runs (`run_records`) and the door of execution; nothing is saved twice.

A desk that is not a stock options desk has no cycle: the analysis is unavailable there, with its reason, and goes on in the chat of the chief.
Nothing here sends an order: the cycle hands its proposal to the door of execution, and the owner decides there.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from hokma.backoffice import checkpoint, runs
from hokma.db import Database, NotFound, get_database
from hokma.desks.agents.manager import load_desk_mandate
from hokma.desks.errors import DeskError
from hokma.desks.swo import graph
from hokma.desks.swo import report as seat_report

log = logging.getLogger("hokma.desks.swo")

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_PER_DESK = 2                     # live analyses on one desk: each is a whole cycle (about ten model calls in sequence); two at once keep the weekly
                                     # budget of the desk and the rate limit of the model provider predictable, and a third asset waits
MAX_TOTAL = 4                        # live analyses in the whole app, over every desk: the app runs on a student's computer, and four cycles
                                     # already hold four sets of data and model calls open
REQUEST_TTL = timedelta(minutes=5)   # a request of the tool server that no process picked up in this time is closed as failed
STALE = timedelta(minutes=30)        # a run of another process that opened longer ago than this is not counted as alive
MAX_SYMBOL = 20

_lock = threading.Lock()
_live: dict[tuple[int, str], int] = {}              # (desk, symbol) -> run id of the run that goes in this process
_trails: dict[int, list[str]] = {}                  # run id -> the steps it entered, for a report asked while it goes
_subscribers: list[Callable[[dict[str, Any]], None]] = []


# ---- the live feed ------------------------------------------------------------------------------------------------------------------

def subscribe(callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """Receive every event of every analysis that runs in this process. Returns the function that stops it. A callback runs in the thread of
    the cycle: it must not block."""
    with _lock:
        _subscribers.append(callback)

    def stop() -> None:
        with _lock:
            if callback in _subscribers:
                _subscribers.remove(callback)

    return stop


def _publish(event: dict[str, Any]) -> None:
    with _lock:
        targets = list(_subscribers)
    for target in targets:
        try:
            target(event)
        except Exception:                                                              # noqa: BLE001  (a panel that fails never stops a cycle)
            log.warning("a subscriber of the analysis feed failed")


def watch_key(desk_id: int) -> str:
    """The name under which the chat socket groups the panels that watch one desk."""
    return f"desk:{desk_id}"


# ---- what can be analyzed -----------------------------------------------------------------------------------------------------------

def availability(desk_id: int, *, db: Database | None = None) -> dict[str, Any]:
    """Whether the desk has an analysis: only a stock options desk runs the cycle of agents. Otherwise the reason, for the panel to show it
    disabled with its motive (the analysis then goes on in the chat of the chief)."""
    database = db or get_database()
    with database.read() as u:
        try:
            desk = u.desks.get(desk_id)
        except NotFound as exc:
            raise DeskError("not_found", 404) from exc
        row = u.session.one("SELECT line FROM desk_profile WHERE desk_id = :d", {"d": desk_id})
    if getattr(desk, "status", "") == "archived":
        return _unavailable("desk_archived")
    if row is None or row["line"] != graph.LINE:
        return _unavailable("not_options_desk")
    return {"available": True, "reason": None, "message": None}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "message": DeskError("analysis_unavailable", 422, reason=reason).message}


def require_available(desk_id: int, db: Database | None = None) -> None:
    """Refuse with the reason when the desk has no analysis (also the door of the panel: it is not added to a desk that cannot run it)."""
    _require(desk_id, db or get_database())


def _require(desk_id: int, db: Database) -> None:
    found = availability(desk_id, db=db)
    if not found["available"]:
        raise DeskError("analysis_unavailable", 422, reason=found["reason"])


def _symbol(value: Any) -> str:
    from hokma.market.yahoo import valid_symbol

    if not isinstance(value, str) or not 1 <= len(value.strip()) <= MAX_SYMBOL or not valid_symbol(value.strip()):
        raise DeskError("bad_symbol", 400)
    return value.strip().upper()


def _strategy(desk_id: int, value: Any, db: Database) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
        raise DeskError("bad_strategy", 400)
    name = value.strip()
    names = [s.name for s in load_desk_mandate(desk_id, db=db).strategies]
    if name not in names:
        raise DeskError("bad_strategy", 400)
    return name


# ---- start ---------------------------------------------------------------------------------------------------------------------------

def _alive_id(row: Mapping[str, Any], now: datetime, live: set[int]) -> bool:
    from hokma.db.timeutil import from_db_time

    started = row.get("started_at")
    return int(row["id"]) in live or not started or now - from_db_time(started) <= STALE


def _symbol_of(u, row: Mapping[str, Any]) -> str | None:
    """The asset of a run that is open: from its record (it is written when the run opens) or, for a run an older door opened, from its checkpoint."""
    entries = (json.loads(row["record_json"] or "{}").get("entries") or {})
    if entries.get("symbol"):
        return str(entries["symbol"])
    point = u.session.one("SELECT state_json FROM run_checkpoints WHERE run_id = :r ORDER BY node DESC LIMIT 1", {"r": row["id"]})
    if point is None:
        return None
    return (json.loads(point["state_json"]).get("state") or {}).get("symbol")


def _open(desk_id: int, sym: str, chosen: str | None, cut: datetime, requested: bool, db: Database) -> int:
    """Open the record of the run, refusing in the same transaction when it would pass a limit: the same asset on this desk already goes
    (`analysis_running`), this desk has `MAX_PER_DESK` runs or the app has `MAX_TOTAL` (`analysis_busy`). The count reads the records, so it
    holds for every process that opens runs (the panel's door in the app, the chat tools in the tool server)."""
    now = datetime.now(timezone.utc)
    with _lock:
        live = set(_live.values())

    def guard(u) -> None:
        rows = u.session.query("SELECT id, desk_id, started_at, record_json FROM run_records WHERE kind = :k AND status = 'running'", {"k": graph.KIND})
        alive = [r for r in rows if _alive_id(r, now, live)]
        for row in alive:
            if row["desk_id"] == desk_id and _symbol_of(u, row) == sym:
                raise DeskError("analysis_running", 409, run_id=int(row["id"]))
        if sum(1 for r in alive if r["desk_id"] == desk_id) >= MAX_PER_DESK:
            raise DeskError("analysis_busy", 409, scope="desk", limit=MAX_PER_DESK)
        if len(alive) >= MAX_TOTAL:
            raise DeskError("analysis_busy", 409, scope="total", limit=MAX_TOTAL)

    return runs.begin(desk_id, graph.KIND, signature=graph.cycle_signature(desk_id, sym, cut), entries={
        "symbol": sym, "as_of": cut.isoformat(), "strategy": chosen, "requested": requested}, guard=guard, db=db)


def assert_budget(desk_id: int, db: Database | None = None) -> None:
    """The weekly budget of the desk, for the work that is not a new analysis but spends the same models (the learning of step 9)."""
    from hokma.desks import service

    database = db or get_database()
    with database.read() as u:
        project_id = u.desks.get(desk_id).project_id
    service.assert_can_run(project_id, database)


def _admit(desk_id: int, symbol: Any, strategy: Any, database: Database) -> tuple[str, str | None]:
    """What both doors check before a run opens: the asset, that the desk has an analysis, the strategy and the weekly budget of the desk."""
    from hokma.desks import service

    sym = _symbol(symbol)
    _require(desk_id, database)
    chosen = _strategy(desk_id, strategy, database)
    with database.read() as u:
        project_id = u.desks.get(desk_id).project_id
    service.assert_can_run(project_id, database)                 # a desk over its weekly budget starts nothing new
    return sym, chosen


def request(desk_id: int, symbol: Any, strategy: Any = None, *, as_of: datetime | None = None, db: Database | None = None) -> dict[str, Any]:
    """The door of the chat tools. The tool server is a process of its own that the engine may kill, with no socket to push progress, so it never
    runs the cycle: it opens the record of the run (counted against the limits at once) and answers with its id. The app picks the request up
    in `drain_requests` and runs the cycle in its own process, which pushes the progress to the panels."""
    database = db or get_database()
    sym, chosen = _admit(desk_id, symbol, strategy, database)
    cut = as_of or datetime.now(timezone.utc)
    run_id = _open(desk_id, sym, chosen, cut, True, database)
    return {"run_id": run_id, "desk_id": desk_id, "symbol": sym, "status": "queued", "strategy": chosen}


def start(desk_id: int, symbol: Any, strategy: Any = None, *, as_of: datetime | None = None, db: Database | None = None) -> dict[str, Any]:
    """The door of the panel, in the app: opens the record, runs the cycle in a thread and answers at once with the run id."""
    from hokma.kit import desk_tools

    database = db or get_database()
    sym, chosen = _admit(desk_id, symbol, strategy, database)
    desk_tools.ensure_runtime()
    try:
        runtime = graph.preflight(desk_id, database)             # a desk with no runtime or no mandate is refused before a run opens
    except graph.CycleError as exc:
        raise _refusal(exc) from exc
    cut = as_of or datetime.now(timezone.utc)
    run_id = _open(desk_id, sym, chosen, cut, False, database)
    _spawn(run_id, desk_id, sym, chosen, cut, database, runtime)
    return {"run_id": run_id, "desk_id": desk_id, "symbol": sym, "status": "running", "strategy": chosen}


def _spawn(run_id: int, desk_id: int, sym: str, chosen: str | None, cut: datetime, database: Database, runtime: Any = None) -> None:
    """Run the cycle of an open record in a thread of this process (the only process that pushes progress)."""
    key = (desk_id, sym)
    with _lock:
        _live[key] = run_id
        _trails[run_id] = []

    def listener(event: dict[str, Any]) -> None:
        if event["event"] == "step" and event["phase"] == "enter":
            with _lock:
                _trails.setdefault(event["run_id"], []).append(event["node"])
        _publish(event)

    def work() -> None:
        try:
            rt = runtime
            if rt is None:
                from hokma.kit import desk_tools

                desk_tools.ensure_runtime()
                rt = graph.preflight(desk_id, database)
            graph.run_cycle(desk_id, symbol=sym, as_of=cut, strategy=chosen, runtime=rt, db=database, listener=listener, run_id=run_id)
        except BaseException as exc:                                                    # noqa: BLE001  (the record says it failed)
            log.warning("the analysis of %s on desk %s ended in an error: %s", sym, desk_id, graph.explain(exc))
            if _fail(run_id, sym, graph.explain(exc), database):
                _publish({"event": "failed", "desk_id": desk_id, "run_id": run_id, "symbol": sym, "error": graph.explain(exc)})
        finally:
            with _lock:
                if _live.get(key) == run_id:
                    _live.pop(key, None)
                _trails.pop(run_id, None)

    threading.Thread(target=work, name=f"analysis-{desk_id}-{sym}", daemon=True).start()


def _fail(run_id: int, sym: str, why: str, db: Database) -> bool:
    """Close a run that is still open as failed. False when it was already closed (the cycle closes its own record when the graph breaks)."""
    try:
        runs.finish(run_id, "failed", entries={"symbol": sym}, outputs={"reason": "analysis_error", "error": why}, db=db)
    except runs.RunClosed:
        return False
    return True


def drain_requests(db: Database | None = None, *, now: datetime | None = None) -> list[int]:
    """Start the cycle of every request the tool server opened and nobody runs yet. Called by the scheduler of the app in the tick it already has
    (no timer of its own); the answer is the ids it started. A request older than `REQUEST_TTL` is closed as failed: nothing picked it up."""
    from hokma.db.timeutil import from_db_time

    database = db or get_database()
    cut = now or datetime.now(timezone.utc)
    with _lock:
        live = set(_live.values())
    started: list[int] = []
    for row in runs.list_runs(kind=graph.KIND, status="running", limit=200, db=database):
        entries = (row["record"].get("entries") or {})
        if not entries.get("requested") or row["id"] in live or row["desk_id"] is None:
            continue
        sym = str(entries.get("symbol") or "")
        if cut - from_db_time(row["started_at"]) > REQUEST_TTL:
            _fail(row["id"], sym, "no process took the request", database)
            continue
        _spawn(row["id"], row["desk_id"], sym, entries.get("strategy"), datetime.fromisoformat(entries["as_of"]), database)
        started.append(row["id"])
    return started


def recover_orphans(db: Database | None = None) -> list[int]:
    """At the start of the app nothing runs yet: every analysis still `running` belongs to a process that died (a closed app, a killed tool server).
    Close them as failed so they stop counting against the limits and stop blocking their asset. A run this process runs itself (`_live`) is left alone."""
    database = db or get_database()
    closed: list[int] = []
    with _lock:
        live = set(_live.values())
    for row in runs.list_runs(kind=graph.KIND, status="running", limit=500, db=database):
        if row["id"] in live:                                              # one this very process is running is not an orphan
            continue
        entries = (row["record"].get("entries") or {})
        if _fail(row["id"], str(entries.get("symbol") or ""), "the process that ran it ended", database):
            closed.append(row["id"])
    return closed


def _refusal(error: BaseException) -> DeskError:
    if isinstance(error, graph.CycleError):
        return DeskError("analysis_unavailable", 422, reason=error.code, detail=str(error)[:300])
    return DeskError("analysis_unavailable", 503, reason=type(error).__name__, detail=graph.explain(error)[:300])


# ---- read ---------------------------------------------------------------------------------------------------------------------------

def _decision(record: Mapping[str, Any], db: Database) -> dict[str, Any] | None:
    """What became of the proposal of the run at the door of execution: the decision to approve or reject, its state, strategy and account."""
    orders = ((record.get("record") or {}).get("outputs") or {}).get("orders") or []
    if not orders:
        return None
    with db.read() as u:
        row = u.session.one("SELECT id, decision_id, status, code, level, strategy_key, account_id FROM exec_proposals WHERE id = :i AND desk_id = :d",
                            {"i": int(orders[0]["proposal_id"]), "d": record["desk_id"]})
    if row is None:
        return None
    return {"proposal_id": row["id"], "decision_id": row["decision_id"], "status": row["status"], "code": row["code"], "level": row["level"],
            "strategy_key": row["strategy_key"], "account_id": row["account_id"], "pending": row["status"] == "awaiting_owner",
            "needs_account": row["code"] == "needs_account"}


def _own_run(desk_id: int, run_id: Any, db: Database) -> dict[str, Any]:
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise DeskError("analysis_not_found", 404)
    record = runs.get(run_id, db=db)
    if record is None or record["desk_id"] != desk_id or record["kind"] != graph.KIND:
        raise DeskError("analysis_not_found", 404)
    return record


def _progress(record: Mapping[str, Any], db: Database) -> tuple[list[str] | None, str | None]:
    """The steps a run that is still going has entered, and its asset: from this process when it runs here, from its checkpoints when not."""
    if record["status"] != "running":
        return None, None
    with _lock:
        trail = list(_trails.get(record["id"], ())) or None
    point = checkpoint.last(record["id"], db=db)
    if point is None:
        return trail, ((record.get("record") or {}).get("entries") or {}).get("symbol")
    return trail or list(point.path), point.state.get("symbol")


def get(desk_id: int, run_id: Any, *, db: Database | None = None) -> dict[str, Any]:
    """The report of one analysis: steps, grade and reasons, proposal, decision and the report of each agent."""
    database = db or get_database()
    record = _own_run(desk_id, run_id, database)
    trail, symbol = _progress(record, database)
    out = seat_report.view(record, live_trail=trail, decision=_decision(record, database))
    if not out["symbol"]:
        out["symbol"] = symbol
    return out


def list_runs(desk_id: int, limit: Any = DEFAULT_LIMIT, *, db: Database | None = None) -> dict[str, Any]:
    """The latest analyses of the desk, newest first, with whether the desk can run one."""
    database = db or get_database()
    found = availability(desk_id, db=database)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise DeskError("bad_limit", 400)
    rows = runs.list_runs(desk_id, kind=graph.KIND, limit=limit, db=database) if found["available"] else []
    items = []
    for row in rows:
        item = seat_report.brief(row)
        if not item["symbol"]:
            item["symbol"] = _progress(row, database)[1]
        items.append(item)
    return {"desk_id": desk_id, **found, "analyses": items}
