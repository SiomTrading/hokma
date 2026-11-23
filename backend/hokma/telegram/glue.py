"""Hokma side of the Telegram link: where the link is stored, how the owner's text reaches the chat engine, how a
button answers a permission and how a finished routine is announced. The Telegram code itself is `core/`."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

from hokma.connections.store import KeyStore
from hokma.core.config import hokma_data_dir
from hokma.core.tls import outbound_context
from hokma.db import Database
from hokma.engine.protocols import DEFAULT_MODEL
from hokma.i18n import t
from hokma.mail import notify as mail_notify
from hokma.telegram.core import TelegramLink
from hokma.telegram.core.link import GROUP_PLATFORM, PLATFORM, _topic_thread_id
from hokma.telegram.core.session_policy import SessionResetPolicy, should_reset
from hokma.telegram.core.telegram_ids import normalize_telegram_chat_id
from hokma.telegram.link_ext import HokmaPairingStore, LateRunUI

log = logging.getLogger("hokma.telegram")
TOKEN_ENTRY = "telegram_bot"            # entry of the bot token in the system credential store
CONVERSATION_TITLE = "Telegram"
TELEGRAM_LIMIT = 4000                  # characters of one message (Telegram allows 4096)
RETRY_SECONDS = 60
RECONNECT_BACKOFF = (5, 15, 45, 120, 300)     # seconds between attempts to bring the bot up, capped at the last one
WATCH_SECONDS = 30                             # how often a bot that should be up is checked
POLICY = SessionResetPolicy(mode="both", idle_minutes=120, at_hour=4)     # local time
PROMPT_LIMIT = 3800                    # characters the approval message can show (core truncates beyond)


def tls12_context() -> ssl.SSLContext:
    # Bug 1114: some networks cut the OpenSSL TLS 1.3 ClientHello towards Telegram; Telegram serves TLS 1.2.
    ctx = outbound_context()
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


API_BASE_ENV = "HOKMA_TELEGRAM_API_BASE"


def api_base_override() -> str | None:
    """Tests only: a local Bot API double (`HOKMA_TELEGRAM_API_BASE=http://127.0.0.1:PORT`). Anything that is not plain
    http to loopback is ignored, so the variable can never send the bot token to another host."""
    raw = os.environ.get(API_BASE_ENV, "").strip().rstrip("/")
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
        ok = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost") and not parsed.path
        parsed.port                                   # a malformed port raises here
    except ValueError:
        ok = False
    if not ok:
        log.warning("%s ignored: only http://127.0.0.1 or http://localhost are accepted", API_BASE_ENV)
        return None
    return raw


async def build_application(token: str):
    """core's Application with every Telegram request (calls and long-poll) on a TLS 1.2 context."""
    from telegram.ext import Application
    from telegram.request import HTTPXRequest
    from hokma.telegram.core.link import discover_fallback_ips, SEED_FALLBACK_IPS, TelegramFallbackTransport, tcp_keepalive_socket_options
    import httpx

    kw = {"connection_pool_size": 512, "pool_timeout": 8.0, "connect_timeout": 10.0,
          "read_timeout": 20.0, "write_timeout": 20.0, "media_write_timeout": 60.0}
    local = api_base_override()
    if local:                                         # a loopback double: plain requests, no fallback IPs, no TLS
        return (Application.builder().token(token).base_url(f"{local}/bot").base_file_url(f"{local}/file/bot")
                .request(HTTPXRequest(**kw)).get_updates_request(HTTPXRequest(**{**kw, "connection_pool_size": 8})).build())
    try:
        ips = await asyncio.wait_for(discover_fallback_ips(), timeout=5.0)
    except Exception:
        ips = list(SEED_FALLBACK_IPS)

    def request(keepalive: int) -> HTTPXRequest:
        limits = httpx.Limits(max_connections=512, max_keepalive_connections=keepalive, keepalive_expiry=5.0)
        transport = TelegramFallbackTransport(
            ips, socket_options=tcp_keepalive_socket_options(), limits=limits, verify=tls12_context())
        return HTTPXRequest(**kw, httpx_kwargs={"transport": transport})

    # A long-poll is continuously active: never hand getUpdates a pooled socket from a previous poll.
    return Application.builder().token(token).request(request(20)).get_updates_request(request(0)).build()


