"""conversations, messages and attachments."""
from __future__ import annotations

from typing import Any

from hokma.db.errors import NotFound, ValidationError
from hokma.db.models import Attachment, Conversation, Message, dumps_json
from hokma.db.repos.base import UNSET, Repo, flag, given, require_in
from hokma.db.timeutil import utcnow

_CONV = ("SELECT c.*, (SELECT count(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
         "FROM conversations c")


class ConversationsRepo(Repo):
    def create(self, *, engine: str, title: str | None = None, project_id: int | None = None,
               model: str | None = None, agent_slug: str | None = None, pinned: bool = False) -> Conversation:
        if not engine:
            raise ValidationError("engine is required")
        new_id = self.s.insert("conversations", {
            "engine": engine, "title": title, "project_id": project_id, "model": model, "agent_slug": agent_slug,
            "pinned": flag(pinned), "started_at": utcnow()})
        return self.get(new_id)

    def get(self, id: int) -> Conversation:
        row = self.s.one(_CONV + " WHERE c.id = :id", {"id": id})
        if row is None:
            raise NotFound(f"conversations {id!r} not found")
        return Conversation.from_row(row)

    def find(self, id: int) -> Conversation | None:
        try:
            return self.get(id)
        except NotFound:
            return None

    def list(self, *, project_id: int | None = None, pinned: bool | None = None, search: str | None = None,
             archived: bool | None = None, limit: int = 100, offset: int = 0) -> list[Conversation]:
        """`archived`: None = every conversation, False = the visible ones, True = only the archived ones."""
        sql, params = _CONV + " WHERE 1 = 1", {"limit": limit, "offset": offset}
        if project_id is not None:
            sql += " AND c.project_id = :project_id"
            params["project_id"] = project_id
        if pinned is not None:
            sql += " AND c.pinned = :pinned"
            params["pinned"] = flag(pinned)
        if archived is not None:
            sql += " AND c.archived = :archived"
            params["archived"] = flag(archived)
        if search:
            sql += (" AND (c.title LIKE :q ESCAPE '\\' OR EXISTS (SELECT 1 FROM messages m "
                    "WHERE m.conversation_id = c.id AND m.content LIKE :q ESCAPE '\\'))")
            params["q"] = like(search)
        sql += " ORDER BY c.pinned DESC, COALESCE(c.last_message_at, c.started_at) DESC, c.id DESC LIMIT :limit OFFSET :offset"
        return [Conversation.from_row(r) for r in self.s.query(sql, params)]

    def _set(self, id: int, **values: Any) -> Conversation:
        self.get(id)
        self.s.update("conversations", {"id": id}, values)
        return self.get(id)

    def rename(self, id: int, title: str | None) -> Conversation:
        return self._set(id, title=(title or "").strip() or None)

    def pin(self, id: int, pinned: bool) -> Conversation:
        return self._set(id, pinned=flag(pinned))

    def archive(self, id: int, archived: bool) -> Conversation:
        return self._set(id, archived=flag(archived))

    def set_project(self, id: int, project_id: int | None) -> Conversation:
        return self._set(id, project_id=project_id)

    def set_model(self, id: int, model: str | None) -> Conversation:
        return self._set(id, model=model)

    def set_agent(self, id: int, agent_slug: str | None) -> Conversation:
        return self._set(id, agent_slug=agent_slug)

    def set_engine_session(self, id: int, session: str | None) -> Conversation:
        return self._set(id, engine_session=session)

    def delete(self, id: int) -> None:
        self.get(id)
        self.s.execute("DELETE FROM conversations WHERE id = :id", {"id": id})


