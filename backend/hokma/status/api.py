"""HTTP surface of the extensions, news and system status screens. Every route needs the session token."""
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from hokma.db.api import current_database
from hokma.i18n import t
from hokma.status import catalog, checks, extensions_service as service, home, news
from hokma.status.auth import require_token

router = APIRouter(dependencies=[Depends(require_token)])

PLUGIN_ID = r"^[A-Za-z0-9._-]{1,100}@[A-Za-z0-9._-]{1,100}$"
REQUEST_ID = r"^([0-9a-f]{8}|db-[0-9]{1,9})$"
GITHUB_REPO = r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"


class ToggleBody(BaseModel):
    id: str = Field(pattern=PLUGIN_ID)
    enabled: bool
    confirmed: bool = False


class MarketplaceBody(BaseModel):
    repo: str = Field(pattern=GITHUB_REPO)


def _call(fn, *args):
    try:
        return fn(*args)
    except service.ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None


@router.get("/api/status", summary="System checks: what `hokma doctor` reports plus the database")
def system_status() -> dict:
    return checks.collect()


@router.get("/api/news", summary="Installed version, CHANGELOG and the latest published version")
def system_news(request: Request, refresh: bool = Query(default=False)) -> dict:
    return request.app.state.updates.check(current_database(request), refresh)


@router.get("/api/extensions", summary="Installed extensions, marketplaces and pending agent requests")
def list_extensions() -> dict:
    return service.listing()


@router.get("/api/extensions/marketplace", summary="Plugins of the Anthropic marketplace, read from its repository")
def anthropic_marketplace(q: str = Query(default="", max_length=100), limit: int = Query(default=60, ge=1, le=200),
                          refresh: bool = Query(default=False)) -> dict:
    return _call(catalog.listing, q, limit, refresh)


@router.post("/api/extensions/marketplace", status_code=201, summary="Add the Anthropic marketplace to your list")
def add_anthropic_marketplace() -> dict:
    return _call(service.add_marketplace, catalog.REPO)


@router.get("/api/home", summary="Numbers for the home screen")
def home_numbers() -> dict:
    return home.collect()


@router.post("/api/extensions/toggle", summary="Turn a plugin on or off; needs the owner's confirmation")
def toggle_extension(body: ToggleBody) -> dict:
    if not body.confirmed:
        raise HTTPException(status_code=400, detail=t("status.ext.not_confirmed"))
    return _call(service.toggle_plugin, body.id, body.enabled)


@router.post("/api/extensions/marketplaces", status_code=201, summary="Add a GitHub marketplace by owner/repo")
def add_marketplace(body: MarketplaceBody) -> dict:
    return _call(service.add_marketplace, body.repo)


@router.post("/api/extensions/requests/{request_id}/approve", summary="Approve a request made by an agent")
def approve_request(request_id: str = Path(pattern=REQUEST_ID)) -> dict:
    return _call(service.approve, request_id)


@router.post("/api/extensions/requests/{request_id}/reject", summary="Reject a request made by an agent")
def reject_request(request_id: str = Path(pattern=REQUEST_ID)) -> dict:
    return _call(service.reject, request_id)