_PERMISSION_HINTS = ("rights", "administrator", "forbidden", "not a forum", "forums_disabled", "chat_admin_required")


def classify_failure(exc: BaseException) -> str:
    """`network` (Telegram not reached), `permission` (Telegram refused for lack of rights), `refused` (Telegram answered
    with another refusal: chat not found, bad request), `migrated` (the group became a supergroup) or `other`.
    Bug 1170: python-telegram-bot makes BadRequest a NetworkError, so the class of the API answer is read first."""
    import httpx
    from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, TimedOut

    if isinstance(exc, ChatMigrated):
        return "migrated"
    text = str(exc).lower()
    if isinstance(exc, Forbidden) or (isinstance(exc, BadRequest) and any(h in text for h in _PERMISSION_HINTS)):
        return "permission"
    if isinstance(exc, BadRequest):
        return "refused"
    if isinstance(exc, (NetworkError, TimedOut, httpx.HTTPError, OSError, asyncio.TimeoutError)):
        return "network"
    if any(h in text for h in _PERMISSION_HINTS):
        return "permission"
    return "other"


_TELEGRAM_CODES = {"BadRequest": 400, "ChatMigrated": 400, "Forbidden": 403, "Conflict": 409, "RetryAfter": 429,
                   "InvalidToken": 401}


def describe_failure(exc: BaseException) -> dict:
    """What the screen and engine.log say about a failed topic call: the Telegram code, its text and a translation."""
    from hokma.telegram.core.approvals_ui import _redact_telegram_error_text

    kind = classify_failure(exc)
    original = _redact_telegram_error_text(str(exc))[:300]
    text = original.lower()
    detail = original or type(exc).__name__
    if kind == "permission":
        message = t("telegram.topic.err.no_rights", detail=detail)
    elif kind == "network":
        message = t("telegram.topic.err.network", detail=detail)
    elif "chat not found" in text or "chat_id is empty" in text:
        message = t("telegram.topic.err.chat_not_found", detail=detail)
    elif kind == "migrated":
        message = t("telegram.topic.err.migrated", detail=detail)
    else:
        message = t("telegram.topic.err.other", detail=detail)
    name = type(exc).__name__
    return {"kind": kind, "code": _TELEGRAM_CODES.get(name), "error": name, "telegram": original,
            "message": message}


LABEL_LIMIT = 60                       # characters of the name that `/new Name` adds to the conversation title


def reset_label(text: str | None) -> str:
    """The name after `/new` or `/reset` (`/new@bot Nasdaq` too): one line, no control characters, at most 60
    characters. Empty when the command came without one."""
    parts = (text or "").strip().split(None, 1)
    if len(parts) < 2:
        return ""
    clean = "".join(" " if (c.isspace() or not c.isprintable()) else c for c in parts[1])
    return " ".join(clean.split())[:LABEL_LIMIT].strip()