def like(text: str) -> str:
    """Pattern for LIKE ... ESCAPE '\\' that matches the text literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class MessagesRepo(Repo):
    def add(self, conversation_id: int, role: str, content: str, *, agent_slug: str | None = None,
            model: str | None = None, tokens_in: int = 0, tokens_out: int = 0, status: str = "complete",
            meta: Any = None) -> Message:
        require_in("role", role, ("user", "assistant", "system"))
        require_in("status", status, ("complete", "stopped", "error"))
        if content is None:
            raise ValidationError("content is required")
        now = utcnow()
        new_id = self.s.insert("messages", {
            "conversation_id": conversation_id, "role": role, "agent_slug": agent_slug, "model": model,
            "content": content, "status": status, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "meta": dumps_json(meta) if meta else None, "created_at": now})
        self.s.update("conversations", {"id": conversation_id}, {"last_message_at": now})
        return self.get(new_id)

    def get(self, id: int) -> Message:
        return self._get("messages", "id", id, Message)

    def list(self, conversation_id: int, *, limit: int | None = None, before_id: int | None = None,
             after_id: int | None = None) -> list[Message]:
        sql, params = "SELECT * FROM messages WHERE conversation_id = :c", {"c": conversation_id}
        if before_id is not None:
            sql += " AND id < :before"
            params["before"] = before_id
        if after_id is not None:
            sql += " AND id > :after"
            params["after"] = after_id
        if limit is not None and after_id is None:
            # the last `limit` messages before a point (or the newest ones), returned oldest first
            rows = self.s.query(sql + " ORDER BY id DESC LIMIT :limit", {**params, "limit": limit})
            return [Message.from_row(r) for r in reversed(rows)]
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = limit
        return [Message.from_row(r) for r in self.s.query(sql, params)]

    def update(self, id: int, *, content: Any = UNSET, tokens_in: Any = UNSET, tokens_out: Any = UNSET,
               model: Any = UNSET, status: Any = UNSET) -> Message:
        self.get(id)
        values = given(content=content, tokens_in=tokens_in, tokens_out=tokens_out, model=model, status=status)
        if "status" in values:
            require_in("status", values["status"], ("complete", "stopped", "error"))
        if "content" in values and values["content"] is None:
            raise ValidationError("content is required")
        self.s.update("messages", {"id": id}, values)
        return self.get(id)

    def search(self, text: str, *, limit: int = 50) -> list[Message]:
        rows = self.s.query("SELECT * FROM messages WHERE content LIKE :q ESCAPE '\\' ORDER BY id DESC LIMIT :n",
                            {"q": like(text), "n": limit})
        return [Message.from_row(r) for r in rows]

    def count(self, conversation_id: int) -> int:
        return int(self.s.scalar("SELECT count(*) FROM messages WHERE conversation_id = :c", {"c": conversation_id}))


class AttachmentsRepo(Repo):
    def add(self, *, kind: str, stored_path: str, mime_type: str, size_bytes: int, sha256: str,
            message_id: int | None = None, project_id: int | None = None, transcript: str | None = None,
            name: str | None = None) -> Attachment:
        new_id = self.s.insert("attachments", {
            "kind": kind, "stored_path": stored_path, "mime_type": mime_type, "size_bytes": size_bytes,
            "sha256": sha256, "name": name, "message_id": message_id, "project_id": project_id, "transcript": transcript,
            "created_at": utcnow()})
        return self.get(new_id)

    def get(self, id: int) -> Attachment:
        return self._get("attachments", "id", id, Attachment)

    def list(self, *, kind: str | None = None, project_id: int | None = None, message_id: int | None = None,
             limit: int = 100, offset: int = 0) -> list[Attachment]:
        sql, params = "SELECT * FROM attachments WHERE 1 = 1", {"limit": limit, "offset": offset}
        for column, value in (("kind", kind), ("project_id", project_id), ("message_id", message_id)):
            if value is not None:
                sql += f" AND {column} = :{column}"
                params[column] = value
        rows = self.s.query(sql + " ORDER BY id DESC LIMIT :limit OFFSET :offset", params)
        return [Attachment.from_row(r) for r in rows]

    def by_conversation(self, conversation_id: int) -> dict[int, list[Attachment]]:
        """Attachments of the messages of a conversation, grouped by message id, oldest first."""
        rows = self.s.query("SELECT a.* FROM attachments a JOIN messages m ON m.id = a.message_id "
                            "WHERE m.conversation_id = :c ORDER BY a.id", {"c": conversation_id})
        grouped: dict[int, list[Attachment]] = {}
        for row in rows:
            grouped.setdefault(int(row["message_id"]), []).append(Attachment.from_row(row))
        return grouped

    def link_message(self, id: int, message_id: int) -> Attachment:
        self.get(id)
        self.s.update("attachments", {"id": id}, {"message_id": message_id})
        return self.get(id)

    def set_transcript(self, id: int, text: str | None) -> Attachment:
        self.get(id)
        self.s.update("attachments", {"id": id}, {"transcript": text})
        return self.get(id)

    def delete(self, id: int) -> None:
        self.get(id)
        self.s.execute("DELETE FROM attachments WHERE id = :id", {"id": id})
