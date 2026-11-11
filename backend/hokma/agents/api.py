"""HTTP surface of D7: agents, consumption and scheduled jobs. Every route needs the per-boot session token
(`X-Hokma-Token` or `?token=`), which never reaches an agent process: that is why an agent cannot turn a job on.
Errors are `{"detail": {"code", "message"}}`; the panel translates by `code`, `message` is localized as a fallback."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from hokma.core.origin import token_matches
from hokma.agents import errors
from hokma.agents import usage as usage_mod
from hokma.agents.service import AgentService, AgentsError
from hokma.rpc.ws import SESSION_TOKEN
from hokma.scheduler.loop import Scheduler
from hokma.scheduler.service import JobsService

log = logging.getLogger("hokma.agents")
_running: dict[str, Scheduler | None] = {"scheduler": None}


def current_scheduler() -> Scheduler | None:
    """The scheduler of the running app (None before startup)."""
    return _running["scheduler"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewAgent(_Body):
    name: str = Field(max_length=200)
    description: str = Field(max_length=5000)
    model: str = Field(max_length=20)
    instructions: str = Field(max_length=50000)


class EditAgent(_Body):
    description: str | None = Field(default=None, max_length=5000)
    model: str | None = Field(default=None, max_length=20)
    instructions: str | None = Field(default=None, max_length=50000)
    tools: str | None = Field(default=None, max_length=2000)
    active: bool | None = None


class ModelBody(_Body):
    model: str = Field(max_length=20)


class StatusBody(_Body):
    status: str = Field(max_length=20)


class CapBody(_Body):
    cap: StrictInt | None


class JobBody(_Body):
    name: str = Field(max_length=200)
    agent: str = Field(max_length=60)
    prompt: str = Field(max_length=20000)
    kind: str = Field(max_length=20)
    expr: str = Field(max_length=100)
    timezone: str = Field(max_length=64)


class ActivateBody(_Body):
    confirm: bool


def _auth(header_token: str, query_token: str) -> None:
    if not token_matches(header_token or query_token, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "bad token"})


def _fail(exc: AgentsError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": errors.message(exc.code, str(exc))})


def create_router() -> APIRouter:
    router = APIRouter()
    agents, jobs = AgentService(), JobsService()

    # ------------------------------------------------------------------ agents

    @router.get("/api/agents")
    def list_agents(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict[str, Any]:
        _auth(x_hokma_token, token)
        return agents.list()

    @router.get("/api/agents/summary")
    def agents_summary(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict[str, int]:
        _auth(x_hokma_token, token)
        return agents.summary()

    @router.post("/api/agents", status_code=201)
    def create_agent(body: NewAgent, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        try:
            return agents.create(body.name, body.description, body.model, body.instructions)
        except AgentsError as exc:
            raise _fail(exc) from exc

    @router.put("/api/agents/{slug}")
    def edit_agent(slug: str, body: EditAgent, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        try:
            return agents.update(slug, description=body.description, model=body.model, instructions=body.instructions,
                                 tools=body.tools, active=body.active)
        except AgentsError as exc:
            raise _fail(exc) from exc

    @router.put("/api/agents/{slug}/model")
    def set_model(slug: str, body: ModelBody, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        try:
            return agents.set_model(slug, body.model)
        except AgentsError as exc:
            raise _fail(exc) from exc

    @router.put("/api/agents/{slug}/status")
    def set_status(slug: str, body: StatusBody, x_hokma_token: str = Header(default=""),
                   token: str = Query(default="")):
        _auth(x_hokma_token, token)
        try:
            return agents.set_status(slug, body.status)
        except AgentsError as exc:
            raise _fail(exc) from exc

    # ------------------------------------------------------------------ consumption

    @router.get("/api/usage/weekly")
    def weekly_usage(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict[str, Any]:
        _auth(x_hokma_token, token)
        return usage_mod.weekly()

    @router.put("/api/usage/cap")
    def set_cap(body: CapBody, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        try:
            return usage_mod.set_cap(body.cap)
        except AgentsError as exc:
            raise _fail(exc) from exc

    # ------------------------------------------------------------------ scheduled jobs

    @router.get("/api/scheduled-jobs")
    def list_jobs(x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        active = current_scheduler()
        return {"jobs": jobs.list(), "running": active.running_ids() if active else []}

    @router.post("/api/scheduled-jobs", status_code=201)
    def create_job(body: JobBody, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        try:
            return jobs.create(body.name, body.agent, body.prompt, body.kind, body.expr, body.timezone)
        except AgentsError as exc:
            raise _fail(exc) from exc

    @router.put("/api/scheduled-jobs/{job_id}")
    def update_job(job_id: int, body: JobBody, x_hokma_token: str = Header(default=""),
                   token: str = Query(default="")):
        _auth(x_hokma_token, token)
        try:
            return jobs.update(job_id, body.name, body.agent, body.prompt, body.kind, body.expr, body.timezone)
        except AgentsError as exc:
            raise _fail(exc) from exc

    @router.delete("/api/scheduled-jobs/{job_id}", status_code=204, response_class=Response)
    def delete_job(job_id: int, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        try:
            jobs.delete(job_id)
        except AgentsError as exc:
            raise _fail(exc) from exc
        return Response(status_code=204)

    @router.post("/api/scheduled-jobs/{job_id}/activate")
    def activate_job(job_id: int, body: ActivateBody, x_hokma_token: str = Header(default=""),
                     token: str = Query(default="")):
        _auth(x_hokma_token, token)
        try:
            return jobs.activate(job_id, confirmed=body.confirm)
        except AgentsError as exc:
            raise _fail(exc) from exc

    @router.post("/api/scheduled-jobs/{job_id}/deactivate")
    def deactivate_job(job_id: int, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        try:
            return jobs.deactivate(job_id)
        except AgentsError as exc:
            raise _fail(exc) from exc

    # ------------------------------------------------------------------ lifecycle

    async def startup() -> None:
        from hokma.engine.api import database_ready

        if database_ready():
            try:
                report = agents.reconcile()
                if report.problems:
                    log.warning("agents not reconciled: %s", report.problems)
            except Exception:
                log.exception("agent reconcile failed")
        _running["scheduler"] = Scheduler()
        await _running["scheduler"].start()

    async def shutdown() -> None:
        if _running["scheduler"] is not None:
            await _running["scheduler"].stop()
            _running["scheduler"] = None

    router.add_event_handler("startup", startup)
    router.add_event_handler("shutdown", shutdown)
    return router


router = create_router()
