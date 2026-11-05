"""REST side of the chat panel: conversation list and search, pin / rename / delete, the agent and project lists
the slash commands complete from, and "save to the project". The live chat itself is the WebSocket in `api.py`.

Every route needs the per-boot session token (`X-Hokma-Token` or `?token=`). Errors are `{"detail": {"code", "message"}}`
and the panel translates by `code`.
"""
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from hokma.core.origin import token_matches
from hokma.db import NotFound, from_db_time, get_database
from hokma.engine.dbstore import DbProjects
from hokma.projects import service as projects_service
from hokma.projects.errors import ProjectError
from hokma.rpc.ws import SESSION_TOKEN

LIST_LIMIT = 200


class ConversationPatch(BaseModel):
    pinned: bool | None = None
    archived: bool | None = None
    title: str | None = Field(default=None, min_length=1, max_length=120)


class SaveRequest(BaseModel):
    """Exactly one of `message_id` (the answer is saved as a .md) or `attachment_id` (the file is copied)."""
    message_id: str | None = Field(default=None, max_length=32)
    attachment_id: str | None = Field(default=None, max_length=32)


def _fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _auth(header_token: str, query_token: str) -> None:
    if not token_matches(header_token or query_token, SESSION_TOKEN):
        raise _fail(401, "unauthorized", "bad token")


def _iso(value: str | None) -> str | None:
    return from_db_time(value).isoformat() if value else None


def _summary(c) -> dict[str, Any]:
    return {"id": str(c.id), "title": c.title or "", "pinned": c.pinned, "archived": c.archived, "model": c.model, "agent": c.agent_slug,
            "project_id": str(c.project_id) if c.project_id is not None else None,
            "message_count": c.message_count, "started_at": _iso(c.started_at),
            "last_message_at": _iso(c.last_message_at or c.started_at)}


def _slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned[:60] or "answer"


def create_router(db_getter=get_database, engine_getter=None) -> APIRouter:
    router = APIRouter()

    def engine():
        if engine_getter is not None:
            return engine_getter()
        from hokma.engine.api import get_engine
        return get_engine()

    def ready() -> bool:
        from hokma.engine.api import database_ready
        return database_ready()

    @router.get("/api/conversations")
    def list_conversations(q: str = Query(default="", max_length=200), archived: bool = Query(default=False),
                           x_hokma_token: str = Header(default=""),
                           token: str = Query(default="")):
        """Pinned first, then by last activity. `q` matches the title and the content of the messages.
        Archived conversations are left out unless `archived=true`, which lists only those."""
        _auth(x_hokma_token, token)
        if not ready():
            return {"conversations": []}
        with db_getter().read() as u:
            rows = u.conversations.list(search=q.strip() or None, archived=archived, limit=LIST_LIMIT)
        return {"conversations": [_summary(c) for c in rows if c.message_count > 0 or c.title]}

    @router.patch("/api/conversations/{conversation_id}")
    def patch_conversation(conversation_id: int, body: ConversationPatch, x_hokma_token: str = Header(default=""),
                           token: str = Query(default="")):
        _auth(x_hokma_token, token)
        if body.pinned is None and body.title is None and body.archived is None:
            raise _fail(400, "bad_request", "nothing to change")
        try:
            with db_getter().write() as u:
                if body.pinned is not None:
                    u.conversations.pin(conversation_id, body.pinned)
                if body.archived is not None:
                    u.conversations.archive(conversation_id, body.archived)
                if body.title is not None:
                    u.conversations.rename(conversation_id, body.title.strip())
                updated = u.conversations.get(conversation_id)
        except NotFound:
            raise _fail(404, "unknown_conversation", "no such conversation") from None
        return _summary(updated)

    @router.delete("/api/conversations/{conversation_id}", status_code=204)
    def delete_conversation(conversation_id: int, x_hokma_token: str = Header(default=""),
                            token: str = Query(default="")):
        _auth(x_hokma_token, token)
        session = engine().sessions.get(str(conversation_id))
        if session is not None and session.state == "running":
            raise _fail(409, "busy", "a turn is running in this conversation")
        try:
            with db_getter().write() as u:
                u.conversations.delete(conversation_id)
        except NotFound:
            raise _fail(404, "unknown_conversation", "no such conversation") from None

    @router.get("/api/chat/options")
    def chat_options(x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        """What the slash commands complete from: active agents and the projects, plus the default project."""
        _auth(x_hokma_token, token)
        if not ready():
            return {"agents": [], "projects": [], "default_project": None}
        default = DbProjects(db_getter()).default()
        with db_getter().read() as u:
            agents = [{"slug": a.slug, "name": a.display_name, "description": a.description or ""}
                      for a in u.agents.list() if a.status != "off"]
            projects = [{"id": str(p.id), "name": p.name} for p in u.projects.list()]
        return {"agents": agents, "projects": projects, "default_project": {"id": default.id, "name": default.name}}

    @router.post("/api/conversations/{conversation_id}/save", status_code=201)
    def save_to_project(conversation_id: int, body: SaveRequest, x_hokma_token: str = Header(default=""),
                        token: str = Query(default="")):
        """Copy an attachment, or save an answer as markdown, into the conversation's project (with its Git commit)."""
        _auth(x_hokma_token, token)
        if (body.message_id is None) == (body.attachment_id is None):
            raise _fail(400, "bad_request", "send message_id or attachment_id")
        db = db_getter()
        try:
            with db.read() as u:
                conv = u.conversations.get(conversation_id)
                message = u.messages.get(int(body.message_id)) if body.message_id else None
                attachment = u.attachments.get(int(body.attachment_id)) if body.attachment_id else None
        except (NotFound, ValueError):
            raise _fail(404, "unknown_item", "no such message or attachment") from None
        if message is not None and message.conversation_id != conv.id:
            raise _fail(404, "unknown_item", "that message is not in this conversation")
        try:
            project_id = conv.project_id
            if project_id is None:
                projects_service.ensure_default(db)
                project_id = projects_service.active_id(db)
            if project_id is None:
                raise _fail(409, "no_project", "there is no project to save into")
            notes = [{"name": f"{_slug(conv.title or 'answer')}-{message.id}.md", "text": message.content}] if message else []
            saved = projects_service.save_from_chat(project_id, [str(attachment.id)] if attachment else [], notes,
                                                    engine().store, db)
            with db.read() as u:
                project = u.projects.get(project_id)
        except ProjectError as exc:
            raise _fail(exc.status, exc.code, str(exc)) from None
        return {"path": saved["saved"][0], "git": saved["git"], "project": {"id": str(project.id), "name": project.name}}

    return router


router = create_router()
