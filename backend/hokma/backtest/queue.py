"""The queue of long backtest jobs: optimization, walk-forward, replay (SPEC-020: B-27, L-47, N-37).

A job is a list of work packages. It has an id (the row of `backtest_runs` that holds it), a spec hash, a state and a result per package. The
packages run in several worker processes (`fork`: a process gets the memory of the server, which is the only way a worker can load the sensitive
packages); each worker loads the data ONCE and then takes package after package, so a thousand packages do not load a thousand times. The
results are the same for one process and for five: they are kept in the order of the packages, never in the order they finished.

What is saved, and where: the state of the job in `backtest_runs` (queued, running, degraded, failed, done); every finished package as a
checkpoint in `run_checkpoints` under a `run_records` row of kind `backtest_job` (H2), so a job that was stopped goes on from its last package;
the results of a finished job in that run record. Nothing is written by a worker and no path comes from the caller: the files are by id.

A package that raises is a failed package and the queue goes on. A worker that dies makes the job `degraded`: its package goes to another
worker (twice at most) and the job finishes. A job whose data cannot be loaded in any worker is `failed` with the reason.
"""
from __future__ import annotations

import json
import multiprocessing
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from multiprocessing.connection import wait
from typing import Any

from hokma.backoffice import checkpoint, runs
from hokma.db import Database, get_database
from hokma.db.timeutil import to_db_time, utcnow
from hokma.desks.core.hashing import content_hash
from hokma.quant import limits

KINDS = ("sweep", "optimize", "walkforward", "montecarlo", "replay", "calibration")
RECORD_KIND = "backtest_job"
MAX_ATTEMPTS = 2                                  # a package goes to a worker twice at most when its worker dies
MAX_ERROR = 500
OPEN_STATES = ("queued", "running", "degraded")


class JobState(str, Enum):
    """The states of a job (N-37). `degraded` is a job that lost a process and went on."""
    QUEUED = "queued"
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"
    DONE = "done"


