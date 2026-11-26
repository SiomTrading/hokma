"""What the routes of the screen and the tools of the agents share (SPEC-032, sections 9.2, 11 and 12).

`payload` is the verdict as a reader gets it: the contract of 9.2 plus three optional fields that make the screen richer (`notices`, `evaluating` and
`ideal.plateau_mean`). `failure_of` gives every known failure its stable code, its class for the tools (`no_data`, `stale`, `bad_input`, ...) and its HTTP
status, so a route and a tool never disagree about what a failure is. `BOARD` is the queue of "evaluate": one worker runs the evaluations one after the other
(an evaluation runs backtests, and the service never runs two searches of one process at once), each with a job id and the progress of the neighbors.

Nothing here reads the verdict of a strategy to stop an action: the verdict is a suggestion and this module only shows it. No road to a broker lives here.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from hokma import i18n
from hokma.factory import evidence_bridge, questions, thresholds
from hokma.factory import verdict as factory_verdict
from hokma.factory.library_catalog import LibraryError
from hokma.quant import limits

LOG = logging.getLogger("hokma.verdict")
INTERNAL_NOTES = ("no_walk_forward", "no_optimization", "regimes_from_instrument_bars")     # the empty blocks already say why; they are not notices
MAX_NOTICES = 40
MAX_FINISHED_JOBS = 50
_CODED = re.compile(r"^[a-z][a-z0-9_]*(:|$)")
_CODE_PREFIX = re.compile(r"^([a-z][a-z_]*):")

# ---- failures -----------------------------------------------------------------------------------------------------------------------------
TOOL_CLASS = {"no_run": "no_data", "no_walk_forward": "no_data", "no_verdict": "no_data", "no_optimization": "no_data", "data_changed": "stale",
              "no_spec": "bad_input", "no_thresholds": "bad_input", "bad_input": "bad_input"}
HTTP_STATUS = {"no_data": 404, "stale": 409, "bad_input": 400, "unavailable": 503, "blocked": 403}
NOT_FOUND = ("no_spec",)                                    # a bad_input class for the tools that is a missing thing for a route


@dataclass(frozen=True)
class Failure:
    code: str                    # the stable code: no_run, no_spec, no_thresholds, no_verdict, data_changed, bad_input, ...
    kind: str                    # the class the tools answer with: no_data, stale, bad_input, unavailable, blocked
    message: str                 # starts with the stable code
    status: int                  # the HTTP status of a route

    @property
    def http_code(self) -> str:
        """The code in the body of a route: the stable one, except that a line with no limits is a `bad_input` with its reason next to it."""
        return "bad_input" if self.code == "no_thresholds" else self.code

    @property
    def reason(self) -> str | None:
        return "no_thresholds" if self.code == "no_thresholds" else None


def _stated(code: str, exc: Exception) -> str:
    text = str(exc).strip("'\"")[:400]
    return text if _CODE_PREFIX.match(text) else f"{code}: {text}"


def failure_of(exc: Exception) -> Failure | None:
    """The known failure behind `exc`, or None when it is not one (it is then a bug and must propagate). The modules of the verdict keep their own stable
    codes; every other failure of the backtest service is classified by the one function of the backtest tools, which this module does not change."""
    domain = (evidence_bridge.BridgeError, questions.QuestionError, thresholds.ThresholdError, factory_verdict.VerdictError, LibraryError)
    if isinstance(exc, domain):
        code = str(getattr(exc, "code", "bad_input"))
        kind = TOOL_CLASS.get(code, "bad_input")
        status = 404 if code in NOT_FOUND else HTTP_STATUS[kind]
        return Failure(code, kind, _stated(code, exc), status)
    from hokma.kit import backtest_tools

    error = backtest_tools.classify(exc)
    if error is None:
        return None
    found = _CODE_PREFIX.match(error.message)
    code = found.group(1) if found and found.group(1) in TOOL_CLASS else error.code
    kind = TOOL_CLASS.get(code, error.code)
    return Failure(code, kind, error.message, HTTP_STATUS.get(kind, 400))


# ---- the verdict as a reader gets it -----------------------------------------------------------------------------------------------------

def language_of(header: str | None) -> str:
    """The language of the panel that asks (Accept-Language), else the one of this installation."""
    return i18n.accepted_language(header or "") or i18n.language()


def _summary(db: Any, run_id: int | None) -> dict[str, Any]:
    if run_id is None:
        return {}
    with db.read() as u:
        row = u.session.one("SELECT summary_json FROM backtest_runs WHERE id = :i", {"i": int(run_id)})
    return json.loads(row["summary_json"]) if row is not None and row["summary_json"] else {}


def _coded(texts: Any) -> list[str]:
    return [t for t in texts if isinstance(t, str) and _CODED.match(t)] if isinstance(texts, (list, tuple)) else []


def notices(v: factory_verdict.Verdict, db: Any) -> list[str]:
    """The stable codes the screen turns into plain words (7.2 I): the notes of the bridge and the warnings that the saved runs of this verdict carry (the run,
    the optimization, the walk forward and the Monte Carlo). A missing Monte Carlo is always said. Codes keep their detail after a colon (`ambiguous_bars:3`)."""
    run, walk = _summary(db, v.run_id), _summary(db, v.walkforward_id)
    found = [n for n in v.notes if n not in INTERNAL_NOTES]
    found += _coded(run.get("warnings"))
    found += _coded(_summary(db, v.optimize_id).get("warnings"))
    found += _coded((walk.get("result") or {}).get("warnings"))
    found += _coded(_summary(db, v.montecarlo_id).get("warnings"))
    if int(run.get("ambiguous_bars") or 0) > 0 and not any(n.split(":")[0] == "ambiguous_bars" for n in found):
        found.append(f"ambiguous_bars:{int(run['ambiguous_bars'])}")
    if v.montecarlo_id is None and "monte_carlo_missing" not in found:
        found.append("monte_carlo_missing")
    return list(OrderedDict.fromkeys(found))[:MAX_NOTICES]


def payload(v: factory_verdict.Verdict, *, language: str = "en", services: Any = None) -> dict[str, Any]:
    """`Verdict.to_dict` of 9.2 with `notices`, `evaluating` (the progress of a running evaluation of this version, else null) and `ideal.plateau_mean`."""
    db = evidence_bridge.resolve_services(services).database()
    body = v.to_dict(language=language)
    body["ideal"] = {**body["ideal"], "plateau_mean": v.ideal.plateau_mean}
    body["notices"] = notices(v, db)
    body["evaluating"] = BOARD.progress(v.strategy_id, v.strategy_version)
    return body


def moment(value: Any) -> Any:
    """A moment read from a row (a datetime or its text) as a datetime."""
    return factory_verdict._when(value)


def has_run(db: Any, strategy_id: str, version: int) -> bool:
    """True when the version has a finished run of its own (a run that a neighbor or the ideal point derived from another one does not count)."""
    with db.read() as u:
        found = u.session.one("SELECT 1 AS found FROM backtest_runs WHERE kind = 'run' AND status IN ('done', 'account_blown') AND parent_id IS NULL "
                              "AND strategy_id = :s AND strategy_version = :v LIMIT 1", {"s": strategy_id, "v": version})
    return found is not None


def iso(value: Any) -> str:
    """A moment as the contract writes it: UTC, to the second, with a Z."""
    return factory_verdict._iso(value)


def question_dict(s: questions.QuestionState) -> dict[str, Any]:
    return {"key": s.key, "subject": s.subject, "status": s.status, "suggestion": s.suggestion, "suggestion_reason": s.suggestion_reason, "answer": s.answer,
            "answered_at": None if s.answered_at is None else iso(s.answered_at), "answer_id": s.answer_id, "note": s.note, "scope": s.scope,
            "na_reason": s.na_reason}


def threshold_dict(s: thresholds.ThresholdSet) -> dict[str, Any]:
    return {"id": s.id, "line": s.line, "version": s.version, "values": dict(s.values), "changed_by": s.changed_by, "reason": s.reason,
            "created_at": iso(s.created_at), "restored_from": s.restored_from, "unchanged": s.unchanged}


# ---- the queue of "evaluate" -------------------------------------------------------------------------------------------------------------

@dataclass
class Job:
    id: int
    strategy_id: str
    version: int
    actor: str
    state: str = "queued"                  # queued, running, done, failed
    done: int = 0                          # neighbors finished
    total: int = 0                         # neighbors of the step being run
    verdict_id: int | None = None
    code: str | None = None
    message: str | None = None

    @property
    def open(self) -> bool:
        return self.state in ("queued", "running")

    def to_dict(self) -> dict[str, Any]:
        return {"job_id": self.id, "kind": "verdict", "strategy_id": self.strategy_id, "version": self.version, "state": self.state, "done": self.done,
                "total": self.total, "verdict_id": self.verdict_id, "error": None if self.code is None else {"code": self.code, "message": self.message}}


class Busy(ValueError):
    """Too many evaluations are open. `code` is the stable `unavailable`."""

    code = "unavailable"


class JobBoard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: OrderedDict[int, Job] = OrderedDict()
        self._next = 1
        self._pool: ThreadPoolExecutor | None = None

    def submit(self, strategy_id: str, version: int, *, actor: str, services: Any) -> Job:
        """Queue `verdict.evaluate`. What can be known before it runs is checked here, so the caller hears it at once: `no_spec`, `no_run` and
        `no_thresholds`. An evaluation of the same version that is already open is the answer, not a second one."""
        thresholds.current(evidence_bridge.line_of(strategy_id, version, services=services), db=services.database())      # the order of `verdict.evaluate`
        evidence_bridge.gather(strategy_id, version, services=services, bundle=False)
        with self._lock:
            for job in self._jobs.values():
                if job.open and (job.strategy_id, job.version) == (strategy_id, version):
                    return job
            if sum(1 for j in self._jobs.values() if j.open) >= limits.MAX_OPEN_JOBS:
                raise Busy(f"{limits.MAX_OPEN_JOBS} evaluations are already open: wait for one to finish")
            job = Job(self._next, strategy_id, version, actor)
            self._next += 1
            self._jobs[job.id] = job
            while len(self._jobs) > MAX_FINISHED_JOBS and not next(iter(self._jobs.values())).open:
                self._jobs.popitem(last=False)
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="verdict-job")
            self._pool.submit(self._work, job, services)
        return job

    def _work(self, job: Job, services: Any) -> None:
        job.state = "running"

        def hook(done: int, total: int) -> None:
            job.done, job.total = done, total

        token = evidence_bridge.NEIGHBOR_PROGRESS.set(hook)
        try:
            job.verdict_id = factory_verdict.evaluate(job.strategy_id, job.version, actor=job.actor, services=services).id
            job.state = "done"
        except Exception as exc:                                    # noqa: BLE001 - nobody awaits this thread: the failure is the state of the job
            found = failure_of(exc)
            if found is None:
                LOG.exception("evaluation of %s version %s failed", job.strategy_id, job.version)
            job.code = "internal" if found is None else found.code
            job.message = f"internal: {type(exc).__name__}" if found is None else found.message
            job.state = "failed"
        finally:
            evidence_bridge.NEIGHBOR_PROGRESS.reset(token)

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def progress(self, strategy_id: str, version: int) -> dict[str, int] | None:
        with self._lock:
            for job in self._jobs.values():
                if job.open and (job.strategy_id, job.version) == (strategy_id, version):
                    return {"done": job.done, "total": job.total}
        return None


BOARD = JobBoard()
