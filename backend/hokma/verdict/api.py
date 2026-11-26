"""HTTP surface of the screen Backtest (SPEC-032, section 12). Every route needs the session token (X-Hokma-Token or ?token=). The ones that write need, besides,
the confirmation that only the owner's open tab holds (X-Hokma-Owner, `session.owner_matches`): without it they answer 403 `owner_confirmation_required`.
"Evaluate" is the exception: the creator may also ask for it through the tools, so it asks for no confirmation; it goes to the queue of the service and answers with
a job id (`GET /jobs/{id}` says how far it is, and the verdict of the version carries `evaluating` while it runs).

The shapes are the ones of `dashboard/src/screens/backtest/types.ts`. A failure answers `{"detail": {"code", "message"}}`, with `reason` next to it for a line that has no
limits (code `bad_input`, reason `no_thresholds`). The verdict never stops anything: it is shown, and nothing here reads it to refuse an action. The routes with a fixed
word come before the ones with ids, because a path is matched in the order it was declared.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from hokma.core import session
from hokma.factory import library_catalog, questions, thresholds
from hokma.factory import verdict as factory_verdict
from hokma.i18n import t
from hokma.status.auth import require_token
from hokma.verdict import service

OWNER = "owner"
REPORT_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:", "X-Content-Type-Options": "nosniff",
                  "Cache-Control": "no-store"}
HEATMAP_LAYOUT = {"cell": 48, "left": 76, "top": 28, "gap": 8, "bottom": 38}       # the geometry of surface.Heatmap.svg; a test compares it with a real svg
FINISHED = ("done", "account_blown")


def _body(status: int, code: str, message: str, reason: str | None = None) -> JSONResponse:
    detail = {"code": code, "message": message, **({"reason": reason} if reason else {})}
    return JSONResponse({"detail": detail}, status_code=status)


class Missing(Exception):
    """A thing of the service that is not there. `code` is the stable code of the route: no_run, no_verdict, no_optimization, no_job."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VerdictRoute(APIRoute):
    """Gives every known failure of the modules its status and its stable code. Anything else is a bug and stays one."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except HTTPException:
                raise
            except Missing as exc:
                return _body(404, exc.code, str(exc))
            except service.Busy as exc:
                return _body(503, "unavailable", str(exc))
            except Exception as exc:                                # noqa: BLE001 - an exception that is not a known failure is raised again
                found = service.failure_of(exc)
                if found is None:
                    raise
                return _body(found.status, found.http_code, found.message, found.reason)

        return handler


router = APIRouter(route_class=VerdictRoute, dependencies=[Depends(require_token)])


def owner_required(x_hokma_owner: str = Header(default="")) -> None:
    """The second layer after the token: the confirmation that only the open tab of the owner holds."""
    if not session.owner_matches(x_hokma_owner):
        raise HTTPException(403, detail={"code": "owner_confirmation_required", "message": t("permissions.err.owner_confirmation_required")})


ASOWNER = [Depends(owner_required)]


def _services() -> Any:
    from hokma.kit import backtest_tools

    return backtest_tools.services()


def _language(request: Request) -> str:
    return service.language_of(request.headers.get("accept-language"))


def _run_row(db: Any, run_id: int, kind: str, *, missing: str = "no_run", finished: tuple[str, ...] = FINISHED) -> dict[str, Any]:
    with db.read() as u:
        row = u.session.one("SELECT id, kind, status, strategy_id, strategy_version, spec_json, data_hash, summary_json FROM backtest_runs WHERE id = :i", {"i": run_id})
    if row is None or row["kind"] != kind or row["status"] not in finished:
        raise Missing(missing, f"there is no finished {kind} with the id {run_id}")
    return dict(row)


# ---- bodies ---------------------------------------------------------------------------------------------------------------------------

class ExcludeBody(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=30)
    reason: str | None = Field(default=None, max_length=500)


class IncludeBody(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=30)


class RecomputeBody(BaseModel):
    cause: str = Field(max_length=20)


class AnswerItem(BaseModel):
    key: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=10)
    note: str | None = Field(default=None, max_length=1000)


class AnswersBody(BaseModel):
    answers: list[AnswerItem] = Field(min_length=1, max_length=40)


class ThresholdsBody(BaseModel):
    values: dict[str, Any] = Field(max_length=40)
    reason: str | None = Field(default=None, max_length=500)


# ---- lists, the library and the limits ------------------------------------------------------------------------------------------------------

_LAST_RUNS = ("SELECT r.strategy_id, r.strategy_version, r.spec_json FROM backtest_runs r JOIN (SELECT MAX(id) AS last FROM backtest_runs WHERE kind = 'run' "
              "AND status IN ('done', 'account_blown') AND parent_id IS NULL AND strategy_id IS NOT NULL AND strategy_version IS NOT NULL "
              "GROUP BY strategy_id, strategy_version) g ON g.last = r.id ORDER BY r.strategy_id, r.strategy_version")
_LAST_VERDICTS = ("SELECT v.id, v.strategy_id, v.strategy_version, v.verdict, v.created_at FROM strategy_verdicts v "
                  "JOIN (SELECT MAX(id) AS last FROM strategy_verdicts GROUP BY strategy_id, strategy_version) g ON g.last = v.id")


@router.get("/api/verdict/strategies")
def strategies() -> list[dict[str, Any]]:
    """The versions of strategies that have a finished run, with the last verdict of each (null when it has none)."""
    db = _services().database()
    with db.read() as u:
        runs = u.session.query(_LAST_RUNS, {})
        seen = {(r["strategy_id"], int(r["strategy_version"])): r for r in u.session.query(_LAST_VERDICTS, {})}
    out = []
    for run in runs:
        last = seen.get((run["strategy_id"], int(run["strategy_version"])))
        out.append({"strategy_id": run["strategy_id"], "version": int(run["strategy_version"]), "line": json.loads(run["spec_json"])["line"],
                    "verdict": None if last is None else last["verdict"], "verdict_id": None if last is None else int(last["id"]),
                    "evaluated_at": None if last is None else service.iso(service.moment(last["created_at"]))})
    return out


@router.get("/api/verdict/library")
def library(kind: str | None = Query(default=None, max_length=20), status: str | None = Query(default=None, max_length=20)) -> list[dict[str, Any]]:
    return [item.as_dict() for item in library_catalog.catalog(kind=kind, status=status, db=_services().database())]


@router.get("/api/verdict/thresholds/{line}")
def thresholds_now(line: str) -> dict[str, Any]:
    return service.threshold_dict(thresholds.current(line, db=_services().database()))


@router.get("/api/verdict/thresholds/{line}/history")
def thresholds_history(line: str) -> list[dict[str, Any]]:
    return [service.threshold_dict(s) for s in thresholds.history(line, db=_services().database())]


@router.put("/api/verdict/thresholds/{line}", dependencies=ASOWNER)
def thresholds_save(line: str, body: ThresholdsBody) -> dict[str, Any]:
    return service.threshold_dict(thresholds.save(line, body.values, reason=body.reason, actor=OWNER, db=_services().database()))


@router.post("/api/verdict/thresholds/{line}/restore/{set_id}", dependencies=ASOWNER)
def thresholds_restore(line: str, set_id: int) -> dict[str, Any]:
    return service.threshold_dict(thresholds.restore(line, set_id, actor=OWNER, db=_services().database()))


@router.post("/api/verdict/thresholds/{line}/copy-from/{source}", dependencies=ASOWNER)
def thresholds_copy(line: str, source: str) -> dict[str, Any]:
    return service.threshold_dict(thresholds.copy_line(line, source, actor=OWNER, db=_services().database()))


@router.get("/api/verdict/jobs/{job_id}")
def job(job_id: int) -> dict[str, Any]:
    found = service.BOARD.get(job_id)
    if found is None:
        raise Missing("no_job", f"there is no evaluation with the id {job_id}")
    return found.to_dict()


# ---- what the runs saved ----------------------------------------------------------------------------------------------------------------------

@router.get("/api/verdict/runs/{run_id}/report")
def report(run_id: int) -> Response:
    """The HTML report of a run, with every text escaped and no resource or script; the header keeps a browser from loading anything else."""
    from hokma.backtest import results, walkforward

    svc = _services()
    row = _run_row(svc.database(), run_id, "run")
    found = walkforward.load_result(svc.database(), run_id, curve_dir=svc.curves())
    html = results.report_html(found, title=f"{row['strategy_id']} version {row['strategy_version']} run {run_id}")
    return Response(html, media_type="text/html", headers=REPORT_HEADERS)


@router.get("/api/verdict/runs/{run_id}/summary")
def summary(run_id: int) -> dict[str, Any]:
    """The summary that `results.record_result` saved with the run: costs, ambiguous bars, statistics and warnings (no trades, no curve)."""
    return json.loads(_run_row(_services().database(), run_id, "run")["summary_json"])


def _dates(row: dict[str, Any], body: dict[str, Any]) -> None:
    """Dates for the rows of each window, read from the bars the walk forward was made on. Left out when those bars cannot be read or are not the same ones."""
    from hokma.backtest import runner
    from hokma.backtest.runner import StageError
    from hokma.backtest.spec import SpecError

    try:
        data = _services().load(runner.spec_from_canonical(json.loads(row["spec_json"])))
    except (StageError, SpecError, ValueError, LookupError, OSError):
        return
    if data.data_hash != row["data_hash"]:
        return
    bars = data.bars

    def day(i: int) -> str:
        return bars[i].ts.date().isoformat()

    for w in body["windows"]:
        first, end = w["train"]
        if 0 <= first < end <= len(bars) and all(0 <= a < b <= len(bars) for a, b in w["test_blocks"]):
            w["train_dates"] = [day(first), day(end - 1)]
            w["test_dates"] = [[day(a), day(b - 1)] for a, b in w["test_blocks"]]


@router.get("/api/verdict/runs/{run_id}/walkforward")
def walkforward_saved(run_id: int) -> dict[str, Any]:
    """The walk forward saved with its id (`runs.walkforward` of the verdict): `WalkForwardResult.to_dict()` with its own id and, when the bars can be read, the dates."""
    row = _run_row(_services().database(), run_id, "walkforward")
    body = json.loads(row["summary_json"])["result"]
    body["run_id"] = int(row["id"])
    _dates(row, body)
    return body


@router.get("/api/verdict/runs/{run_id}/montecarlo")
def montecarlo_saved(run_id: int) -> dict[str, Any]:
    """The Monte Carlo saved with its id (`runs.montecarlo` of the verdict)."""
    return json.loads(_run_row(_services().database(), run_id, "montecarlo")["summary_json"])


@router.post("/api/verdict/runs/{run_id}/montecarlo", dependencies=ASOWNER)
def montecarlo_run(run_id: int) -> dict[str, Any]:
    """Run the Monte Carlo of a run with the defaults of H4 (a bootstrap of its daily returns: `runs` and `seed` of `quant.metrics.montecarlo`). The result is saved by
    the service as a child of the run; the next evaluation of the version finds it."""
    from hokma import backtest
    from hokma.quant.metrics import montecarlo

    svc = _services()
    _run_row(svc.database(), run_id, "run")
    return backtest.monte_carlo(run_id, n=montecarlo.DEFAULT_RUNS, method="bootstrap", seed=montecarlo.DEFAULT_SEED, services=svc).to_dict()


@router.get("/api/verdict/optimizations/{opt_id}/heatmap")
def heatmap(opt_id: int, x: str | None = Query(default=None, max_length=48), y: str | None = Query(default=None, max_length=48),
            metric: str | None = Query(default=None, max_length=48)) -> dict[str, Any]:
    """The map of an optimization over two parameters: the SVG of `surface.heatmap` (an image for the screen, never HTML), its cells and the facts of the search.
    Without `x` and `y` the first two parameters are used. A part of the analysis that cannot be computed (the groups of the plateaus need the portfolio module)
    comes back as the warning `analysis_part_missing:{part}`; the map is still drawn."""
    from hokma.backtest import optimize, surface

    db = _services().database()
    _run_row(db, opt_id, "optimize", missing="no_optimization", finished=("done",))
    found = optimize.load(db, opt_id)
    names = list(found.space.names)
    x = x or next((n for n in names if n != y), None)
    y = y or next((n for n in names if n != x), None)
    drawn = surface.heatmap(found, x, y, metric=metric)
    analysis = surface.analyze_result(found, opt_id=opt_id)
    return {"svg": drawn.svg(), "heatmap": drawn.to_dict(), "axes": [{"name": a.name, "kind": a.kind} for a in found.space.axes],
            "best": None if found.best is None else {"params": dict(found.best.params), "objective": found.best.objective}, "n_trials": found.n_trials,
            "failed": sum(1 for trial in found.trials if trial.status == "failed"), "cache_hits": found.cache_hits, "edge_of_space": list(found.edge_of_space),
            "warnings": list(dict.fromkeys([*found.warnings, *analysis.warnings])), "layout": HEATMAP_LAYOUT}


# ---- one verdict: take checks out, put them back, recompute ---------------------------------------------------------------------------------------

@router.post("/api/verdict/{verdict_id}/exclude", dependencies=ASOWNER)
def exclude(verdict_id: int, body: ExcludeBody, request: Request) -> dict[str, Any]:
    svc = _services()
    return service.payload(factory_verdict.exclude(verdict_id, body.keys, reason=body.reason, actor=OWNER, db=svc.database()), language=_language(request), services=svc)


@router.post("/api/verdict/{verdict_id}/include", dependencies=ASOWNER)
def include(verdict_id: int, body: IncludeBody, request: Request) -> dict[str, Any]:
    svc = _services()
    return service.payload(factory_verdict.include(verdict_id, body.keys, actor=OWNER, db=svc.database()), language=_language(request), services=svc)


@router.post("/api/verdict/{verdict_id}/recompute", dependencies=ASOWNER)
def recompute(verdict_id: int, body: RecomputeBody, request: Request) -> dict[str, Any]:
    """The verdict again with the limits or the answers of now. A new neighbor spread can run backtests, so this can take a while."""
    svc = _services()
    return service.payload(factory_verdict.recompute(verdict_id, cause=body.cause, actor=OWNER, services=svc), language=_language(request), services=svc)  # type: ignore[arg-type]


# ---- one version of a strategy ------------------------------------------------------------------------------------------------------------------

@router.get("/api/verdict/{strategy_id}/{version}/questions")
def questions_of(strategy_id: str, version: int) -> list[dict[str, Any]]:
    return [service.question_dict(s) for s in questions.states(strategy_id, version, services=_services())]


@router.post("/api/verdict/{strategy_id}/{version}/answers", dependencies=ASOWNER)
def answer(strategy_id: str, version: int, body: AnswersBody) -> dict[str, Any]:
    """One row for each answer (the old ones stay), and the questions as they are now. Every key and value is checked before the first row is written."""
    svc = _services()
    asked = {q.key for q in questions.QUESTIONS}
    for item in body.answers:
        if item.key not in asked or item.value not in questions.ANSWERS:
            raise questions.QuestionError("bad_input", f"key is one of {', '.join(sorted(asked))} and value one of {', '.join(questions.ANSWERS)}")
    for item in body.answers:
        questions.answer(strategy_id, version, item.key, item.value, note=item.note, actor=OWNER, db=svc.database())
    return {"saved": len(body.answers), "questions": [service.question_dict(s) for s in questions.states(strategy_id, version, services=svc)]}


@router.post("/api/verdict/{strategy_id}/{version}/evaluate")
def evaluate(strategy_id: str, version: int) -> JSONResponse:
    """Queue the evaluation. The answer is the id of the job; the screen asks the verdict of the version until it changes."""
    started = service.BOARD.submit(strategy_id, version, actor=OWNER, services=_services())
    return JSONResponse({"job_id": started.id, "state": started.state}, status_code=202)


@router.get("/api/verdict/{strategy_id}/{version}")
def latest(strategy_id: str, version: int, request: Request) -> dict[str, Any]:
    svc = _services()
    found = factory_verdict.latest(strategy_id, version, db=svc.database())
    if found is None:
        if not service.has_run(svc.database(), strategy_id, version):
            raise Missing("no_run", f"{strategy_id} version {version} has no finished run: run it first")
        raise Missing("no_verdict", f"{strategy_id} version {version} has no verdict yet: evaluate it")
    return service.payload(found, language=_language(request), services=svc)
