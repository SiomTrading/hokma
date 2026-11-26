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
from hokma.desks import budget, company, earnings, option_actions, option_detail, service, strategies
from hokma.desks.swo import analysis
from hokma.desks.errors import DeskError
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message, **extra}}, status_code=status)


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
                return _error(exc.status, exc.code, exc.message, **exc.extra)
            except DbError:
                return _error(400, "db_error", t("desks.err.db_failed"))

        return handler


router = APIRouter(route_class=TokenRoute)


class NewDesk(BaseModel):
    name: Any
    capital: Any = None                       # the old way, a desk without account; the app sends `account`
    account: Any = None                       # {"mode": "new", "initial_capital": n} or {"mode": "existing", "account_id": n}
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


class PanelOrder(BaseModel):
    panels: Any


class NewPanel(BaseModel):
    id: Any


class StrategiesAsk(BaseModel):
    symbol: Any = None                  # a missing field reaches check_arguments, which answers bad_strategy_args (422)
    view: Any = None
    target: Any = None
    budget: Any = None
    expiration: Any = None
    priority: Any = "balanced"


class OptionDetailAsk(BaseModel):
    symbol: Any
    expiration: Any
    legs: Any = None
    structure: Any = None
    strikes: Any = None
    quantity: Any = None
    range_pct: Any = None
    iv_mult: Any = None
    rows: Any = None
    columns: Any = None


class OptionSelection(OptionDetailAsk):
    strategy_key: Any = None
    name: Any = None


class SaveStrategy(BaseModel):
    name: Any = None
    symbol: Any = None
    expiration: Any = None
    legs: Any = None
    structure: Any = None
    strikes: Any = None
    quantity: Any = None


class ProposeStrategy(BaseModel):
    symbol: Any = None
    expiration: Any = None
    legs: Any = None
    structure: Any = None
    strikes: Any = None
    quantity: Any = None
    strategy_key: Any = None


class ConnectionChoice(BaseModel):
    enabled: Any


class NewAnalysis(BaseModel):
    symbol: Any
    strategy: Any = None


# ---- fund -----------------------------------------------------------------------------------

@router.get("/api/desks")
def list_desks(include_archived: bool = Query(default=False)) -> dict:
    return {"desks": service.list_desks(include_archived=include_archived)}


@router.post("/api/desks", status_code=201)
def create_desk(body: NewDesk) -> dict:
    service.check_symbols(body.assets)
    return service.create_desk(body.name, body.capital, account=body.account, focus=body.focus, template=body.template,
                               objective_pct=body.objective_pct, mandate=body.mandate, assets=body.assets,
                               token_budget=body.token_budget)


@router.get("/api/runs/active")
def active_runs() -> dict:
    return service.active_runs()


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


@router.post("/api/desks/{desk_id}/connections/activate")
def activate_my_connections(desk_id: int) -> dict:
    return service.activate_my_connections(desk_id)


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


# ---- analysis of an asset (SPEC-028, delivery 4). The progress is not here: it goes by the chat socket (`watch_desk`, `engine/engine.py`) ----------

@router.post("/api/desks/{desk_id}/analysis", status_code=202)
def start_analysis(desk_id: int, body: NewAnalysis) -> dict:
    """Open the analysis of an asset. Answers at once with the run id; a live run of the same desk and asset answers 409 with its id."""
    return analysis.start(desk_id, body.symbol, body.strategy)


@router.get("/api/desks/{desk_id}/analysis")
def list_analysis(desk_id: int, limit: int = Query(default=analysis.DEFAULT_LIMIT)) -> dict:
    return analysis.list_runs(desk_id, limit)


@router.get("/api/desks/{desk_id}/analysis/{run_id}")
def get_analysis(desk_id: int, run_id: int) -> dict:
    return analysis.get(desk_id, run_id)


# ---- panels of the screen and the Company panel (SPEC-025, delivery 3) ----------------------------------------------------------

@router.put("/api/desks/{desk_id}/panels")
def set_panels(desk_id: int, body: PanelOrder) -> dict:
    return service.set_panels(desk_id, body.panels)


