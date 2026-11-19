"""HTTP surface of brokers and accounts (task 3294, C7-5c). Every route needs the per-boot session token. Errors carry a
stable `code` and a message in the student's language. A reset needs {"confirmed": true}."""
from typing import Any, Callable

from fastapi import APIRouter, Request, Response
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


class Assign(BaseModel):
    account_id: Any


@router.get("/api/brokers")
def list_brokers() -> dict:
    return {"brokers": service.list_brokers()}


@router.post("/api/brokers", status_code=201)
def create_broker(body: NewBroker) -> dict:
    return service.create_broker(body.name, body.commission, body.slippage_bps)


@router.put("/api/brokers/{broker_id}/costs")
def update_costs(broker_id: int, body: Costs) -> dict:
    return service.update_costs(broker_id, body.commission, body.slippage_bps)


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
