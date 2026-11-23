"""Cache and audit trail of model answers (SPEC-017: AH-49).

An identical question (same agent, same model, same prompt) is answered from disk instead of paying for a second call. Every
write and every hit is also appended to `audit.jsonl` next to the cache, so the owner can see what was asked, when, and
what was served from the cache. A corrupt cache entry is a miss and is rewritten; a corrupt audit line never blocks a call.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hokma.desks.core.hashing import render
from hokma.desks.core.paths import llm_cache_dir

AUDIT_FILE = "audit.jsonl"


def prompt_key(agent: str, model: str, system: str, user: str) -> str:
    """Cache key of one (agent, model, prompt) combination: 24 hex characters of a sha256."""
    payload = f"{agent}|{model}|{system}|{user}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def messages_key(agent: str, model: str, messages: list[dict], schema: dict | None = None) -> str:
    """Key of a chat message list (and the schema asked for). Same content, same key, whatever the dict order."""
    digest = hashlib.sha256(render({"agent": agent, "model": model, "messages": messages, "schema": schema})).hexdigest()
    return digest[:24]


class PromptCache:
    """One JSON file per key, plus an append-only audit trail."""

    def __init__(self, cache_dir: Path | str | None = None) -> None:
        self._dir = Path(cache_dir) if cache_dir is not None else llm_cache_dir()
        self.hits = 0
        self.misses = 0

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def audit_path(self) -> Path:
        return self._dir / AUDIT_FILE

    def _path(self, key: str) -> Path:
        if not key or not all(c in "0123456789abcdef" for c in key) or len(key) > 64:
            raise ValueError(f"bad cache key: {key!r}")
        return self._dir / f"{key}.json"

    def get(self, key: str, *, agent: str = "", model: str = "") -> dict | None:
        """The stored record, or None (missing or corrupt entry). A hit is written to the audit trail."""
        path = self._path(key)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.misses += 1
            return None
        if not isinstance(record, dict):
            self.misses += 1
            return None
        self.hits += 1
        self._audit({"event": "hit", "key": key, "agent": agent or record.get("agent", ""), "model": model or record.get("model", "")})
        return record

    def put(self, key: str, record: dict[str, Any], *, agent: str = "", model: str = "") -> None:
        """Store a record atomically (temp file, then replace) and write it to the audit trail."""
        path = self._path(key)
        self._dir.mkdir(parents=True, exist_ok=True)
        stored = {**record, "created_at": datetime.now(UTC).isoformat()}
        handle, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=f"{key}.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as tmp:
                json.dump(stored, tmp, indent=2, ensure_ascii=False)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        self._audit({"event": "store", "key": key, "agent": agent or record.get("agent", ""), "model": model or record.get("model", "")})

    def _audit(self, row: dict[str, Any]) -> None:
        row = {"at": datetime.now(UTC).isoformat(), **row}
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass                                             # the trail is evidence, not a dependency of the answer

    def audit_rows(self) -> list[dict[str, Any]]:
        """The audit trail, oldest first. A line that is not JSON is skipped."""
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows


class CachedClient:
    """Wraps an `LLMClient`: an identical call is answered from the cache. A reply with no text is never stored."""

    def __init__(self, inner: Any, cache: PromptCache, *, agent: str) -> None:
        self.inner, self.cache, self.agent = inner, cache, agent

    @property
    def model(self) -> str:
        return self.inner.model

    def complete(self, messages: list[dict], *, schema: dict | None = None, max_tokens: int = 4096):
        from hokma.llm.protocol import LLMReply

        key = messages_key(self.agent, self.model, messages, schema)
        stored = self.cache.get(key, agent=self.agent, model=self.model)
        if stored is not None and isinstance(stored.get("text"), str):
            return LLMReply(text=stored["text"], model=self.model, usage=dict(stored.get("usage") or {}), cached=True,
                            attempts=0, parsed=stored.get("parsed"))
        reply = self.inner.complete(messages, schema=schema, max_tokens=max_tokens)
        if reply.text.strip():
            self.cache.put(key, {"agent": self.agent, "model": self.model, "text": reply.text, "usage": reply.usage,
                                 "parsed": reply.parsed}, agent=self.agent, model=self.model)
        return reply
