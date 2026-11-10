"""engine_connections: one row per way into an engine. Secrets are not here (keyring, service hokma-engine)."""
from __future__ import annotations

import json
from typing import Any

from hokma.db.errors import NotFound, ValidationError
from hokma.db.models import EngineConnection
from hokma.db.repos.base import Repo, flag, require_in
from hokma.db.timeutil import utcnow

ENGINES = ("claude_code", "codex", "gemini_cli", "kimi_code", "api")
AUTH_KINDS = ("subscription", "oauth_token", "api_key", "gateway", "imported", "local")
STATUSES = ("connected", "no_session", "no_cli", "error", "unknown", "removed")


class EngineConnectionsRepo(Repo):
    def add(self, engine: str, label: str, auth_kind: str, *, provider: str | None = None, wire: str | None = None,
            base_url: str | None = None, tier_models: dict[str, str] | None = None,
            settings: dict[str, Any] | None = None, make_default: bool = False) -> EngineConnection:
        require_in("engine", engine, ENGINES)
        require_in("auth_kind", auth_kind, AUTH_KINDS)
        label = " ".join(str(label or "").split())
        if not 1 <= len(label) <= 60:
            raise ValidationError("label must be 1 to 60 characters")
        if wire is not None:
            require_in("wire", wire, ("openai", "anthropic"))
        has_default = self.s.one("SELECT id FROM engine_connections WHERE is_default = 1 AND status <> 'removed'")
        new_id = self.s.insert("engine_connections", {
            "engine": engine, "label": label, "auth_kind": auth_kind, "provider": provider, "wire": wire,
            "base_url": base_url, "tier_models": json.dumps(tier_models) if tier_models else None,
            "settings": json.dumps(settings) if settings else None, "status": "unknown", "is_default": 0,
            "created_at": utcnow(), "updated_at": utcnow()})
        if make_default or has_default is None:
            self.set_default(new_id)
        return self.get(new_id)

    def get(self, id: int) -> EngineConnection:
        return self._get("engine_connections", "id", id, EngineConnection)

    def list(self, *, include_removed: bool = False) -> list[EngineConnection]:
        sql = "SELECT * FROM engine_connections" + ("" if include_removed else " WHERE status <> 'removed'")
        return [EngineConnection.from_row(r) for r in self.s.query(sql + " ORDER BY is_default DESC, id")]

    def default(self) -> EngineConnection | None:
        row = self.s.one("SELECT * FROM engine_connections WHERE is_default = 1 AND status <> 'removed' ORDER BY id")
        return EngineConnection.from_row(row) if row else None

    def set_default(self, id: int) -> EngineConnection:
        self.get(id)
        self.s.execute("UPDATE engine_connections SET is_default = 0 WHERE is_default = 1")
        self.s.update("engine_connections", {"id": id}, {"is_default": 1, "updated_at": utcnow()})
        return self.get(id)

    def set_status(self, id: int, status: str, detail: str | None = None) -> EngineConnection:
        require_in("status", status, STATUSES)
        self.get(id)
        self.s.update("engine_connections", {"id": id}, {
            "status": status, "status_detail": (detail or None) and str(detail)[:500], "checked_at": utcnow(),
            "updated_at": utcnow()})
        return self.get(id)

    def update(self, id: int, *, label: str | None = None, tier_models: dict[str, str] | None = None,
               base_url: str | None = None) -> EngineConnection:
        self.get(id)
        values: dict[str, Any] = {"updated_at": utcnow()}
        if label is not None:
            values["label"] = " ".join(label.split())[:60] or self.get(id).label
        if tier_models is not None:
            values["tier_models"] = json.dumps(tier_models)
        if base_url is not None:
            values["base_url"] = base_url or None
        self.s.update("engine_connections", {"id": id}, values)
        return self.get(id)

    def remove(self, id: int) -> EngineConnection:
        """The row stays with status 'removed'; conversations that used it keep running until they pick another."""
        self.get(id)
        self.s.update("engine_connections", {"id": id}, {"status": "removed", "is_default": 0, "updated_at": utcnow()})
        return self.get(id)

    def for_conversation(self, conversation_id: int) -> EngineConnection | None:
        row = self.s.one("SELECT c.* FROM engine_connections c JOIN conversations v ON v.connection_id = c.id "
                         "WHERE v.id = :id AND c.status <> 'removed'", {"id": conversation_id})
        return EngineConnection.from_row(row) if row else None

    def attach(self, conversation_id: int, connection_id: int | None) -> None:
        if connection_id is not None:
            self.get(connection_id)
        self.s.update("conversations", {"id": conversation_id}, {"connection_id": connection_id})


_ = (NotFound, flag)
