"""HTTP surface of the extensions, news and system status screens. Every route needs the session token, except the
OAuth callback of remote MCP servers (`oauth_router`), which is authorized by its one-use state."""
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from hokma.db.api import current_database
from hokma.i18n import t
from hokma.status import catalog, checks, extensions_service as service, home, mcp_auth, news
from hokma.status.auth import require_token

router = APIRouter(dependencies=[Depends(require_token)])

PLUGIN_ID = r"^[A-Za-z0-9._-]{1,100}@[A-Za-z0-9._-]{1,100}$"
REQUEST_ID = r"^([0-9a-f]{8}|db-[0-9]{1,9})$"
GITHUB_REPO = r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"


class ToggleBody(BaseModel):
    id: str = Field(pattern=PLUGIN_ID)
    enabled: bool
    confirmed: bool = False


class UninstallBody(BaseModel):
    id: str = Field(pattern=PLUGIN_ID)
    confirmed: bool = False


class MarketplaceBody(BaseModel):
    repo: str = Field(pattern=GITHUB_REPO)


class McpServerBody(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    url: str = Field(min_length=1, max_length=2000)


class McpOpenBody(BaseModel):
    url: str = Field(min_length=1, max_length=8000)


MCP_NAME = Path(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")


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


@router.get("/api/extensions/servers", summary="State of each remote MCP server: needs authorization, connected or error")
def remote_servers() -> dict:
    return {"servers": _call(mcp_auth.servers_state)}


@router.post("/api/extensions/servers", status_code=201, summary="Add a remote MCP server (name and https address)")
def add_remote_server(body: McpServerBody) -> dict:
    return _call(mcp_auth.add_server, body.name, body.url)


@router.post("/api/extensions/servers/{name}/connect",
             summary="Start the sign-in; returns the authorization page to open in the system browser")
def connect_remote_server(request: Request, name: str = MCP_NAME) -> dict:
    server = request.scope.get("server")
    if not server:
        raise HTTPException(status_code=400, detail=t("status.mcp.connect_failed", output="no port"))
    return _call(mcp_auth.start_connect, name, int(server[1]))


@router.post("/api/extensions/servers/{name}/connect/cancel", summary="Cancel a sign-in in progress")
def cancel_remote_connect(name: str = MCP_NAME) -> dict:
    mcp_auth.cancel_connect(name)
    return {"cancelled": True}


@router.post("/api/extensions/servers/{name}/open", summary="Open the sign-in page in the system browser")
def open_remote_connect(body: McpOpenBody, name: str = MCP_NAME) -> dict:
    return {"opened": _call(mcp_auth.open_in_browser, name, body.url)}


@router.post("/api/extensions/servers/{name}/logout", summary="Revoke and forget the stored access of a remote MCP server")
def revoke_remote_server(name: str = MCP_NAME) -> dict:
    return _call(mcp_auth.revoke, name)


@router.get("/api/home", summary="Numbers for the home screen")
def home_numbers() -> dict:
    return home.collect()


@router.post("/api/extensions/uninstall", summary="Uninstall a plugin; needs the owner's confirmation")
def uninstall_extension(body: UninstallBody) -> dict:
    if not body.confirmed:
        raise HTTPException(status_code=400, detail=t("status.ext.not_confirmed"))
    return _call(service.uninstall_plugin, body.id)


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


# The browser comes back here from the authorization server. It carries no session token (it is a plain navigation in
# the system browser): the one-use `state` of a sign-in started from the panel is what authorizes it.
oauth_router = APIRouter()

_PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>Hokma</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<style>body{{font-family:system-ui,sans-serif;background:#0f1115;color:#e6e6e6;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0}}main{{max-width:440px;padding:28px;text-align:center}}
h1{{font-size:20px;margin:0 0 10px}}p{{opacity:.8;line-height:1.5}}</style></head>
<body><main><h1>{title}</h1><p>{text}</p></main></body></html>"""


@oauth_router.get("/oauth/mcp/callback", include_in_schema=False)
def oauth_callback(request: Request):
    import html

    from fastapi.responses import HTMLResponse

    from hokma.i18n import language

    params = {k: v[:4000] for k, v in request.query_params.items()
              if k in ("code", "state", "iss", "error", "error_description")}
    ok, detail = mcp_auth.finish_connect(params)
    if ok:
        title, text = t("status.mcp.page.ok_title", name=detail), t("status.mcp.page.ok_text")
    else:
        title, text = t("status.mcp.page.fail_title"), t("status.mcp.page.fail_text", output=detail)
    body = _PAGE.format(lang=language(), title=html.escape(title), text=html.escape(text))
    return HTMLResponse(body, status_code=200 if ok else 400,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
