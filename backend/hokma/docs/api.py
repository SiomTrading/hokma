"""HTTP surface of the Library documents (SPEC-010 section 4.8): list, upload, remove, move and add from a link, plus
the 0.0.26 per-project aliases. Same token rule as projects."""
import email.parser
import email.policy

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from hokma.core import session
from hokma.docs import diskfolders, folder_worker, folders, ingest, picker, web
from hokma.projects.api import TokenRoute
from hokma.projects.errors import ProjectError

router = APIRouter(route_class=TokenRoute)

MAX_UPLOAD_BODY = ingest.MAX_DOCUMENT_BYTES + 256 * 1024


def parse_upload(content_type: str, body: bytes) -> tuple[str, bytes, str | None] | None:
    """(file name, bytes, description) of a multipart/form-data body, or None when it carries no file part."""
    if not content_type.lower().startswith("multipart/"):
        return None
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1", "replace")
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + body)
    if not message.is_multipart():
        return None
    found: tuple[str, bytes] | None = None
    description: str | None = None
    for part in message.iter_parts():
        field = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        if filename is not None or field == "file":
            if found is None:
                found = (filename or "file", payload)
        elif field == "description":
            description = payload.decode("utf-8", errors="replace")
    return (found[0], found[1], description) if found else None


class LinkBody(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class FetchBody(LinkBody):
    title: str | None = Field(default=None, max_length=300)
    folder_id: int | None = None


class NewFolder(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: int | None = None


class RenameFolder(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class MoveBody(BaseModel):
    library_folder_id: int | None


async def _read_upload(request: Request) -> tuple[str, bytes, str | None]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BODY:
        raise ProjectError("doc_too_large", 413)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD_BODY:
            raise ProjectError("doc_too_large", 413)
    parsed = parse_upload(request.headers.get("content-type", ""), bytes(body))
    if parsed is None:
        raise ProjectError("no_file", 400)
    return parsed


async def _upload(request: Request, folder_id: int | None, origin_project_id: int | None) -> dict:
    name, data, description = await _read_upload(request)
    return await run_in_threadpool(lambda: ingest.add_library_document(
        name, data, description=description, folder_id=folder_id, origin_project_id=origin_project_id))


# ------------------------------------------------------------------------------------------------ the Library
@router.get("/api/library/documents")
def list_library(folder_id: int | None = None) -> dict:
    return ingest.list_library_documents(folder_id)


@router.post("/api/library/documents", status_code=201)
async def upload_library(request: Request, folder_id: int | None = None) -> dict:
    return await _upload(request, folder_id, None)


@router.post("/api/library/documents/discover")
def discover_library(body: LinkBody) -> dict:
    return web.discover(body.url)


@router.post("/api/library/documents/fetch", status_code=201)
def fetch_library(body: FetchBody) -> dict:
    return web.fetch_document(body.url, body.title, folder_id=body.folder_id)


@router.patch("/api/library/documents/{document_id}")
def move_library(document_id: int, body: MoveBody) -> dict:
    return ingest.move_library_document(document_id, body.library_folder_id)


@router.delete("/api/library/documents/{document_id}")
def remove_library(document_id: int) -> dict:
    return ingest.remove_library_document(document_id)


@router.get("/api/library/status")
def library_status() -> dict:
    return folder_worker.status()


# ------------------------------------------------------------------------------------------------ Library folders
@router.get("/api/library/folders")
def list_library_folders(parent_id: int | None = None) -> dict:
    return folders.list_folders(parent_id)


@router.post("/api/library/folders", status_code=201)
def create_library_folder(body: NewFolder) -> dict:
    return folders.create_folder(body.name, body.parent_id)


@router.patch("/api/library/folders/{folder_id}")
def rename_library_folder(folder_id: int, body: RenameFolder) -> dict:
    return folders.rename_folder(folder_id, body.name)


@router.delete("/api/library/folders/{folder_id}")
def remove_library_folder(folder_id: int, confirm: bool = False) -> dict:
    return folders.remove_folder(folder_id, confirm)


# ------------------------------------------------------------------------------------------------ 0.0.26 aliases
# The per-project routes stay for the 0.0.26 app that is still open while the update runs. They answer exactly like
# the Library routes (the project only has to exist, and it is recorded as the origin of what it uploads).
@router.get("/api/projects/{project_id}/documents")
def list_documents(project_id: int, folder_id: int | None = None) -> dict:
    web.check_project(project_id)
    return ingest.list_library_documents(folder_id)


@router.post("/api/projects/{project_id}/documents", status_code=201)
async def upload_document(project_id: int, request: Request, folder_id: int | None = None) -> dict:
    web.check_project(project_id)
    return await _upload(request, folder_id, project_id)


@router.delete("/api/projects/{project_id}/documents/{document_id}")
def remove_document(project_id: int, document_id: int) -> dict:
    web.check_project(project_id)
    return ingest.remove_library_document(document_id)


@router.post("/api/projects/{project_id}/documents/discover")
def discover_documents(project_id: int, body: LinkBody) -> dict:
    web.check_project(project_id)
    return web.discover(body.url)


@router.post("/api/projects/{project_id}/documents/fetch", status_code=201)
def fetch_document(project_id: int, body: FetchBody) -> dict:
    web.check_project(project_id)
    return web.fetch_document(body.url, body.title, folder_id=body.folder_id, origin_project_id=project_id)


# ---- disk folders of a project and the system folder dialog (SPEC-010 sections 4.3 and 4.8) ----

class NewDiskFolder(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


@router.get("/api/projects/{project_id}/folders")
def list_disk_folders(project_id: int) -> dict:
    return diskfolders.list_folders(project_id)


def _owner(header: str) -> None:
    """Only the open tab that registered the owner confirmation may widen what the agent reads: a token read from the
    browser profile is not enough."""
    if not session.owner_matches(header):
        raise ProjectError("owner_confirmation_required", 403)


@router.post("/api/projects/{project_id}/folders", status_code=201)
def add_disk_folder(project_id: int, body: NewDiskFolder, x_hokma_owner: str = Header(default="")) -> dict:
    _owner(x_hokma_owner)
    return diskfolders.add_folder(project_id, body.path)


@router.delete("/api/projects/{project_id}/folders/{folder_id}")
def remove_disk_folder(project_id: int, folder_id: int) -> dict:
    return diskfolders.remove_folder(project_id, folder_id)


@router.post("/api/projects/{project_id}/folders/{folder_id}/rescan")
async def rescan_disk_folder(project_id: int, folder_id: int) -> dict:
    return await run_in_threadpool(diskfolders.rescan_folder, project_id, folder_id)


@router.post("/api/system/pick-folder")
async def pick_folder(x_hokma_owner: str = Header(default="")) -> dict:
    """Open the system's folder dialog. `{available, path, cancelled}`; the chosen path is checked when it is added."""
    _owner(x_hokma_owner)
    return (await run_in_threadpool(picker.pick_folder)).public()
