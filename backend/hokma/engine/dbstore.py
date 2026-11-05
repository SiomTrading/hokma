"""The engine protocols on top of the SQLite repositories (`hokma.db`).

The attachment name and the message status (`complete`, `stopped`, `error`) are columns; the answers to permission
prompts go to `approvals` (a person decided) or `guard_events` (timeout or stop).
"""
import json
import uuid
from pathlib import Path
from typing import Any

from hokma.core.config import hokma_home
from hokma.db import Database, IntegrityViolation, NotFound, get_database
from hokma.engine.attachments import attachments_dir
from hokma.learn.progress import FIRST_CONVERSATION, FIRST_IMAGE
from hokma.engine.protocols import (DEFAULT_MODEL, AttachmentRecord, ConversationRecord, MessageRecord,
                                    ProjectRecord)

ENGINE_NAME = "claude_code"
META_LIMIT = 60_000                 # messages.meta holds at most 64000 characters
_GUARD_DECISIONS = ("allowed_by_human", "denied_by_human")
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
        "application/pdf": ".pdf"}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class DbChatStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    @staticmethod
    def _conv(c) -> ConversationRecord:
        return ConversationRecord(str(c.id), c.title or "", c.model or DEFAULT_MODEL, c.agent_slug,
                                  str(c.project_id) if c.project_id is not None else None, c.engine_session)

    def create_conversation(self, title, model, agent, project_id):
        with self.db.write() as u:
            created = u.conversations.create(engine=ENGINE_NAME, title=title or None, model=model,
                                             agent_slug=agent, project_id=_int(project_id))
            u.onboarding.mark(FIRST_CONVERSATION)
            return self._conv(created)

    def get_conversation(self, conversation_id):
        number = _int(conversation_id)
        if number is None:
            return None
        with self.db.read() as u:
            found = u.conversations.find(number)
        return self._conv(found) if found else None

    def update_conversation(self, conversation_id, **fields):
        calls = {"title": "rename", "model": "set_model", "agent": "set_agent", "engine_session": "set_engine_session"}
        with self.db.write() as u:
            for key, value in fields.items():
                if key == "project_id":
                    u.conversations.set_project(int(conversation_id), _int(value))
                elif key in calls:
                    getattr(u.conversations, calls[key])(int(conversation_id), value or None)
                else:
                    raise KeyError(key)

    def add_message(self, conversation_id, role, text, tokens_in=0, tokens_out=0, status="complete", meta=None):
        meta = dict(meta or {})
        ids = [n for n in (_int(i) for i in meta.get("attachments") or []) if n is not None]
        stored = {"tools": meta["tools"]} if meta.get("tools") else None
        if stored is not None and len(json.dumps(stored)) > META_LIMIT:
            stored = {"tools": [{**t, "result": None} for t in stored["tools"]]}
        with self.db.write() as u:
            conv = u.conversations.get(int(conversation_id))
            m = u.messages.add(conv.id, role, text, model=conv.model if role == "assistant" else None,
                               tokens_in=tokens_in, tokens_out=tokens_out, status=status, meta=stored)
            images = False
            for number in ids:
                linked = u.attachments.link_message(number, m.id)
                images = images or linked.kind == "image"
            if images:
                u.onboarding.mark(FIRST_IMAGE)
        return self._message(m, status, meta)

    def _message(self, m, status: str | None = None, meta=None) -> MessageRecord:
        from hokma.db import from_db_time
        return MessageRecord(str(m.id), str(m.conversation_id), m.role, m.content, m.tokens_in, m.tokens_out,
                             status or m.status,
                             from_db_time(m.created_at).timestamp(), dict(meta or {}))

    def list_messages(self, conversation_id):
        number = _int(conversation_id)
        if number is None:
            return []
        with self.db.read() as u:
            files = u.attachments.by_conversation(number)
            return [self._message(m, None, {
                **(m.meta if isinstance(m.meta, dict) else {}),
                **({"attachments": [str(a.id) for a in files[m.id]]} if m.id in files else {})})
                for m in u.messages.list(number)]

    def add_attachment(self, sha256, name, mime, size):
        kind = "image" if mime.startswith("image/") else "audio" if mime.startswith("audio/") else "file"
        base = str(attachments_dir() / sha256)
        with self.db.write() as u:
            try:
                a = u.attachments.add(kind=kind, stored_path=base, mime_type=mime, size_bytes=size, sha256=sha256,
                                      name=name[:255])
            except IntegrityViolation:                  # same content uploaded again: stored_path is UNIQUE
                a = u.attachments.add(kind=kind, stored_path=f"{base}?{uuid.uuid4().hex[:8]}", mime_type=mime,
                                      size_bytes=size, sha256=sha256, name=name[:255])
        return AttachmentRecord(str(a.id), sha256, name, mime, size)

    def set_transcript(self, attachment_id, text):
        """Text of a voice note (kind 'audio'); the Library shows it next to the audio."""
        number = _int(attachment_id)
        if number is not None:
            with self.db.write() as u:
                u.attachments.set_transcript(number, text)

    def get_attachment(self, attachment_id):
        number = _int(attachment_id)
        if number is None:
            return None
        try:
            with self.db.read() as u:
                a = u.attachments.get(number)
        except NotFound:
            return None
        name = a.name or f"{a.sha256[:12]}{_EXT.get(a.mime_type, '')}"
        return AttachmentRecord(str(a.id), a.sha256, name, a.mime_type, a.size_bytes)

    def record_permission(self, conversation_id, tool, summary, decision, decided_by_kind, channel):
        """A person's answer goes to approvals (+ the guard log); a timeout or a stop is a system block."""
        detail = f"{summary.get('kind', '')} {summary.get('target', '')}".strip()
        number = _int(conversation_id)
        with self.db.write() as u:
            if decision in _GUARD_DECISIONS and decided_by_kind == "human" and number is not None:
                verdict = "approved" if decision == "allowed_by_human" else "rejected"
                u.approvals.record_permission(number, tool, verdict, channel=channel if channel in
                                              ("dashboard", "telegram", "cli") else "dashboard", detail=detail)
                u.guard_events.record("permission", decision, f"{tool}: {detail}"[:1000], tool=tool)
            else:
                u.guard_events.record_permission_ended(tool, f"{decision} ({decided_by_kind} via {channel}): {detail}"[:1000])


class DbProjects:
    """Projects table; with no project (or an unknown id) the default is the working folder from the profile."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    @staticmethod
    def _rec(p) -> ProjectRecord:
        return ProjectRecord(str(p.id), p.name, p.path)

    def default(self) -> ProjectRecord:
        """The active project (picked in the projects panel); with none, the working folder itself."""
        from hokma.projects.service import active_id
        number = active_id(self.db)
        with self.db.read() as u:
            found = u.projects.find(number) if number is not None else None
            workdir = u.profile.get_or_default().workdir
        if found is not None:
            return self._rec(found)
        path = Path(workdir) if workdir else hokma_home()
        return ProjectRecord("default", path.name or "Hokma", str(path))

    def get(self, project_id):
        number = _int(project_id)
        if number is not None:
            with self.db.read() as u:
                found = u.projects.find(number)
            if found:
                return self._rec(found)
        return self.default()

    def find_by_name(self, name):
        if not name:
            return None
        with self.db.read() as u:
            for p in u.projects.list():
                if name.lower() in (p.name.lower(), p.slug.lower()):
                    return self._rec(p)
        return None


class DbAgents:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    def exists(self, slug: str, project: ProjectRecord) -> bool:
        with (self._db or get_database()).read() as u:
            found = u.agents.find(slug)
            return found is not None and found.status == "active"
