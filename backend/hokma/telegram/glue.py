"""Hokma side of the Telegram link: where the link is stored, how the owner's text reaches the chat engine, how a
button answers a permission and how a finished routine is announced. The Telegram code itself is `core/`."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable

from hokma.connections.store import KeyStore
from hokma.core.config import hokma_data_dir
from hokma.db import Database
from hokma.engine.protocols import DEFAULT_MODEL
from hokma.i18n import t
from hokma.telegram.core import TelegramLink
from hokma.telegram.core.link import build_application
from hokma.telegram.core.session_policy import SessionResetPolicy, should_reset

log = logging.getLogger("hokma.telegram")
TOKEN_ENTRY = "telegram_bot"            # entry of the bot token in the system credential store
CONVERSATION_TITLE = "Telegram"
TELEGRAM_LIMIT = 4000                  # characters of one message (Telegram allows 4096)
RETRY_SECONDS = 60
POLICY = SessionResetPolicy(mode="both", idle_minutes=120, at_hour=4)     # local time
PROMPT_LIMIT = 3800                    # characters the approval message can show (core truncates beyond)


class LinkStore:
    """The single link record (`telegram_link`: private chat, group, Avisos topic), in the port shape `core/` expects."""

    def __init__(self, get_db: Callable[[], Database]) -> None:
        self._db = get_db

    def load(self) -> dict | None:
        with self._db().read() as u:
            return u.session.one("SELECT * FROM telegram_link WHERE id = 1")

    def save(self, record: dict) -> None:
        chat = str(record["chat_id"])
        with self._db().write() as u:
            old = u.session.one("SELECT chat_id, group_id FROM telegram_link WHERE id = 1")
            u.session.execute("DELETE FROM telegram_link WHERE id = 1")
            if old is not None and old["chat_id"] != chat:       # the queue was meant for the previous chat
                u.session.execute("DELETE FROM telegram_outbox WHERE sent_at IS NULL")
            elif old is not None and old["group_id"] != record.get("group_id"):    # topic ids belong to one group
                u.session.execute("DELETE FROM telegram_outbox WHERE sent_at IS NULL AND thread_id > 0")
                u.session.execute("DELETE FROM telegram_session WHERE thread_id > 0")
            u.session.execute(
                "INSERT INTO telegram_link (id, chat_id, user_name, group_id, avisos_thread_id, avisos_group_id, avisos) "
                "VALUES (1, :c, :n, :g, :t, :ag, :a)",
                {"c": chat, "n": record.get("user_name") or "", "g": record.get("group_id"),
                 "t": record.get("avisos_thread_id"), "ag": record.get("avisos_group_id"), "a": record.get("avisos")})

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


def enqueue(unit, text: str, thread_id: int | None = None) -> int:
    """Put a message for the owner in the outbox (inside an open write unit). The sender delivers it.
    `thread_id`: a topic of the group, 0 for the private chat, None for Avisos."""
    return unit.session.insert("telegram_outbox", {"text": fit(text), "thread_id": thread_id})


class TelegramGlue:
    def __init__(self, get_db: Callable[[], Database], get_engine: Callable[[], Any], *,
                 keys: KeyStore | None = None, application_factory=build_application, data_dir=None, now=datetime.now) -> None:
        from hokma.engine.engine import Connection

        self._db, self._engine = get_db, get_engine
        self.keys = keys or KeyStore()
        self.store = LinkStore(get_db)
        self.link = TelegramLink(data_dir or hokma_data_dir() / "telegram", self.store, t=t,
                                 on_owner_text=self.on_owner_text, on_decision=self.on_decision, on_reset=self.on_reset,
                                 application_factory=application_factory)
        self._connection, self._conns = Connection, {}          # one per conversation, so its answers know their topic
        self._now = now
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

    def _busy(self, cid: str) -> bool:
        """A turn is running or a permission waits (read only): such a session is never closed."""
        session = self._engine().sessions.get(cid)
        task = getattr(session, "_task", None)
        return session is not None and (bool(getattr(session, "pending", None)) or (task is not None and not task.done()))

    def _touch(self, cid: str) -> None:
        with self._db().write() as u:
            u.session.execute("UPDATE telegram_session SET updated_at = :n WHERE conversation_id = :c",
                              {"n": int(self._now().timestamp()), "c": cid})

    def _conversation(self, thread_id: int, topic_name: str | None) -> tuple[str, str | None]:
        """The conversation of (linked chat, topic): created on first use, replaced when the reset policy says so.
        Returns it with the reset reason (`idle`, `daily`) when this call replaced an overdue one."""
        engine, chat = self._engine(), str(self.link.linked_chat_id())
        key = {"c": chat, "t": thread_id}
        with self._db().read() as u:
            row = u.session.one("SELECT conversation_id, topic_name, updated_at FROM telegram_session "
                                "WHERE chat_id = :c AND thread_id = :t", key)
        reason = None
        if row is not None and engine.store.get_conversation(row["conversation_id"]) is not None:
            reason = should_reset(POLICY, datetime.fromtimestamp(row["updated_at"]), self._now(),
                                  self._busy(row["conversation_id"]))
            if reason is None:
                cid = row["conversation_id"]
                if topic_name and topic_name != row["topic_name"]:
                    with self._db().write() as u:
                        u.session.execute("UPDATE telegram_session SET topic_name = :n WHERE chat_id = :c AND thread_id = :t",
                                          {**key, "n": topic_name})
        if row is None or reason is not None or engine.store.get_conversation(row["conversation_id"]) is None:
            name = topic_name or (row or {}).get("topic_name") or CONVERSATION_TITLE
            cid = engine.store.create_conversation(f"{name} {self._now():%Y-%m-%d}", DEFAULT_MODEL, None, None).id
            with self._db().write() as u:
                u.session.execute("DELETE FROM telegram_session WHERE chat_id = :c AND thread_id = :t", key)
                u.session.insert_row("telegram_session", {"chat_id": chat, "thread_id": thread_id, "conversation_id": cid,
                                                          "topic_name": name, "updated_at": int(self._now().timestamp())})
        if cid not in self._conns:
            self._conns[cid] = self._connection(lambda raw, th=thread_id: self._on_event(raw, th))
        engine.subscribe(self._conns[cid], cid)
        return cid, reason

    async def on_owner_text(self, text: str, thread_id: int | None = None, topic_name: str | None = None) -> None:
        """Only called for the linked chat (core checks); `thread_id` is None for the private chat. The answer comes
        back through `_on_event`, to the same topic."""
        thread = thread_id or 0
        cid, reason = self._conversation(thread, topic_name)
        if reason:
            await self.deliver(t(f"telegram.reset_{reason}"), thread)
        self._touch(cid)
        await self._engine().handle(self._conns[cid], json.dumps({"type": "send", "conversation_id": cid, "text": text}))

    def on_reset(self, thread_id: int) -> None:
        """`/new` in a topic: forget its session; the next message opens another and the old conversation stays."""
        with self._db().write() as u:
            u.session.execute("DELETE FROM telegram_session WHERE chat_id = :c AND thread_id = :t",
                              {"c": str(self.link.linked_chat_id()), "t": thread_id})

    async def _on_event(self, raw: str, thread_id: int = 0) -> None:
        event = json.loads(raw)
        kind, turn = event.get("type"), event.get("turn_id", "")
        if kind == "delta":
            self._turn_text[turn] = self._turn_text.get(turn, "") + event.get("text", "")
        elif kind == "done":
            if event.get("conversation_id") in self._conns:
                self._touch(event["conversation_id"])           # a long turn counts as activity
            text = self._turn_text.pop(turn, "").strip()
            if text and not event.get("stopped"):
                await self.deliver(text, thread_id)
        elif kind == "error":
            self._turn_text.pop(turn, None)
            await self.deliver(event.get("message", ""), thread_id)
        elif kind == "permission_request":
            full = json.dumps(event.get("input") or {}, ensure_ascii=False)     # everything the tool will receive
            if len(full) > PROMPT_LIMIT:         # what is approved must be what is shown: too long, so it is denied
                session = self._engine().sessions.get(event.get("conversation_id"))
                if session is not None:
                    await session.resolve(event["request_id"], "deny", "system")
                await self.deliver(t("telegram.prompt_too_long"))      # permissions and notices go to Avisos
                return
            await self.link.send_exec_approval(full, event["request_id"], event.get("tool", ""))

    # ----- the outbox: nothing is lost if Telegram or Hokma is down -----

    async def deliver(self, text: str, thread_id: int | None = None) -> None:
        """Store the message and try to send it now; whatever fails is retried by `retry_loop`."""
        if self._store_text(text, thread_id):
            await self.flush()

    def enqueue(self, text: str) -> None:
        """Same as `deliver` for code that is not async."""
        if self._store_text(text):
            self._spawn(self.flush())

    def _store_text(self, text: str, thread_id: int | None = None) -> bool:
        if not text.strip():
            return False
        with self._db().write() as u:
            enqueue(u, text, thread_id)
        return True

    async def flush(self) -> int:
        """Send the pending messages in order. A failed one stays queued and does not hold back the rest, so a message
        Telegram rejects for good cannot wedge the queue. Returns how many were sent."""
        sent = 0
        async with self._flushing:                      # one sender at a time: no message goes out twice
            if not self.link.connected or self.link.linked_chat_id() is None:
                return 0
            with self._db().read() as u:
                rows = u.session.query("SELECT id, text, thread_id FROM telegram_outbox WHERE sent_at IS NULL ORDER BY id")
            for row in rows:
                result = await self.link.send_text(row["text"], row["thread_id"])
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
        engine = self._engine()
        with self._db().read() as u:
            cids = [r["conversation_id"] for r in u.session.query("SELECT conversation_id FROM telegram_session")]
        cids += [c for c in self._conns if c not in cids]       # a conversation left by /new may still wait
        cid, session, pending = next(((c, s, s.pending[request_id]) for c in cids
                                      if (s := engine.sessions.get(c)) is not None and request_id in s.pending),
                                     (None, None, None))
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
