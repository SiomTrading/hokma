"""Hokma side of the Telegram link: where the link is stored, how the owner's text reaches the chat engine, how a
button answers a permission and how a finished routine is announced. The Telegram code itself is `core/`."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from hokma.connections.store import KeyStore
from hokma.core.config import hokma_data_dir
from hokma.db import Database
from hokma.engine.protocols import DEFAULT_MODEL
from hokma.i18n import t
from hokma.telegram.core import TelegramLink
from hokma.telegram.core.link import build_application

log = logging.getLogger("hokma.telegram")
TOKEN_ENTRY = "telegram_bot"            # entry of the bot token in the system credential store
CONVERSATION_TITLE = "Telegram"
TELEGRAM_LIMIT = 4000                  # characters of one message (Telegram allows 4096)
RETRY_SECONDS = 60
PROMPT_LIMIT = 3800                    # characters the approval message can show (core truncates beyond)


class LinkStore:
    """The single link record (`telegram_link`), in the port shape `core/` expects."""

    def __init__(self, get_db: Callable[[], Database]) -> None:
        self._db = get_db

    def load(self) -> dict | None:
        with self._db().read() as u:
            return u.session.one("SELECT * FROM telegram_link WHERE id = 1")

    def save(self, record: dict) -> None:
        with self._db().write() as u:
            u.session.execute("DELETE FROM telegram_link WHERE id = 1")
            u.session.execute("DELETE FROM telegram_outbox WHERE sent_at IS NULL")   # meant for the previous chat
            u.session.execute(
                "INSERT INTO telegram_link (id, chat_id, user_name) VALUES (1, :c, :n)",
                {"c": str(record["chat_id"]), "n": record.get("user_name") or ""})

    def set_conversation(self, conversation_id: str) -> None:
        with self._db().write() as u:
            u.session.execute("UPDATE telegram_link SET conversation_id = :c WHERE id = 1", {"c": conversation_id})

    def clear(self) -> None:
        with self._db().write() as u:
            u.session.execute("DELETE FROM telegram_link")
            u.session.execute("DELETE FROM telegram_outbox WHERE sent_at IS NULL")   # nothing pending outlives the link


def fit(text: str) -> str:
    """Cut to the Telegram limit and say the rest is in Hokma."""
    text = text.strip()
    if len(text) <= TELEGRAM_LIMIT:
        return text
    tail = "\n" + t("telegram.continues")
    return text[:TELEGRAM_LIMIT - len(tail)].rstrip() + tail


def enqueue(unit, text: str) -> int:
    """Put a message for the owner in the outbox (inside an open write unit). The sender delivers it."""
    return unit.session.insert("telegram_outbox", {"text": fit(text)})


class TelegramGlue:
    def __init__(self, get_db: Callable[[], Database], get_engine: Callable[[], Any], *,
                 keys: KeyStore | None = None, application_factory=build_application, data_dir=None) -> None:
        from hokma.engine.engine import Connection

        self._db, self._engine = get_db, get_engine
        self.keys = keys or KeyStore()
        self.store = LinkStore(get_db)
        self.link = TelegramLink(data_dir or hokma_data_dir() / "telegram", self.store, t=t,
                                 on_owner_text=self.on_owner_text, on_decision=self.on_decision,
                                 application_factory=application_factory)
        self._conn = Connection(self._on_event)
        self._turn_text: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._flushing = asyncio.Lock()

    # ----- lifecycle -----

    async def start(self) -> bool:
        """Connect when a token is saved. Without a token nothing happens."""
        token = self.keys.get(TOKEN_ENTRY)
        if not token:
            return False
        await self.link.start(token)
        return True

    async def stop(self) -> None:
        await self.link.stop()

    async def disconnect(self) -> None:
        """Unlink: stop, forget the link, the pairing grants, the token."""
        await self.link.stop()
        self.link.unlink()
        self.keys.delete(TOKEN_ENTRY)

    # ----- the owner writes -----

    def _conversation(self) -> str:
        engine, record = self._engine(), self.store.load() or {}
        cid = record.get("conversation_id")
        if not cid or engine.store.get_conversation(cid) is None:
            cid = engine.store.create_conversation(CONVERSATION_TITLE, DEFAULT_MODEL, None, None).id
            self.store.set_conversation(cid)
        engine.subscribe(self._conn, cid)
        return cid

    async def on_owner_text(self, text: str) -> None:
        """Only called for the linked chat (core checks). The answer comes back through `_on_event`."""
        cid = self._conversation()
        await self._engine().handle(self._conn, json.dumps({"type": "send", "conversation_id": cid, "text": text}))

    async def _on_event(self, raw: str) -> None:
        event = json.loads(raw)
        kind, turn = event.get("type"), event.get("turn_id", "")
        if kind == "delta":
            self._turn_text[turn] = self._turn_text.get(turn, "") + event.get("text", "")
        elif kind == "done":
            text = self._turn_text.pop(turn, "").strip()
            if text and not event.get("stopped"):
                await self.deliver(text)
        elif kind == "error":
            self._turn_text.pop(turn, None)
            await self.deliver(event.get("message", ""))
        elif kind == "permission_request":
            full = json.dumps(event.get("input") or {}, ensure_ascii=False)     # everything the tool will receive
            if len(full) > PROMPT_LIMIT:         # what is approved must be what is shown: too long, so it is denied
                session = self._engine().sessions.get(event.get("conversation_id"))
                if session is not None:
                    await session.resolve(event["request_id"], "deny", "system")
                await self.deliver(t("telegram.prompt_too_long"))
                return
            await self.link.send_exec_approval(full, event["request_id"], event.get("tool", ""))

    # ----- the outbox: nothing is lost if Telegram or Hokma is down -----

    async def deliver(self, text: str) -> None:
        """Store the message and try to send it now; whatever fails is retried by `retry_loop`."""
        if self._store_text(text):
            await self.flush()

    def enqueue(self, text: str) -> None:
        """Same as `deliver` for code that is not async."""
        if self._store_text(text):
            self._spawn(self.flush())

    def _store_text(self, text: str) -> bool:
        if not text.strip():
            return False
        with self._db().write() as u:
            enqueue(u, text)
        return True

    async def flush(self) -> int:
        """Send the pending messages in order. A failed one stays queued and does not hold back the rest, so a message
        Telegram rejects for good cannot wedge the queue. Returns how many were sent."""
        sent = 0
        async with self._flushing:                      # one sender at a time: no message goes out twice
            if not self.link.connected or self.link.linked_chat_id() is None:
                return 0
            with self._db().read() as u:
                rows = u.session.query("SELECT id, text FROM telegram_outbox WHERE sent_at IS NULL ORDER BY id")
            for row in rows:
                result = await self.link.send_text(row["text"])
                with self._db().write() as u:
                    if result.success:
                        u.session.execute("UPDATE telegram_outbox SET sent_at = CURRENT_TIMESTAMP, attempts = attempts + 1, "
                                          "last_error = NULL WHERE id = :i", {"i": row["id"]})
                    else:
                        u.session.execute("UPDATE telegram_outbox SET attempts = attempts + 1, last_error = :e WHERE id = :i",
                                          {"e": (result.error or "")[:300], "i": row["id"]})
                sent += result.success
        return sent

    async def retry_loop(self, seconds: float = RETRY_SECONDS) -> None:
        while True:
            try:
                await self.flush()
            except Exception as exc:                       # the loop must survive a bad tick
                log.warning("telegram outbox tick failed: %s", type(exc).__name__)
            await asyncio.sleep(seconds)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ----- a button answers -----

    async def on_decision(self, request_id: str, choice: str) -> int:
        """1 when a permission was waiting (and is now answered and recorded), 0 when it already ended."""
        engine, cid = self._engine(), (self.store.load() or {}).get("conversation_id")
        session = engine.sessions.get(cid) if cid else None
        pending = session.pending.get(request_id) if session else None
        if pending is None:
            return 0
        allow = choice != "deny"
        if not await session.resolve(request_id, "allow" if allow else "deny", "human"):   # ended meanwhile
            return 0
        with self._db().write() as u:
            u.approvals.record_permission(int(cid), pending.tool, "approved" if allow else "rejected",
                                          channel="telegram", detail=pending.summary.get("target"))
        return 1

    # ----- a routine ended -----

    def notify_job(self, job: Any, outcome: Any) -> None:
        """Scheduler hook: tell the phone when a routine finishes or fails (skipped runs stay silent)."""
        if outcome.status not in ("done", "failed") or self.store.load() is None:
            return
        if outcome.status == "done":
            text = t("telegram.job_done", name=job.name) + "\n\n" + (outcome.detail or "")
        else:
            text = t("telegram.job_failed", name=job.name, detail=(outcome.detail or "")[:300])
        self.enqueue(text)