class QueueError(ValueError):
    """The job cannot be queued or run as asked. `code` is stable: bad_job, unknown_job, too_many_jobs."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class PackageResult:
    index: int
    status: str                       # done or failed
    value: Any = None
    error: str | None = None


@dataclass(frozen=True)
class JobStatus:
    job_id: int
    kind: str
    spec_hash: str
    state: JobState
    total: int
    done: int
    failed: int
    results: tuple[PackageResult, ...]
    reason: str | None = None         # why a job is failed or degraded
    workers_lost: int = 0


# ---------------------------------------------------------------------------------------------------------------------------- the job rows
def _packages_json(packages: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps({"packages": [dict(p) for p in packages]}, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def submit(kind: str, packages: Sequence[Mapping[str, Any]], *, strategy_id: str | None = None, seed: int = 0, db: Database | None = None) -> int:
    """Queue a job of `packages` (plain JSON values) and return its id. At most `limits.MAX_OPEN_JOBS` jobs are open at once and at most
    `n_packages` packages are in one job."""
    if kind not in KINDS:
        raise QueueError("bad_job", f"kind is one of {', '.join(KINDS)}")
    limits.check_param("n_packages", len(packages))
    if not packages or not all(isinstance(p, Mapping) for p in packages):
        raise QueueError("bad_job", "a job has at least one package, each a mapping of plain values")
    text = _packages_json(packages)
    digest = content_hash({"kind": kind, "packages": json.loads(text)["packages"], "seed": int(seed)})
    database = db or get_database()
    with database.write() as u:
        marks = ", ".join(f":s{i}" for i in range(len(OPEN_STATES)))
        open_jobs = u.session.scalar(f"SELECT COUNT(*) FROM backtest_runs WHERE kind IN ({', '.join(repr(k) for k in KINDS)}) AND status IN ({marks})",
                                     {f"s{i}": s for i, s in enumerate(OPEN_STATES)})
        if int(open_jobs or 0) >= limits.MAX_OPEN_JOBS:
            raise QueueError("too_many_jobs", f"{limits.MAX_OPEN_JOBS} jobs are open: wait for one to finish")
        return u.session.insert("backtest_runs", {"kind": kind, "strategy_id": strategy_id, "spec_json": text, "spec_hash": digest,
                                                  "engine_version": "queue", "seed": int(seed), "status": JobState.QUEUED.value})


def _row(db: Database, job_id: int) -> dict[str, Any]:
    with db.read() as u:
        row = u.session.one("SELECT * FROM backtest_runs WHERE id = :i", {"i": int(job_id)})
    if row is None or row["kind"] not in KINDS:
        raise QueueError("unknown_job", f"job {job_id} does not exist")
    return row


def _signature(row: Mapping[str, Any]) -> str:
    return f"job:{row['id']}:{row['spec_hash']}"


def _record(db: Database, row: Mapping[str, Any]) -> dict[str, Any] | None:
    with db.read() as u:
        return u.session.one("SELECT id, status FROM run_records WHERE kind = :k AND signature = :s ORDER BY id DESC LIMIT 1",
                             {"k": RECORD_KIND, "s": _signature(row)})


def _checkpoints(db: Database, record_id: int) -> dict[int, PackageResult]:
    with db.read() as u:
        rows = u.session.query("SELECT node, state_json FROM run_checkpoints WHERE run_id = :r ORDER BY node", {"r": record_id})
    found: dict[int, PackageResult] = {}
    for r in rows:
        state = runs.decode(json.loads(r["state_json"]))["state"]
        found[int(state["index"])] = PackageResult(int(state["index"]), state["status"], state.get("value"), state.get("error"))
    return found


def status(job_id: int, *, db: Database | None = None) -> JobStatus:
    """The state of a job and what it has finished so far (every package result, in the order of the packages)."""
    database = db or get_database()
    row = _row(database, job_id)
    record = _record(database, row)
    found = {} if record is None else _checkpoints(database, record["id"])
    total = len(json.loads(row["spec_json"])["packages"])
    ordered = tuple(found[k] for k in sorted(found))
    summary = json.loads(row["summary_json"]) if row["summary_json"] else {}
    return JobStatus(int(row["id"]), row["kind"], row["spec_hash"], JobState(row["status"]), total, sum(1 for r in ordered if r.status == "done"),
                     sum(1 for r in ordered if r.status == "failed"), ordered, row["error"], int(summary.get("workers_lost", 0)))


def _set(db: Database, job_id: int, state: JobState, *, reason: str | None = None, summary: Mapping[str, Any] | None = None, finished: bool = False) -> None:
    values: dict[str, Any] = {"status": state.value, "error": None if reason is None else reason[:2000]}
    if summary is not None:
        values["summary_json"] = json.dumps(dict(summary), sort_keys=True)
    if state is JobState.RUNNING:
        values["started_at"] = to_db_time(utcnow())
    if finished:
        values["finished_at"] = to_db_time(utcnow())
    with db.write() as u:
        u.session.update("backtest_runs", {"id": int(job_id)}, values)


# ---------------------------------------------------------------------------------------------------------------------------- the workers
def _worker(conn: Any, loader: Callable[[], Any], work: Callable[[Any, Mapping[str, Any]], Any]) -> None:
    """The body of a worker process: load the data once, then take packages until told to stop."""
    try:
        data = loader()
    except BaseException as exc:                      # noqa: BLE001 - the parent has to know why this worker cannot work
        conn.send(("load_failed", f"{type(exc).__name__}: {exc}"[:MAX_ERROR]))
        return
    conn.send(("ready", os.getpid()))
    while True:
        message = conn.recv()
        if message is None:
            return
        index, payload = message
        try:
            conn.send(("done", index, work(data, payload)))
        except Exception as exc:                      # noqa: BLE001 - a package that raises is a failed package, the worker goes on
            conn.send(("failed", index, f"{type(exc).__name__}: {exc}"[:MAX_ERROR]))


class _Worker:
    def __init__(self, ctx: Any, loader: Callable[[], Any], work: Callable[[Any, Mapping[str, Any]], Any]) -> None:
        self.conn, child = ctx.Pipe()
        self.process = ctx.Process(target=_worker, args=(child, loader, work), daemon=True)
        self.process.start()
        child.close()
        self.ready, self.package = False, None


def _serial(packages: Sequence[tuple[int, Mapping[str, Any]]], loader: Callable[[], Any], work: Callable[[Any, Mapping[str, Any]], Any],
            on_result: Callable[[PackageResult], bool]) -> str | None:
    """The same loop in this process, for a machine that cannot fork. Returns the reason the data could not be loaded, or None."""
    try:
        data = loader()
    except Exception as exc:                          # noqa: BLE001
        return f"data_load_failed: {type(exc).__name__}: {exc}"[:MAX_ERROR]
    for index, payload in packages:
        try:
            result = PackageResult(index, "done", work(data, payload))
        except Exception as exc:                      # noqa: BLE001
            result = PackageResult(index, "failed", None, f"{type(exc).__name__}: {exc}"[:MAX_ERROR])
        if not on_result(result):
            break
    return None


def _pool(packages: Sequence[tuple[int, Mapping[str, Any]]], loader: Callable[[], Any], work: Callable[[Any, Mapping[str, Any]], Any], workers: int,
          on_result: Callable[[PackageResult], bool]) -> tuple[str | None, int]:
    """Run `packages` in `workers` processes. `on_result` is called in this process for each finished package and answers False to stop.
    Returns (the reason no worker could load the data, or None; the number of workers lost)."""
    ctx = multiprocessing.get_context("fork")
    queue = list(packages)
    attempts: dict[int, int] = {}
    lost, respawns, load_reason = 0, 0, None
    live = [_Worker(ctx, loader, work) for _ in range(min(workers, len(queue)))]
    stopping = False
    try:
        while live:
            idle = [w for w in live if w.ready and w.package is None]
            for w in idle:
                if stopping or not queue:
                    continue
                w.package = queue.pop(0)
                w.conn.send((w.package[0], w.package[1]))
            if not queue and all(w.package is None for w in live) or stopping and all(w.package is None for w in live):
                break
            ready = wait([w.conn for w in live] + [w.process.sentinel for w in live])
            for w in list(live):
                dead = w.process.sentinel in ready and not w.conn.poll()
                if w.conn in ready or w.conn.poll():
                    try:
                        message = w.conn.recv()
                    except (EOFError, OSError):
                        dead = True
                        message = None
                    if message is not None:
                        if message[0] == "ready":
                            w.ready = True
                        elif message[0] == "load_failed":
                            load_reason = f"data_load_failed: {message[1]}"
                            live.remove(w)
                            lost += 1                                       # a worker that cannot load its data is a lost worker
                            w.process.join(1)
                            continue
                        else:
                            kind, index, value = message
                            w.package = None
                            result = PackageResult(index, "done", value) if kind == "done" else PackageResult(index, "failed", None, str(value))
                            if not on_result(result):
                                stopping = True
                        continue
                if dead or not w.process.is_alive():
                    live.remove(w)
                    lost += 1
                    if w.package is not None:
                        index, payload = w.package
                        attempts[index] = attempts.get(index, 0) + 1
                        if attempts[index] >= MAX_ATTEMPTS:
                            on_result(PackageResult(index, "failed", None, "worker_lost: the process of the package died twice"))
                        else:
                            queue.insert(0, w.package)
                    if (queue or any(x.package for x in live)) and respawns < workers:
                        respawns += 1
                        live.append(_Worker(ctx, loader, work))
        if queue and not stopping and load_reason is None and lost:
            load_reason = "worker_lost: no process is left to take the packages"
    finally:
        for w in live:
            try:
                w.conn.send(None)
            except (OSError, BrokenPipeError):
                pass
        for w in live:
            w.process.join(2)
            if w.process.is_alive():
                w.process.terminate()
    return (load_reason if queue and not stopping else None), lost


def run_job(job_id: int, work: Callable[[Any, Mapping[str, Any]], Any], loader: Callable[[], Any], *, workers: int = 1, budget: int | None = None,
            db: Database | None = None) -> JobStatus:
    """Run a queued job, or go on with one that was stopped, from its last checkpoint. `loader()` runs once in each worker and its value is the
    first argument of `work(data, package)`, which returns a plain value. `budget` is how many packages to finish in this call before no new
    one is taken (those already running finish): the job stays `running` and a later call goes on. The job ends `done`, `degraded` (a worker was lost and the job finished) or `failed` (no worker
    could load the data, with the reason)."""
    limits.check_param("n_workers", workers)
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise QueueError("bad_job", "workers is a whole number of at least 1")
    database = db or get_database()
    row = _row(database, job_id)
    if row["status"] in (JobState.DONE.value, JobState.DEGRADED.value, JobState.FAILED.value) and _finished(database, row):
        return status(job_id, db=database)
    packages = json.loads(row["spec_json"])["packages"]
    record = _record(database, row)
    if record is None:
        record_id = runs.begin(None, RECORD_KIND, signature=_signature(row), db=database)
    else:
        record_id = int(record["id"])
        if record["status"] == "running":                 # the process that was running it is gone
            runs.interrupt(record_id, db=database)
        if record["status"] in ("running", "interrupted"):
            runs.resume(record_id, db=database)
    finished = _checkpoints(database, record_id)
    pending = [(i, p) for i, p in enumerate(packages) if i not in finished]
    _set(database, job_id, JobState.RUNNING)
    step = checkpoint.count(record_id, db=database)
    taken = [0]

    def save(result: PackageResult) -> bool:
        nonlocal step
        try:
            state = {"index": result.index, "status": result.status, "value": result.value, "error": result.error}
            checkpoint.save(record_id, _signature(row), step, f"pkg{result.index}", state, (), db=database)
        except runs.RunError as exc:                       # a result that cannot be kept is a failed package
            state = {"index": result.index, "status": "failed", "value": None, "error": f"unserializable_result: {exc}"[:MAX_ERROR]}
            checkpoint.save(record_id, _signature(row), step, f"pkg{result.index}", state, (), db=database)
        step += 1
        taken[0] += 1
        return budget is None or taken[0] < budget

    lost, reason = 0, None
    if pending:
        if "fork" in multiprocessing.get_all_start_methods():
            reason, lost = _pool(pending, loader, work, workers, save)
        else:
            reason = _serial(pending, loader, work, save)
    final = _checkpoints(database, record_id)
    remaining = len(packages) - len(final)
    if remaining and reason is None:                        # stopped by the budget: it goes on from its checkpoints
        runs.interrupt(record_id, db=database)
        _set(database, job_id, JobState.RUNNING, summary={"workers_lost": lost})
        return status(job_id, db=database)
    ok = sum(1 for r in final.values() if r.status == "done")
    counts = {"total": len(packages), "done": ok, "failed": len(final) - ok, "workers_lost": lost}
    if reason is not None:
        runs.finish(record_id, "failed", outputs=counts, db=database)
        _set(database, job_id, JobState.FAILED, reason=reason, summary=counts, finished=True)
    else:
        runs.finish(record_id, "done", outputs={**counts, "results": [r.value for _, r in sorted(final.items())]}, db=database)
        _set(database, job_id, JobState.DEGRADED if lost else JobState.DONE, reason=f"worker_lost:{lost}" if lost else None, summary=counts, finished=True)
    return status(job_id, db=database)


def _finished(db: Database, row: Mapping[str, Any]) -> bool:
    """A job that ended is not run again: its row says so and its run record is closed."""
    record = _record(db, row)
    return record is not None and record["status"] in runs.FINAL_STATUSES


__all__ = ["JobState", "JobStatus", "PackageResult", "QueueError", "run_job", "status", "submit"]