@router.post("/api/desks/{desk_id}/panels", status_code=201)
def add_panel(desk_id: int, body: NewPanel) -> dict:
    return service.add_panel(desk_id, body.id)


@router.delete("/api/desks/{desk_id}/panels/{panel}")
def remove_panel(desk_id: int, panel: str) -> dict:
    return service.remove_panel(desk_id, panel)


@router.get("/api/desks/{desk_id}/earnings-calendar")
def desk_earnings_calendar(desk_id: int, from_: str = Query(alias="from"), to: str = Query(), filter: str = "all") -> dict:
    """The week of earnings of the Calendar panel (SPEC-028, delivery 7): read when the panel opens or the week changes."""
    return earnings.calendar(desk_id, from_, to, filter)


@router.get("/api/desks/{desk_id}/company/{symbol}")
def desk_company(desk_id: int, symbol: str) -> dict:
    service.get_desk(desk_id)                       # 404 when the desk is not there
    return company.company(desk_id, symbol)


# ---- the Strategies panel (SPEC-028, delivery 5): the cards of a view on the real chain and a verdict per card ---------------------------------

@router.get("/api/desks/{desk_id}/strategies/last")
def desk_strategies_last(desk_id: int) -> dict:
    """The last consultation of the desk (form and cards), kept in memory: what the panel shows when it opens."""
    service.get_desk(desk_id)
    return strategies.last(desk_id)


@router.post("/api/desks/{desk_id}/strategies")
def desk_strategies(desk_id: int, body: StrategiesAsk) -> dict:
    service.get_desk(desk_id)                       # 404 when the desk is not there
    return strategies.suggest(desk_id, body.symbol, body.view, body.target, body.budget, body.expiration, body.priority)


# ---- the Option detail panel (SPEC-028, delivery 6): profit and loss by date and price of one strategy, its curves and its events ------------

@router.post("/api/desks/{desk_id}/option-detail")
def desk_option_detail(desk_id: int, body: OptionDetailAsk) -> dict:
    service.get_desk(desk_id)                       # 404 when the desk is not there
    return option_detail.option_detail(desk_id, body.symbol, body.expiration, body.legs, body.structure, body.strikes, body.quantity, body.range_pct,
                                       body.iv_mult, body.rows, body.columns)


@router.get("/api/desks/{desk_id}/option-detail/selection")
def option_detail_selection(desk_id: int) -> dict:
    """The strategy the panel opens with (the one chosen in Strategies, by the chief or by the owner), kept in memory; `selection` is null when there is none."""
    service.get_desk(desk_id)
    return option_actions.selection(desk_id)


@router.put("/api/desks/{desk_id}/option-detail/selection")
def option_detail_remember(desk_id: int, body: OptionSelection) -> dict:
    service.get_desk(desk_id)
    return option_actions.remember(desk_id, body.symbol, body.expiration, body.legs, body.structure, body.strikes, body.quantity, body.range_pct, body.iv_mult,
                                   body.strategy_key, body.name)


@router.get("/api/desks/{desk_id}/option-detail/saved")
def option_detail_saved(desk_id: int) -> dict:
    return option_actions.saved_list(desk_id)


@router.post("/api/desks/{desk_id}/option-detail/saved", status_code=201)
def option_detail_save(desk_id: int, body: SaveStrategy) -> dict:
    service.get_desk(desk_id)
    return option_actions.save(desk_id, body.name, body.symbol, body.expiration, body.legs, body.structure, body.strikes, body.quantity)


@router.delete("/api/desks/{desk_id}/option-detail/saved/{key}")
def option_detail_forget(desk_id: int, key: str) -> dict:
    service.get_desk(desk_id)
    return option_actions.delete_saved(desk_id, key)


@router.post("/api/desks/{desk_id}/option-detail/propose")
def option_detail_propose(desk_id: int, body: ProposeStrategy) -> dict:
    """Propose the strategy through the door of execution: it never sends by itself (the level of the desk and the rules of the owner decide)."""
    service.get_desk(desk_id)
    return option_actions.propose(desk_id, body.symbol, body.expiration, body.legs, body.structure, body.strikes, body.quantity, body.strategy_key)
