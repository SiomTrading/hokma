"""HTTP surface of brokers and accounts (task 3294, C7-5c). Every route needs the per-boot session token. Errors carry a
stable `code` and a message in the student's language. A reset needs {"confirmed": true}."""
from typing import Any, Callable

from fastapi import APIRouter, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel

from hokma.broker import service
from hokma.core.origin import token_matches
from hokma.db import DbError
from hokma.desks.api import _error
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN


class TokenRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            candidate = request.headers.get("x-hokma-token") or request.query_params.get("token") or ""
            if not token_matches(candidate, SESSION_TOKEN):
                return _error(401, "bad_token", t("desks.err.bad_token"))
            try:
                return await original(request)
            except service.BrokerError as exc:
                return _error(exc.status, exc.code, exc.message)
            except DbError:
                return _error(400, "db_error", t("desks.err.db_failed"))

        return handler


router = APIRouter(route_class=TokenRoute)


class NewBroker(BaseModel):
    name: Any
    commission: Any = None
    slippage_bps: Any = None


class Costs(BaseModel):
    commission: Any = None
    slippage_bps: Any = None


class NewAccount(BaseModel):
    name: Any
    capital: Any


class Reset(BaseModel):
    confirmed: Any = None


class OrdersEnabled(BaseModel):
    enabled: Any = None


class Assign(BaseModel):
    account_id: Any


class OrderBody(BaseModel):
    order: Any = None
    reason: Any = None


class CancelBody(BaseModel):
    reason: Any = None


class LimitsBody(BaseModel):
    limits: Any = None


@router.get("/api/brokers")
def list_brokers() -> dict:
    return {"brokers": service.list_brokers()}


@router.post("/api/brokers", status_code=201)
def create_broker(body: NewBroker) -> dict:
    return service.create_broker(body.name, body.commission, body.slippage_bps)


@router.put("/api/brokers/{broker_id}/costs")
def update_costs(broker_id: int, body: Costs) -> dict:
    return service.update_costs(broker_id, body.commission, body.slippage_bps)


@router.put("/api/brokers/{broker_id}/orders-enabled")
def set_orders_enabled(broker_id: int, body: OrdersEnabled) -> dict:
    """The switch of the orders of a broker that takes them (18.2 point 13). 422 for a broker that cannot."""
    return service.set_orders_enabled(broker_id, body.enabled)


@router.get("/api/brokers/{broker_id}/accounts")
def list_accounts(broker_id: int) -> dict:
    return {"accounts": service.list_accounts(broker_id)}


@router.post("/api/brokers/{broker_id}/accounts", status_code=201)
def create_account(broker_id: int, body: NewAccount) -> dict:
    return service.create_account(broker_id, body.name, body.capital)


@router.get("/api/accounts/{account_id}")
def get_account(account_id: int) -> dict:
    return service.get_account(account_id)


@router.post("/api/accounts/{account_id}/reset")
def reset_account(account_id: int, body: Reset) -> dict:
    return service.reset_account(account_id, body.confirmed)


@router.put("/api/desks/{desk_id}/account")
def assign_account(desk_id: int, body: Assign) -> dict:
    return service.assign_account(desk_id, body.account_id)


@router.delete("/api/desks/{desk_id}/account")
def unassign_account(desk_id: int) -> dict:
    return service.unassign_account(desk_id)


# ---- orders of a desk (SPEC-015 18.2 to 18.6): the same service as the tools of the chat. The app is the owner, so a soft
# condition of 18.3 does not ask again; the refusals (switch off, desk paused, Schwab rejects) still apply. No route returns
# an account number or a hash: an account is its account_ref and mask.

OWNER = service.owner_who()      # holds the start secret of the app (generated in memory, never saved): see service.Who


@router.get("/api/desks/{desk_id}/orders")
def list_orders(desk_id: int, status: str | None = None, fromEnteredTime: str | None = None,  # noqa: N803
                toEnteredTime: str | None = None, maxResults: int | None = Query(default=None)) -> dict:  # noqa: N803
    return service.order_list(desk_id, status, fromEnteredTime, toEnteredTime, maxResults)


@router.get("/api/desks/{desk_id}/orders/log")
def order_log(desk_id: int, since: str | None = None, limit: int = 50) -> dict:
    return service.order_log(desk_id, since, limit)


@router.get("/api/desks/{desk_id}/orders/pending")
def pending_orders(desk_id: int) -> dict:
    """The orders that wait for the owner (18.3), with the decision id to approve or reject."""
    return {"pending": service.list_pending(desk_id)}


@router.post("/api/desks/{desk_id}/orders/pending/{decision_id}/approve")
def approve_pending(desk_id: int, decision_id: int) -> dict:
    """Preview again, apply the rules with the prices of now and send it when it passes; past 30 minutes it only expires."""
    return service.approve_pending(decision_id, desk_id=desk_id, who=OWNER)


@router.post("/api/desks/{desk_id}/orders/pending/{decision_id}/reject")
def reject_pending(desk_id: int, decision_id: int) -> dict:
    return service.reject_pending(decision_id, desk_id=desk_id, who=OWNER)


@router.post("/api/desks/{desk_id}/orders/preview")
def preview_order(desk_id: int, body: OrderBody) -> dict:
    return service.preview(desk_id, body.order, OWNER)


@router.post("/api/desks/{desk_id}/orders")
def place_order(desk_id: int, body: OrderBody) -> dict:
    return service.place(desk_id, body.order, body.reason, OWNER)


@router.get("/api/desks/{desk_id}/orders/{order_id}")
def order_status(desk_id: int, order_id: str) -> dict:
    return service.order_status(desk_id, order_id)


@router.put("/api/desks/{desk_id}/orders/{order_id}")
def replace_order(desk_id: int, order_id: str, body: OrderBody) -> dict:
    return service.replace(desk_id, order_id, body.order, body.reason, OWNER)


@router.post("/api/desks/{desk_id}/orders/{order_id}/cancel")
def cancel_order(desk_id: int, order_id: str, body: CancelBody) -> dict:
    return service.cancel(desk_id, order_id, body.reason, OWNER)


@router.get("/api/desks/{desk_id}/positions")
def list_positions(desk_id: int) -> dict:
    return service.position_list(desk_id)


@router.get("/api/desks/{desk_id}/limits")
def get_limits(desk_id: int) -> dict:
    """The optional limits of a desk (18.5); empty means all off."""
    return {"limits": service.get_limits(desk_id)}


@router.put("/api/desks/{desk_id}/limits")
def set_limits(desk_id: int, body: LimitsBody) -> dict:
    """Only the given keys change; a null turns that limit off."""
    return {"limits": service.set_limits(desk_id, body.limits)}
