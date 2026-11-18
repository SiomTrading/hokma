"""HTTP surface of the money desks (SPEC-014 P4). Every route needs the per-boot session token (X-Hokma-Token or
?token=). Every route that changes something is for the owner, through the screen: agents have no route here.

Errors carry a stable `code` and a localized `message`, in the same shape as the projects routes.
"""
from typing import Any, Callable

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from hokma.core.origin import token_matches
from hokma.db import DbError
from hokma.desks import budget, service
from hokma.desks.errors import DeskError
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)


class TokenRoute(APIRoute):
    """Token check and error mapping for every route of this router."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            candidate = request.headers.get("x-hokma-token") or request.query_params.get("token") or ""
            if not token_matches(candidate, SESSION_TOKEN):
                return _error(401, "bad_token", t("desks.err.bad_token"))
            try:
                return await original(request)
            except DeskError as exc:
                return _error(exc.status, exc.code, exc.message)
            except DbError:
                return _error(400, "db_error", t("desks.err.db_failed"))

        return handler


router = APIRouter(route_class=TokenRoute)


class NewDesk(BaseModel):
    name: Any
    capital: Any
    focus: Any = None
    template: Any = "standard"
    objective_pct: Any = None
    mandate: Any = None
    assets: Any = None
    token_budget: Any = None


class DeskChanges(BaseModel):
    name: Any = None
    focus: Any = None
    capital: Any = None
    objective_pct: Any = None
    mandate: Any = None
    token_budget: Any = None
    status: Any = None


class Assets(BaseModel):
    assets: Any


class NewAgent(BaseModel):
    agent_slug: str
    model: Any
    role: str | None = None


class AgentChanges(BaseModel):
    model: Any = None
    active: Any = None


class ConnectionChoice(BaseModel):
    enabled: Any


# ---- fund -----------------------------------------------------------------------------------

@router.get("/api/desks")
def list_desks(include_archived: bool = Query(default=False)) -> dict:
    return {"desks": service.list_desks(include_archived=include_archived)}


@router.post("/api/desks", status_code=201)
def create_desk(body: NewDesk) -> dict:
    service.check_symbols(body.assets)
    return service.create_desk(body.name, body.capital, focus=body.focus, template=body.template,
                               objective_pct=body.objective_pct, mandate=body.mandate, assets=body.assets,
                               token_budget=body.token_budget)


@router.get("/api/desks/templates")          # before the `{desk_id}` routes
def list_templates() -> dict:
    return {"templates": service.list_templates()}


# ---- one desk -------------------------------------------------------------------------------

@router.get("/api/desks/{desk_id}")
def get_desk(desk_id: int) -> dict:
    return service.get_desk(desk_id)


@router.patch("/api/desks/{desk_id}")
def update_desk(desk_id: int, body: DeskChanges) -> dict:
    return service.update_desk(desk_id, body.model_dump(exclude_unset=True))


@router.delete("/api/desks/{desk_id}")
def delete_desk(desk_id: int) -> dict:
    return service.delete_desk(desk_id)


@router.put("/api/desks/{desk_id}/assets")
def set_assets(desk_id: int, body: Assets) -> dict:
    service.check_symbols(body.assets)
    return {"assets": service.set_assets(desk_id, body.assets)}


@router.put("/api/desks/{desk_id}/assets/order")
def reorder_assets(desk_id: int, body: Assets) -> dict:
    return {"assets": service.reorder_assets(desk_id, body.assets)}


@router.get("/api/desks/{desk_id}/connections")
def list_connections(desk_id: int) -> dict:
    return {"connections": service.list_connections(desk_id)}


@router.put("/api/desks/{desk_id}/connections/{provider_id}")
def set_connection(desk_id: int, provider_id: str, body: ConnectionChoice) -> dict:
    return service.set_connection(desk_id, provider_id, body.enabled)


# ---- team -----------------------------------------------------------------------------------

@router.get("/api/desks/{desk_id}/agents")
def list_agents(desk_id: int) -> dict:
    return {"agents": service.list_agents(desk_id)}


@router.post("/api/desks/{desk_id}/agents", status_code=201)
def add_agent(desk_id: int, body: NewAgent) -> dict:
    return service.add_agent(desk_id, body.agent_slug, body.model, role=body.role)


@router.put("/api/desks/{desk_id}/agents/{agent_slug}")
def set_agent(desk_id: int, agent_slug: str, body: AgentChanges) -> dict:
    changes = body.model_dump(exclude_unset=True)
    return service.set_agent(desk_id, agent_slug, **changes)


@router.delete("/api/desks/{desk_id}/agents/{agent_slug}", status_code=204)
def remove_agent(desk_id: int, agent_slug: str) -> Response:
    service.remove_agent(desk_id, agent_slug)
    return Response(status_code=204)


# ---- budget and chat ------------------------------------------------------------------------

@router.get("/api/desks/{desk_id}/usage")
def desk_usage(desk_id: int) -> dict:
    return budget.usage(desk_id)


@router.post("/api/desks/{desk_id}/conversation")
def desk_conversation(desk_id: int) -> dict:
    return service.open_conversation(desk_id)
