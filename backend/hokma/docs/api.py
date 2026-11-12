"""HTTP surface of the project documents (SPEC-009 section 4.5): list, upload, remove and add from a link. Same token rule as projects."""
import email.parser
import email.policy

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from hokma.docs import ingest, web
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


@router.get("/api/projects/{project_id}/documents")
def list_documents(project_id: int) -> dict:
    return ingest.list_documents(project_id)


@router.post("/api/projects/{project_id}/documents", status_code=201)
async def upload_document(project_id: int, request: Request) -> dict:
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
    name, data, description = parsed
    return await run_in_threadpool(lambda: ingest.add_document(project_id, name, data, description=description))


@router.delete("/api/projects/{project_id}/documents/{document_id}")
def remove_document(project_id: int, document_id: int) -> dict:
    return ingest.remove_document(project_id, document_id)


class LinkBody(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class FetchBody(LinkBody):
    title: str | None = Field(default=None, max_length=300)


@router.post("/api/projects/{project_id}/documents/discover")
def discover_documents(project_id: int, body: LinkBody) -> dict:
    web.check_project(project_id)
    return web.discover(body.url)


@router.post("/api/projects/{project_id}/documents/fetch", status_code=201)
def fetch_document(project_id: int, body: FetchBody) -> dict:
    return web.fetch_document(project_id, body.url, body.title)