class HokmaLink(TelegramLink):
    """`core/` is a verbatim copy of a shared module (see MANIFEST.sha256): what Hokma needs on top is added here.
    Bug 1126: `connected` is the bot being up, not just an Application object. Bug 1127: the Avisos state tells a
    network failure from a missing permission and can be checked again."""

    topic_failure: str | None = None

    def __init__(self, data_dir, *args, **kw) -> None:
        super().__init__(data_dir, *args, **kw)
        self._pairing = HokmaPairingStore(data_dir)        # knows its lockout and records its rate limit late
        self._lifecycle = asyncio.Lock()                   # bug 1152: one start or stop at a time
        self._lifecycle_owner: asyncio.Task | None = None
        self._started_with: str | None = None

    # ----- bug 1152: connecting is serialized -----

    async def start(self, token: str) -> None:
        async with self._lifecycle:
            if self._app is not None and self._started_with == token and self.connected:
                return                                     # a second Connect finds the bot already up: no second poller
            self._lifecycle_owner = asyncio.current_task()
            try:
                await super().start(token)                 # (core calls self.stop() inside: see `stop`)
                self._started_with = token
            finally:
                self._lifecycle_owner = None

    async def stop(self) -> None:
        if self._lifecycle_owner is not None and self._lifecycle_owner is asyncio.current_task():
            self._started_with = None
            return await super().stop()                    # called by `start` itself, which holds the lock
        async with self._lifecycle:
            self._started_with = None
            await super().stop()

    # ----- the handlers Hokma adds or answers more clearly -----

    def _register_handlers(self, app) -> None:
        from telegram.ext import MessageHandler, filters

        self._ui = order_ticket_ui()(                # before core registers its callback handler
            self._bot, is_authorized=self._is_authorized, on_decision=self._on_decision, t=self._t,
            on_rejected=self._note_ignored, is_sender_ok=self._sender_ok)
        self._ui.on_late = self.on_late
        super()._register_handlers(app)
        # Bug 1157: voice, photos, documents... were dropped without a word. Added last, so text and commands
        # still reach core's handlers first.
        app.add_handler(MessageHandler(filters.ALL & ~filters.TEXT & ~filters.StatusUpdate.ALL, self._handle_unsupported))

    async def _reply(self, chat_id: Any, thread_id: int | None, text: str) -> None:
        if self._bot is None:
            return
        kwargs = {"message_thread_id": thread_id} if thread_id else {}
        try:
            await self._bot.send_message(chat_id=normalize_telegram_chat_id(chat_id), text=text, **kwargs)
        except Exception as exc:                           # a courtesy reply: never in the way
            log.warning("telegram reply failed: %s", type(exc).__name__)

    async def _handle_unsupported(self, update: Any, context: Any = None) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        chat = getattr(update, "effective_chat", None) or getattr(message, "chat", None)
        user = getattr(update, "effective_user", None)
        if message is None or chat is None:
            return
        if not self._sender_ok(str(chat.id), str(getattr(user, "id", ""))):
            self._note_ignored(chat.id)
            return
        await self._reply(chat.id, _topic_thread_id(message), self._t("telegram.unsupported"))

    def _avisos_thread(self) -> int | None:
        return (self._store.load() or {}).get("avisos_thread_id")

    async def _handle_group_text(self, update: Any) -> None:
        context = self._group_context(update)
        if context is None:
            return
        _, chat, thread_id = context
        if thread_id is None:
            return await self._reply(chat.id, None, self._t("telegram.in_general"))
        if thread_id == self._avisos_thread():
            return await self._reply(chat.id, thread_id, self._t("telegram.in_avisos"))
        await super()._handle_group_text(update)

    async def _handle_reset(self, update: Any, context: Any = None) -> None:
        group = self._group_context(update)
        if group is None:
            return
        _, chat, thread_id = group
        if thread_id is None or thread_id == self._avisos_thread():
            return await self._reply(chat.id, thread_id, self._t("telegram.reset_not_in_topic"))
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        label = reset_label(getattr(message, "text", None))
        if self._on_reset is None:
            return
        result = self._on_reset(thread_id, label) if label else self._on_reset(thread_id)
        if hasattr(result, "__await__"):
            title = await result
        else:
            title = result
        text = self._t("telegram.session_reset_named", title=title) if label and title else self._t("session_reset")
        await self._bot.send_message(chat_id=normalize_telegram_chat_id(chat.id), text=text, message_thread_id=thread_id)
        if label and self.mode_text is not None:
            await self._bot.send_message(chat_id=normalize_telegram_chat_id(chat.id), text=self.mode_text(),
                                         message_thread_id=thread_id)

    # ----- bug 1154: a code is never lost silently and the user is told why none comes -----

    @staticmethod
    def _minutes(seconds: int) -> int:
        return max(1, -(-int(seconds) // 60))

    def pairing_lockout_minutes(self, platform: str = PLATFORM) -> int:
        """Minutes until codes are accepted again, 0 when they are accepted now."""
        left = self._pairing.lockout_seconds_left(platform)
        return self._minutes(left) if left else 0

    async def _issue_code(self, platform: str, user_id: str, user_name: str, chat: Any, thread_id: int | None,
                          message_key: str) -> None:
        store = self._pairing
        locked = store.lockout_seconds_left(platform)
        if locked:
            return await self._reply(chat.id, thread_id, self._t("telegram.err.locked", minutes=self._minutes(locked)))
        code = store.generate_code_deferred(platform, user_id, user_name)
        if code is None:
            wait = store.rate_limit_seconds_left(platform, user_id)
            text = (self._t("telegram.pair_already_sent", minutes=self._minutes(wait)) if wait
                    else self._t("telegram.pair_pending"))
            return await self._reply(chat.id, thread_id, text)
        kwargs = {"message_thread_id": thread_id} if thread_id else {}
        try:
            await self._bot.send_message(chat_id=normalize_telegram_chat_id(chat.id),
                                         text=self._t(message_key, code=code), **kwargs)
        except Exception as exc:
            store.discard_code(platform, code)             # it never arrived: it must not count nor take a place
            log.warning("telegram could not deliver a pairing code: %s", type(exc).__name__)
            return
        store.commit_rate_limit(platform, user_id)

    async def _offer_pairing_code(self, message: Any, chat: Any, update: Any) -> None:
        user = getattr(update, "effective_user", None)
        user_name = str(getattr(user, "first_name", "") or getattr(chat, "title", "") or "")
        await self._issue_code(PLATFORM, str(chat.id), user_name, chat, None, "pairing_code_message")

    async def _handle_vincular(self, update: Any, context: Any = None) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        chat = getattr(update, "effective_chat", None) or getattr(message, "chat", None)
        user = getattr(update, "effective_user", None)
        if message is None or chat is None:
            return
        if str(getattr(user, "id", "")) != self.linked_chat_id():
            self._note_ignored(chat.id)
            return
        thread_id = _topic_thread_id(message)
        if self.linked_group_id() is not None:
            return await self._reply(chat.id, thread_id, self._t("telegram.group_already_linked_bot"))
        await self._issue_code(GROUP_PLATFORM, str(chat.id), str(getattr(chat, "title", "") or ""), chat, thread_id,
                               "group_pairing_code_message")

    # ----- bug 1155: a permission goes to the chat or topic where it is being talked about -----

    on_late: Callable | None = None          # async (job_id, choice) -> text; the glue sets it

    async def send_late_prompt(self, job: Any) -> bool:
        """Ask by Telegram whether to run now a routine that fell due too long ago. False when it could not be sent."""
        chat_id, destination = self._destination(None)
        if self._ui is None or chat_id is None:
            return False
        text = self._t("telegram.late_ask", name=job.name, planned=job.next_run_at)
        result = await self._ui.send_late_prompt(chat_id, destination, text, job.id,
                                                 self._t("telegram.late_btn_run"), self._t("telegram.late_btn_skip"))
        return bool(result.success)

    async def send_order_ticket(self, proposal_id: int, fingerprint: str, text: str) -> bool:
        """The ticket of an order that waits for the owner, with its Approve and Reject buttons. False when it could not be sent."""
        chat_id, destination = self._destination(None)
        if self._ui is None or chat_id is None or not hasattr(self._ui, "send_order_ticket"):
            return False
        result = await self._ui.send_order_ticket(chat_id, destination, proposal_id, fingerprint, text,
                                                  (self._t("telegram.order_btn_approve"), self._t("telegram.order_btn_reject")))
        return bool(result.success)

    async def send_exec_approval(self, command: str, request_id: str, description: str = "", *,
                                 thread_id: int | None = None, origin: str | None = None, **kwargs: Any):
        from hokma.telegram.core.approvals_ui import SendResult

        chat_id, destination = self._destination(thread_id)
        if self._ui is None or chat_id is None:
            return SendResult(success=False, error="Not connected")
        return await self._ui.send_exec_approval(chat_id, command, request_id, description, thread_id=destination,
                                                 origin=origin)

    @property
    def connected(self) -> bool:
        """Up and its long-poll alive. PTB retries a dropped poll by itself, so a short blip still counts as up; a poll
        that ended for good (revoked token) does not. The watcher (`keep_connected`) brings a fallen bot back."""
        app = self._app
        if app is None:
            return False
        updater = getattr(app, "updater", None)
        return bool(getattr(updater, "running", True)) if updater is not None else True

    topic_error: dict | None = None
    mode_text = None                       # () -> str: the permission mode in words (bug 1171), set by the glue

    async def create_topic(self, chat_id, name, icon_color=None, icon_custom_emoji_id=None):
        self.topic_failure = None
        self.topic_error = None
        if not self._bot:
            self.topic_failure = "network"
            self.topic_error = {"kind": "network", "code": None, "error": "NoBot", "telegram": "",
                                "message": t("telegram.topic.err.network", detail="")}
            return None
        try:
            topic = await self._bot.create_forum_topic(chat_id=chat_id, name=name)
            return topic.message_thread_id
        except Exception as exc:
            from telegram.error import ChatMigrated

            if isinstance(exc, ChatMigrated) and getattr(exc, "new_chat_id", None):
                return await self._create_topic_after_migration(chat_id, name, exc)
            self._note_topic_failure(exc, name)
            return None

    def _note_topic_failure(self, exc: BaseException, name) -> None:
        info = describe_failure(exc)
        self.topic_failure, self.topic_error = info["kind"], info
        log.warning("could not create topic %r: %s (%s)", name, info["error"], info["kind"])
        from hokma.engine import errlog
        errlog.log(f"telegram avisos topic failed: {info['error']} code={info['code']} kind={info['kind']} "
                   f"telegram={info['telegram']!r}", scope="telegram")

    async def _create_topic_after_migration(self, old_id, name, exc):
        """Bug 1170: the group became a supergroup after it was linked. Telegram says the new id: keep it and retry once."""
        new_id = str(exc.new_chat_id)
        try:
            topic = await self._bot.create_forum_topic(chat_id=normalize_telegram_chat_id(new_id), name=name)
        except Exception as again:
            self._note_topic_failure(again, name)
            return None
        self._save({"group_id": new_id})
        return topic.message_thread_id

    async def ensure_avisos_topic(self, revalidate: bool = False) -> bool:
        """Creates the Avisos topic in the linked group and records `avisos`: `ok`, `no_permission` (Telegram refused
        for lack of rights) or `unreachable` (network or unknown error: not proof of a missing permission).
        `revalidate`: a stored topic is probed first, so a revoked right or a deleted topic shows up."""
        from hokma.telegram.core.link import _message_thread_id_for_send
        from hokma.telegram.core.telegram_ids import normalize_telegram_chat_id

        group_id = self.linked_group_id()
        if group_id is None or self._bot is None:
            return False
        record = self._store.load() or {}
        thread_id = record.get("avisos_thread_id") if record.get("avisos_group_id") == group_id else None
        if thread_id and revalidate:
            try:
                await self._bot.send_chat_action(chat_id=normalize_telegram_chat_id(group_id), action="typing",
                                                 message_thread_id=_message_thread_id_for_send(thread_id))
            except Exception as exc:
                if classify_failure(exc) == "network":
                    self.topic_error = describe_failure(exc)
                    self._save({"avisos": "unreachable"})
                    return False
                thread_id = None                      # gone or refused: create it again below
        if thread_id:
            self.topic_failure = None
        else:
            thread_id = await self.create_topic(normalize_telegram_chat_id(group_id), self._t("avisos_topic_name"))
        state = "ok" if thread_id else {"permission": "no_permission", "network": "unreachable"}.get(self.topic_failure or "", "failed")
        if state == "ok":
            self.topic_error = None
        group_id = self.linked_group_id() or group_id           # a migration may have changed it
        self._save({"avisos_thread_id": thread_id, "avisos_group_id": group_id if thread_id else None, "avisos": state})
        if thread_id:
            self._topics[thread_id] = self._t("avisos_topic_name")
        return thread_id is not None


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
            if old is not None and old["chat_id"] != chat:       # the queue and the sessions were the previous chat's
                u.session.execute("DELETE FROM telegram_outbox WHERE sent_at IS NULL")
                u.session.execute("DELETE FROM telegram_session WHERE chat_id != :c", {"c": chat})
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


def order_ticket_ui() -> type:
    """The approvals interface with the `late run` buttons and the order ticket buttons of SPEC-018 (`hokma.exec.telegram_approval`)."""
    from hokma.exec.telegram_approval import make_ui_class

    return make_ui_class(LateRunUI)


def order_notice(unit, kind: str, text: str) -> int:
    """A notice about an order (a fill, a rejection, an expiry) for the owner, through the outbox: the sender retries it until Telegram takes
    it, and one that fails does not hold back the next ones (`flush` goes on with the rest). `kind` is only for the log."""
    log.info("order notice queued: %s", kind)
    return enqueue(unit, text)


class TelegramGlue:
    def __init__(self, get_db: Callable[[], Database], get_engine: Callable[[], Any], *,
                 keys: KeyStore | None = None, application_factory=build_application, data_dir=None, now=datetime.now) -> None:
        from hokma.engine.engine import Connection

        self._db, self._engine = get_db, get_engine
        self.keys = keys or KeyStore()
        self.store = LinkStore(get_db)
        self.link = HokmaLink(data_dir or hokma_data_dir() / "telegram", self.store, t=t,
                                 on_owner_text=self.on_owner_text, on_decision=self.on_decision, on_reset=self.on_reset,
                                 application_factory=application_factory)
        self.link.mode_text = self.mode_notice
        self.link.on_late = self.on_late_choice
        self._announce: set[str] = set()                # conversations born here that still owe the mode notice
        self._connection, self._conns = Connection, {}          # one per conversation, so its answers know their topic
        self._now = now
        self._turn_text: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._flushing = asyncio.Lock()
        self._starting = asyncio.Lock()
        self._queued: dict[str, list[str]] = {}       # bug 1156: text that arrived while a turn was running
        self.connect_error: str | None = None       # why the last attempt to connect failed (no token in it)

    # ----- lifecycle -----

    async def start(self) -> bool:
        """Connect when a token is saved. Without a token nothing happens."""
        async with self._starting:                  # the API, the startup and the watcher never connect at once
            token = self.keys.get(TOKEN_ENTRY)      # read inside: a token saved while we waited is the one used
            if not token:
                return False
            await self.link.start(token)
            from hokma.exec import telegram_approval

            telegram_approval.set_button_sender(self.link.send_order_ticket)     # the tickets of the orders go out with their buttons
        self.connect_error = None
        await self._revalidate_avisos()
        return True

    async def _revalidate_avisos(self) -> None:
        """Bug 1127: `avisos` is checked again every time the bot comes up, not trusted from the day it was written."""
        if self.link.linked_group_id() is None:
            return
        try:
            await self.link.ensure_avisos_topic(revalidate=True)
        except Exception as exc:                      # the bot is up either way
            log.warning("telegram avisos check failed: %s", type(exc).__name__)

    async def keep_connected(self, backoff: tuple = RECONNECT_BACKOFF, watch: float = WATCH_SECONDS) -> None:
        """Bring the bot up in the background and bring it back when it falls (no network at start, Telegram blocked,
        a poll that died). Tries again with a growing pause; stops trying only when the token is gone (unlinked)."""
        attempt = 0
        while True:
            delay = watch
            try:
                if self.keys.get(TOKEN_ENTRY) and not self.link.connected:
                    await self.start()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:                  # no traceback: a network error may carry the bot token
                from hokma.telegram.core.approvals_ui import _redact_telegram_error_text
                self.connect_error = _redact_telegram_error_text(f"{type(exc).__name__}: {exc}")[:300]
                log.warning("telegram did not connect: %s", self.connect_error)
                delay = backoff[min(attempt, len(backoff) - 1)]
                attempt += 1
            await asyncio.sleep(delay)

    def permission_mode(self) -> str:
        """Bug 1171: the mode of Settings > Permissions, the one every Telegram conversation runs with."""
        return self._engine().permission_mode()

    def mode_notice(self) -> str:
        return t("telegram.mode_auto") if self.permission_mode() == "auto" else t("telegram.mode_ask")

    def outbox_state(self) -> dict:
        """What waits for Telegram: how many messages and the last error Telegram gave for one of them."""
        with self._db().read() as u:
            pending = u.session.one("SELECT COUNT(*) AS n FROM telegram_outbox WHERE sent_at IS NULL")["n"]
            failed = u.session.one("SELECT last_error FROM telegram_outbox WHERE sent_at IS NULL AND last_error IS NOT NULL "
                                   "ORDER BY id DESC LIMIT 1")
        return {"pending": int(pending), "last_error": (failed or {}).get("last_error")}

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
            self._announce.add(cid)
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
        if cid in self._announce:                        # bug 1171: a new conversation says which mode it runs in
            self._announce.discard(cid)
            await self.deliver(self.mode_notice(), thread)
        self._touch(cid)
        running = self._engine().sessions.get(cid)
        if getattr(running, "state", None) == "running":     # bug 1156: not lost, not refused: sent when the turn ends
            self._queued.setdefault(cid, []).append(text)
            await self.deliver(t("telegram.queued"), thread)
            return
        await self._engine().handle(self._conns[cid], json.dumps({"type": "send", "conversation_id": cid, "text": text}))

    def on_reset(self, thread_id: int, label: str = "") -> str | None:
        """`/new` in a topic: forget its session; the next message opens another and the old conversation stays.
        `/new Name`: the new conversation opens now, titled "<topic> · Name"; returns that title."""
        chat = str(self.link.linked_chat_id())
        key = {"c": chat, "t": thread_id}
        with self._db().read() as u:
            row = u.session.one("SELECT topic_name FROM telegram_session WHERE chat_id = :c AND thread_id = :t", key)
        with self._db().write() as u:
            u.session.execute("DELETE FROM telegram_session WHERE chat_id = :c AND thread_id = :t", key)
        if not label:
            return None
        topic = (row or {}).get("topic_name") or self.link._topics.get(thread_id) or CONVERSATION_TITLE
        title = f"{topic} \u00b7 {label}"
        cid = self._engine().store.create_conversation(title, DEFAULT_MODEL, None, None).id
        with self._db().write() as u:
            u.session.insert_row("telegram_session", {"chat_id": chat, "thread_id": thread_id, "conversation_id": cid,
                                                      "topic_name": topic, "updated_at": int(self._now().timestamp())})
        return title

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
            self._send_queued(event.get("conversation_id"))
        elif kind == "error":
            self._turn_text.pop(turn, None)
            await self.deliver(self._error_text(event.get("code")), thread_id)     # bug 1156: never the raw message
            if turn:                                                  # an error that ended a turn, not a refused send
                self._send_queued(event.get("conversation_id"))
        elif kind == "permission_request":
            full = json.dumps(event.get("input") or {}, ensure_ascii=False)     # everything the tool will receive
            if len(full) > PROMPT_LIMIT:         # what is approved must be what is shown: too long, so it is denied
                session = self._engine().sessions.get(event.get("conversation_id"))
                if session is not None:
                    await session.resolve(event["request_id"], "deny", "system")
                await self.deliver(t("telegram.prompt_too_long"))      # notices go to Avisos
                return
            origin = self._origin_label(thread_id) if self.link.linked_group_id() is not None else None
            result = await self.link.send_exec_approval(full, event["request_id"], event.get("tool", ""),
                                                        thread_id=thread_id, origin=origin)
            if not result.success:                # bug 1127: a permission nobody could see must not wait in silence
                await self.deliver(t("telegram.permission_unsent", tool=event.get("tool", "")))

    @staticmethod
    def _error_text(code: object) -> str:
        """What the phone shows for an engine error: our own text for its code, never the engine's English message."""
        code = code if isinstance(code, str) and re.fullmatch(r"[a-z_0-9]{1,40}", code) else "engine_error"
        if code == "busy":
            return t("telegram.err.busy")
        if code == "unknown_project":
            return t("telegram.err.unknown_project")
        return t("telegram.err.engine", code=code)

    def _origin_label(self, thread_id: int) -> str:
        """Where a permission comes from: the topic it is being talked about in, or the private chat."""
        if not thread_id:
            return t("telegram.origin_private")
        with self._db().read() as u:
            row = u.session.one("SELECT topic_name FROM telegram_session WHERE chat_id = :c AND thread_id = :t",
                                {"c": str(self.link.linked_chat_id()), "t": thread_id})
        return t("telegram.origin_topic", name=(row or {}).get("topic_name") or self.link._topics.get(thread_id) or thread_id)

    def _send_queued(self, cid: object) -> None:
        """The turn ended: what the owner wrote meanwhile goes to the engine now, as one message."""
        texts = self._queued.pop(cid, []) if isinstance(cid, str) else []
        if texts and cid in self._conns:
            self._spawn(self._engine().handle(self._conns[cid], json.dumps(
                {"type": "send", "conversation_id": cid, "text": "\n\n".join(texts)})))

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
                rows = u.session.query("SELECT id, text, thread_id, attempts FROM telegram_outbox WHERE sent_at IS NULL ORDER BY id")
            for row in rows:
                result = await self.link.send_text(row["text"], row["thread_id"])
                if not result.success and not row["attempts"]:      # first failure only: a retry never mails it again
                    await asyncio.to_thread(mail_notify.fallback, row["text"])
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
        session = next((s for c in cids if (s := engine.sessions.get(c)) is not None and request_id in s.pending), None)
        if session is None:
            return 0
        # The session records the answer (once, with this channel): bug 1153 was a second row written here.
        resolved = await session.resolve(request_id, "deny" if choice == "deny" else "allow", "human", channel="telegram")
        return 1 if resolved else 0           # 0: it ended meanwhile

    # ----- a routine ended -----

    def notify_job(self, job: Any, outcome: Any) -> None:
        """Scheduler hook: tell the phone when a routine finishes, fails or is skipped (bug 1136)."""
        if outcome.status not in ("done", "failed", "skipped"):
            return
        if outcome.status == "skipped" and outcome.detail == "asked":
            self._spawn(self._ask_late(job))
            return
        if outcome.status == "skipped":
            code = str(outcome.detail or "")
            reasons = {"missed": t("sched.reason.missed"), "agent_off": t("sched.reason.agent_off")}
            detail = reasons.get(code, code[:300])
            text = t("telegram.job_skipped", name=job.name, detail=detail)
        elif outcome.status == "done" and getattr(outcome, "denied", 0):     # bug 1129: part of it was refused
            text = t("telegram.job_done_denied", name=job.name, count=outcome.denied) + "\n\n" + (outcome.detail or "")
        elif outcome.status == "done":
            text = t("telegram.job_done", name=job.name) + "\n\n" + (outcome.detail or "")
        else:
            text = t("telegram.job_failed", name=job.name, detail=(outcome.detail or "")[:300])
        planned = getattr(outcome, "late_planned", None)
        if planned and outcome.status in ("done", "failed"):         # it ran at startup instead of at its hour
            text = t("telegram.job_late", name=job.name, planned=planned) + "\n\n" + text
        if getattr(job, "id", None) in mail_notify.load()["jobs"]:         # C7-2c: the routines the owner also wants in the mailbox
            self._spawn(asyncio.to_thread(mail_notify.for_job, job.id, text))
        if self.store.load() is not None:
            self.enqueue(text)

    async def _ask_late(self, job: Any) -> None:
        """Too old to run on its own: ask with two buttons. If Telegram cannot take the question, say it was skipped."""
        if not await self.link.send_late_prompt(job):
            self.enqueue(t("telegram.job_skipped", name=job.name, detail=t("sched.reason.missed")))

    async def on_late_choice(self, job_id: int, choice: str) -> str:
        """The owner tapped "Run now" or "Skip": do it and leave the answer in the journal. Returns the text shown."""
        from hokma.agents.api import current_scheduler
        from hokma.db import NotFound
        from hokma.scheduler import runner

        with self._db().read() as u:
            try:
                job = u.scheduled_jobs.get(job_id)
            except NotFound:
                return t("telegram.late_gone")
        scheduler = current_scheduler()
        allowed = job.approval_id is not None and (job.is_active or job.schedule_kind == "once")   # a `once` turns off when skipped
        ran = choice == "run" and allowed and scheduler is not None and scheduler.run_now(job)
        if ran:
            reason, text = "ran", t("telegram.late_ran", name=job.name)
        elif choice != "run":
            reason, text = "skip", t("telegram.late_omitted", name=job.name)
        elif allowed and scheduler is not None:
            reason, text = "busy", t("telegram.late_busy", name=job.name)         # run_now said no: already running
        else:
            reason, text = "refused", t("telegram.late_refused", name=job.name)   # off, not approved, or no scheduler
        with self._db().write() as u:
            u.journal.add("note", t("telegram.late_journal", name=job.name), actor=runner.SYSTEM, outcome="success",
                          agent_slug=job.agent_slug, body=text,
                          refs={"scheduled_job": job.id, "late_answer": reason})
        return text
