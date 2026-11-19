"""HTTP surface of the Today card. Every route needs the per-boot session token (X-Hokma-Token or ?token=).
Errors carry a stable `code` and a message in the student's language, in the same shape as the desks routes."""
from typing import Any, Callable

from fastapi import APIRouter, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel

from hokma.core.origin import token_matches
from hokma.db import DbError, NotFound
from hokma.desks.api import _error
from hokma.i18n import t
from hokma.kit import company_tools
from hokma.rpc.ws import SESSION_TOKEN
from hokma.today import service


class TokenRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            candidate = request.headers.get("x-hokma-token") or request.query_params.get("token") or ""
            if not token_matches(candidate, SESSION_TOKEN):
                return _error(401, "bad_token", t("desks.err.bad_token"))
            try:
                return await original(request)
            except service.TodayError as exc:
                return _error(400, exc.code, str(exc))
            except NotFound:
                return _error(400, "job", str(service.TodayError("job")))
            except DbError:
                return _error(400, "db_error", t("desks.err.db_failed"))

        return handler


router = APIRouter(route_class=TokenRoute)


class Config(BaseModel):
    config: Any


@router.get("/api/today")
def get_today() -> dict:
    return service.today()


@router.put("/api/today/config")
def put_config(body: Config) -> dict:
    return {"config": service.save_config(body.config)}


@router.post("/api/today/refresh")
def refresh() -> dict:
    company_tools.forget_cache()   # forget the short cache so the sources are asked again
    return service.today()
