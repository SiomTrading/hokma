"""HTTP surface of the projects (D6). Every route needs the per-boot session token (X-Hokma-Token or ?token=)."""
from typing import Callable
from urllib.parse import quote

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from hokma.core.origin import token_matches
from hokma.db import DbError
from hokma.engine import attachments as att
from hokma.i18n import t
from hokma.projects import service
from hokma.projects.errors import ProjectError
from hokma.rpc.ws import SESSION_TOKEN

MAX_UPLOAD_BODY = att.MAX_ATTACHMENT_BYTES + 64 * 1024


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)


class _Route(APIRoute):
    """Token check and error mapping for every route of this router."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            candidate = request.headers.get("x-hokma-token") or request.query_params.get("token") or ""
            if not token_matches(candidate, SESSION_TOKEN):
                return _error(401, "bad_token", t("projects.err.bad_token"))
            try:
                return await original(request)
            except ProjectError as exc:
                return _error(exc.status, exc.code, str(exc))
            except DbError:
                return _error(400, "db_error", t("projects.err.db_failed"))

        return handler


router = APIRouter(route_class=_Route)


class NewProject(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = "other"


class Rename(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class NewLink(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=200)
    download: bool = True


class Note(BaseModel):
    name: str = Field(default="note", max_length=120)
    text: str = Field(min_length=1, max_length=200_000)


class SaveFromChat(BaseModel):
    attachments: list[str] = Field(default_factory=list, max_length=20)
    notes: list[Note] = Field(default_factory=list, max_length=20)


@router.get("/api/projects")
def list_projects() -> dict:
    return service.list_projects()


@router.post("/api/projects", status_code=201)
def create_project(body: NewProject) -> dict:
    return service.create_project(body.name, body.kind)


@router.patch("/api/projects/{project_id}")
def rename_project(project_id: int, body: Rename) -> dict:
    return service.rename_project(project_id, body.name)


@router.post("/api/projects/{project_id}/activate")
def activate_project(project_id: int) -> dict:
    service.activate(project_id)
    return service.list_projects()


@router.get("/api/projects/{project_id}/tree")
def project_tree(project_id: int) -> dict:
    return service.project_tree(project_id)


@router.post("/api/projects/{project_id}/files", status_code=201)
async def upload_file(project_id: int, request: Request, x_filename: str = Header(default="")) -> dict:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BODY:
        raise ProjectError("too_large", 413)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD_BODY:
            raise ProjectError("too_large", 413)
    content_type = request.headers.get("content-type", "")
    parsed = att.parse_multipart(content_type, bytes(body))
    if parsed is not None:
        name, mime, data = parsed
    elif content_type.lower().startswith("multipart/"):
        raise ProjectError("no_file", 400)
    else:
        name, mime, data = x_filename or "file", content_type.split(";")[0], bytes(body)
    return await run_in_threadpool(service.add_file, project_id, name, data, mime)


@router.get("/api/projects/{project_id}/file")
def read_file(project_id: int, path: str = Query(min_length=1, max_length=400),
              limit: int | None = Query(default=None, ge=1, le=att.MAX_ATTACHMENT_BYTES)) -> Response:
    data, mime, name = service.read_file(project_id, path, limit)
    ascii_name = quote(name, safe="")
    return Response(content=data, media_type=mime, headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Disposition": f"inline; filename*=UTF-8''{ascii_name}",
        "Cache-Control": "no-store",
    })


@router.post("/api/projects/{project_id}/links", status_code=201)
def add_link(project_id: int, body: NewLink) -> dict:
    return service.add_link(project_id, body.url, body.title, download=body.download)


@router.post("/api/projects/{project_id}/save", status_code=201)
def save_from_chat(project_id: int, body: SaveFromChat) -> dict:
    from hokma.engine.api import get_engine
    return service.save_from_chat(project_id, body.attachments, [n.model_dump() for n in body.notes],
                                  get_engine().store)
