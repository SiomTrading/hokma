"""Telegram link: long-poll loop, pairing with one owner chat, connect and disconnect.

Ports injected by the host application:
  store            object with ``load() -> dict | None``, ``save(dict)``, ``clear()`` (the single link record)
  is_authorized    ``(chat_id) -> bool``; defaults to "the chat saved in the store"
  on_owner_text    ``(text) -> str | None | awaitable``; the reply, if any, goes back to the chat. A message
                   written in the linked group is called ``(text, thread_id, topic_name)`` and its reply goes
                   to that topic; ``topic_name`` is ``None`` when the bot has not seen the topic being named
  on_reset         ``(thread_id) -> None | awaitable``; ``/new`` or ``/reset`` in a topic of the linked group
  on_decision      ``(request_id, choice) -> int | awaitable``; how many pending requests were resolved
  t                ``(key, **kw) -> str``; i18n
  on_ignored       ``(chat_id) -> None``; called for every message or tap from a chat that is not the linked one
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx

from .approvals_ui import ApprovalsUI, SendResult, _redact_telegram_error_text
from .bot_token import is_valid_telegram_bot_token
from .pairing import PairingStore
from .telegram_ids import normalize_telegram_chat_id
from .telegram_network import (
    SEED_FALLBACK_IPS, TelegramFallbackTransport, discover_fallback_ips, tcp_keepalive_socket_options)

logger = logging.getLogger(__name__)

PLATFORM = "telegram"
GROUP_PLATFORM = "telegram_group"
_GENERAL_TOPIC_THREAD_ID = "1"
_UPDATER_START_TIMEOUT = 30.0
_UPDATER_STOP_TIMEOUT = 10.0


class InvalidBotToken(ValueError):
    """The token does not have Telegram's bot-token shape."""


class AlreadyLinked(RuntimeError):
    """A chat is already linked; unlink it first."""


class NotLinked(RuntimeError):
    """The private chat must be linked before a group."""


def _message_thread_id_for_send(thread_id: Optional[str]) -> Optional[int]:
    if not thread_id or str(thread_id) == _GENERAL_TOPIC_THREAD_ID:
        return None
    return int(thread_id)


def _topic_thread_id(message: Any) -> Optional[int]:
    """Topic id of an incoming message, ``None`` for the General topic and for plain replies."""
    thread_id_raw = getattr(message, "message_thread_id", None)
    chat = getattr(message, "chat", None)
    is_topic_message = bool(getattr(message, "is_topic_message", False))
    if thread_id_raw is not None and (is_topic_message or getattr(chat, "is_forum", False) is True):
        return _message_thread_id_for_send(str(thread_id_raw))
    return None


async def build_application(token: str):
    """Build the python-telegram-bot Application with the fallback-IP transport."""
    from telegram.ext import Application
    from telegram.request import HTTPXRequest

    request_kwargs = {
        "connection_pool_size": 512, "pool_timeout": 8.0, "connect_timeout": 10.0,
        "read_timeout": 20.0, "write_timeout": 20.0, "media_write_timeout": 60.0}
    pool_limits = httpx.Limits(max_connections=512, max_keepalive_connections=20, keepalive_expiry=5.0)
    # A long-poll is continuously active: never hand getUpdates a pooled socket from a previous poll.
    updates_limits = httpx.Limits(max_connections=512, max_keepalive_connections=0, keepalive_expiry=5.0)
    try:
        fallback_ips = await asyncio.wait_for(discover_fallback_ips(), timeout=5.0)
    except Exception as exc:
        logger.warning("Telegram fallback-IP discovery failed, using seed IPs: %s", _redact_telegram_error_text(exc))
        fallback_ips = list(SEED_FALLBACK_IPS)
    transport_kwargs: dict = {"socket_options": tcp_keepalive_socket_options(), "limits": pool_limits}
    updates_transport_kwargs = dict(transport_kwargs)
    updates_transport_kwargs["limits"] = updates_limits
    request = HTTPXRequest(**request_kwargs, httpx_kwargs={
        "transport": TelegramFallbackTransport(fallback_ips, **transport_kwargs)})
    get_updates_request = HTTPXRequest(**request_kwargs, httpx_kwargs={
        "transport": TelegramFallbackTransport(fallback_ips, **updates_transport_kwargs)})
    builder = Application.builder().token(token).request(request).get_updates_request(get_updates_request)
    return builder.build()


class TelegramLink:
    """One bot, one owner chat. ``start`` connects; ``stop`` disconnects."""

    def __init__(
        self, data_dir, store: Any, *, t: Callable[..., str],
        on_owner_text: Callable[[str], "Optional[str] | Awaitable[Optional[str]]"],
        on_decision: Callable[[str, str], "int | Awaitable[int]"],
        is_authorized: Optional[Callable[[str], bool]] = None,
        on_ignored: Optional[Callable[[str], None]] = None,
        on_reset: Optional[Callable[[int], "Optional[Awaitable[None]]"]] = None,
        application_factory: Callable[[str], Awaitable[Any]] = build_application) -> None:
        self._pairing = PairingStore(data_dir)
        self._store = store
        self._t = t
        self._on_owner_text = on_owner_text
        self._on_decision = on_decision
        self._is_authorized = is_authorized or self._default_is_authorized
        self._on_ignored = on_ignored
        self._on_reset = on_reset
        self._topics: dict[int, str] = {}  # thread_id -> name; Telegram cannot list topics
        self._application_factory = application_factory
        self._app: Any = None
        self._bot: Any = None
        self._ui: Optional[ApprovalsUI] = None

    # ----- state -----

    @property
    def connected(self) -> bool:
        return self._app is not None

    def linked_chat_id(self) -> Optional[str]:
        record = self._store.load()
        chat_id = (record or {}).get("chat_id")
        return str(chat_id) if chat_id not in (None, "") else None

    def linked_group_id(self) -> Optional[str]:
        group_id = (self._store.load() or {}).get("group_id")
        return str(group_id) if group_id not in (None, "") else None

    def _save(self, fields: dict) -> None:
        self._store.save({**(self._store.load() or {}), **fields})

    def _sender_ok(self, chat_id: str, user_id: str) -> bool:
        """The linked user, writing in their private chat or in the linked group. Nobody else."""
        linked = self.linked_chat_id()
        if linked is None or not self._is_authorized(linked):
            return False
        if user_id == chat_id:
            return chat_id == linked
        return chat_id == self.linked_group_id() and user_id == linked

    def _default_is_authorized(self, chat_id: str) -> bool:
        linked = self.linked_chat_id()
        return linked is not None and linked == str(chat_id)

    def _note_ignored(self, chat_id: Any) -> None:
        if self._on_ignored is not None:
            with contextlib.suppress(Exception):
                self._on_ignored(str(chat_id))

    # ----- connect / disconnect -----

    async def start(self, token: str) -> None:
        """Connect with ``token`` and start the getUpdates loop."""
        if not is_valid_telegram_bot_token(token):
            raise InvalidBotToken("invalid bot token")
        if self._app is not None:
            await self.stop()
        app = await self._application_factory(token)
        self._app = app
        self._bot = app.bot
        self._ui = ApprovalsUI(
            self._bot, is_authorized=self._is_authorized, on_decision=self._on_decision, t=self._t,
            on_rejected=self._note_ignored, is_sender_ok=self._sender_ok)
        self._register_handlers(app)
        try:
            await app.initialize()
            await app.start()
            await asyncio.wait_for(
                app.updater.start_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False),
                timeout=_UPDATER_START_TIMEOUT)
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Stop polling and release the connection."""
        app, self._app, self._bot = self._app, None, None
        if self._ui is not None:
            self._ui.set_bot(None)
        if app is None:
            return
        try:
            if app.updater and app.updater.running:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(app.updater.stop(), timeout=_UPDATER_STOP_TIMEOUT)
            if app.running:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(app.stop(), timeout=_UPDATER_STOP_TIMEOUT)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(app.shutdown(), timeout=_UPDATER_STOP_TIMEOUT)
        except Exception as e:
            logger.warning("Error during Telegram disconnect: %s", _redact_telegram_error_text(e))

    def _register_handlers(self, app) -> None:
        from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters
        app.add_handler(CommandHandler("vincular", self._handle_vincular, filters=filters.ChatType.GROUPS))
        app.add_handler(CommandHandler(["new", "reset"], self._handle_reset, filters=filters.ChatType.GROUPS))
        app.add_handler(MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CREATED | filters.StatusUpdate.FORUM_TOPIC_EDITED, self._handle_topic_event))
        app.add_handler(MessageHandler(filters.TEXT, self._handle_text_message))
        app.add_handler(CallbackQueryHandler(self._ui.handle_callback_query))

    # ----- pairing -----

    def approve_pairing_code(self, code: str) -> Optional[dict]:
        """Approve a pairing code shown by the bot. Links the chat that asked for it.

        Returns ``{user_id, user_name}`` on success, ``None`` for a wrong, expired or locked-out code.
        Raises ``AlreadyLinked`` when a chat is already linked.
        """
        if self.linked_chat_id() is not None:
            raise AlreadyLinked("a chat is already linked")
        result = self._pairing.approve_code(PLATFORM, code)
        if result is None:
            return None
        self._store.save({
            "chat_id": str(result["user_id"]), "user_name": result.get("user_name", ""), "linked_at": time.time()})
        return result

    async def approve_group_code(self, code: str) -> Optional[dict]:
        """Approve the code the bot showed in a group (``/vincular``). Links that group and creates its Avisos topic.

        Returns ``{user_id, user_name}`` (``user_id`` is the group id) on success, ``None`` for a wrong, expired or
        locked-out code. Raises ``NotLinked`` without a private link and ``AlreadyLinked`` when a group is linked.
        """
        if self.linked_chat_id() is None:
            raise NotLinked("link the private chat first")
        if self.linked_group_id() is not None:
            raise AlreadyLinked("a group is already linked")
        result = self._pairing.approve_code(GROUP_PLATFORM, code)
        if result is None:
            return None
        group_id = str(result["user_id"])
        keep = (self._store.load() or {}).get("avisos_group_id") == group_id  # relink: reuse its Avisos topic
        self._save({"group_id": group_id, "avisos": "no_permission",
                    **({} if keep else {"avisos_thread_id": None})})
        await self.ensure_avisos_topic()
        return result

    def _forget_group_pairing(self) -> None:
        for entry in self._pairing.list_approved(GROUP_PLATFORM):
            self._pairing.revoke(GROUP_PLATFORM, entry["user_id"])
        self._pairing.clear_pending(GROUP_PLATFORM)

    def unlink_group(self) -> bool:
        """Unlink the group only; the private link stays. Returns True when a group was linked."""
        existed = self.linked_group_id() is not None
        self._forget_group_pairing()
        if existed:
            record = dict(self._store.load() or {})
            for key in ("group_id", "avisos"):
                record.pop(key, None)
            self._store.save(record)
        return existed

    async def create_topic(
        self, chat_id: int, name: str, icon_color: Optional[int] = None, icon_custom_emoji_id: Optional[str] = None) -> Optional[int]:
        """Create a forum topic in a chat (Bot API 9.4+); message_thread_id or None."""
        if not self._bot:
            return None
        try:
            kwargs: Dict[str, Any] = {"chat_id": chat_id, "name": name}
            if icon_color is not None:
                kwargs["icon_color"] = icon_color
            if icon_custom_emoji_id:
                kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id
            topic = await self._bot.create_forum_topic(**kwargs)
            thread_id = topic.message_thread_id
            logger.info("Created topic '%s' in chat %s -> thread_id=%s", name, chat_id, thread_id)
            return thread_id
        except Exception as e:
            error_text = str(e).lower()
            # Telegram has no "list topics" API: an existing topic is mapped from incoming messages.
            if "topic_name_duplicate" in error_text or "already" in error_text:
                logger.info(
                    "Topic '%s' already exists in chat %s (will be mapped from incoming messages)", name, chat_id)
            elif "not a forum" in error_text or "forums_disabled" in error_text:
                logger.warning(
                    "Cannot create topic '%s' in chat %s: Topics mode is not enabled. "
                    "The group needs 'Topics' enabled in its settings before topics can be created.", name, chat_id)
            else:
                logger.warning(
                    "Failed to create topic '%s' in chat %s: %s", name, chat_id, _redact_telegram_error_text(e))
            return None

    async def ensure_avisos_topic(self) -> bool:
        """Create the Avisos topic in the linked group; records ``avisos`` as ``ok`` or ``no_permission``."""
        group_id = self.linked_group_id()
        if group_id is None or self._bot is None:
            return False
        name = self._t("avisos_topic_name")
        record = self._store.load() or {}
        thread_id = record.get("avisos_thread_id") if record.get("avisos_group_id") == group_id else None
        thread_id = thread_id or await self.create_topic(normalize_telegram_chat_id(group_id), name)
        self._save({"avisos_thread_id": thread_id, "avisos_group_id": group_id if thread_id else None,
                    "avisos": "ok" if thread_id else "no_permission"})
        if thread_id:
            self._topics[thread_id] = name
        return thread_id is not None

    def unlink(self) -> bool:
        """Remove the link (group included) and every pairing grant. Returns True when a link existed."""
        existed = self.linked_chat_id() is not None
        for entry in self._pairing.list_approved(PLATFORM):
            self._pairing.revoke(PLATFORM, entry["user_id"])
        self._pairing.clear_pending(PLATFORM)
        self._forget_group_pairing()
        self._store.clear()
        return existed

    # ----- incoming -----

    async def _handle_text_message(self, update: Any, context: Any = None) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        chat = getattr(update, "effective_chat", None) or getattr(message, "chat", None)
        if message is None or chat is None:
            return
        chat_id = str(chat.id)
        user = getattr(update, "effective_user", None)
        if getattr(chat, "type", None) != "private":
            await self._handle_group_text(update)
            return
        if str(getattr(user, "id", chat_id)) != chat_id:
            self._note_ignored(chat_id)
            return
        if self.linked_chat_id() is None:
            await self._offer_pairing_code(message, chat, update)
            return
        if not self._is_authorized(chat_id):
            self._note_ignored(chat_id)
            return
        text = getattr(message, "text", None) or ""
        try:
            reply = self._on_owner_text(text)
            if hasattr(reply, "__await__"):
                reply = await reply
        except Exception as e:
            logger.error("Owner text handler failed: %s", _redact_telegram_error_text(e))
            return
        if reply:
            await self._bot.send_message(chat_id=normalize_telegram_chat_id(chat_id), text=str(reply))

    def _group_context(self, update: Any) -> Optional[tuple]:
        """``(message, chat, thread_id)`` for a message of the linked user in the linked group; anything else is
        counted as ignored (its content is never kept) and gives ``None``."""
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        chat = getattr(update, "effective_chat", None) or getattr(message, "chat", None)
        if message is None or chat is None:
            return None
        user = getattr(update, "effective_user", None)
        if not self._sender_ok(str(chat.id), str(getattr(user, "id", ""))):
            self._note_ignored(chat.id)
            return None
        return message, chat, _topic_thread_id(message)

    def _remember_topic(self, thread_id: Optional[int], message: Any) -> Optional[str]:
        origin = getattr(message, "reply_to_message", None)
        created = getattr(origin, "forum_topic_created", None)
        creator = str(getattr(getattr(origin, "from_user", None), "id", ""))
        # The name picks the agent: take it only from a topic the linked user created.
        if thread_id is not None and getattr(created, "name", None) and creator == self.linked_chat_id():
            self._topics[thread_id] = created.name
        return self._topics.get(thread_id)

    async def _handle_group_text(self, update: Any) -> None:
        context = self._group_context(update)
        if context is None:
            return
        message, chat, thread_id = context
        topic_name = self._remember_topic(thread_id, message)
        # General does not converse and Avisos is for notices: neither is answered.
        if thread_id is None or thread_id == (self._store.load() or {}).get("avisos_thread_id"):
            return
        text = getattr(message, "text", None) or ""
        try:
            reply = self._on_owner_text(text, thread_id, topic_name)
            if hasattr(reply, "__await__"):
                reply = await reply
        except Exception as e:
            logger.error("Owner text handler failed: %s", _redact_telegram_error_text(e))
            return
        if reply:
            await self._bot.send_message(
                chat_id=normalize_telegram_chat_id(chat.id), text=str(reply), message_thread_id=thread_id)

    async def _handle_reset(self, update: Any, context: Any = None) -> None:
        """``/new`` or ``/reset`` in a topic: the host starts that topic's session over."""
        group = self._group_context(update)
        if group is None or group[2] is None or self._on_reset is None:
            return
        _, chat, thread_id = group
        if thread_id == (self._store.load() or {}).get("avisos_thread_id"):
            return
        result = self._on_reset(thread_id)
        if hasattr(result, "__await__"):
            await result
        await self._bot.send_message(
            chat_id=normalize_telegram_chat_id(chat.id), text=self._t("session_reset"), message_thread_id=thread_id)

    async def _handle_vincular(self, update: Any, context: Any = None) -> None:
        """``/vincular`` in a group: only the linked user gets a one-use code, and only while no group is linked."""
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        chat = getattr(update, "effective_chat", None) or getattr(message, "chat", None)
        user = getattr(update, "effective_user", None)
        if message is None or chat is None:
            return
        if self.linked_group_id() is not None or str(getattr(user, "id", "")) != self.linked_chat_id():
            self._note_ignored(chat.id)
            return
        code = self._pairing.generate_code(GROUP_PLATFORM, str(chat.id), str(getattr(chat, "title", "") or ""))
        if code is None:
            return
        kwargs = {"message_thread_id": _topic_thread_id(message)} if _topic_thread_id(message) else {}
        await self._bot.send_message(
            chat_id=normalize_telegram_chat_id(chat.id), text=self._t("group_pairing_code_message", code=code), **kwargs)

    async def _handle_topic_event(self, update: Any, context: Any = None) -> None:
        """Topic created or renamed in the linked group: remember its name."""
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        chat = getattr(message, "chat", None)
        user = getattr(update, "effective_user", None)
        # Only the linked user names topics: the name picks the agent, so another member must not rename one.
        if message is None or not self._sender_ok(str(getattr(chat, "id", "")), str(getattr(user, "id", ""))):
            return
        event = getattr(message, "forum_topic_created", None) or getattr(message, "forum_topic_edited", None)
        thread_id = _message_thread_id_for_send(str(getattr(message, "message_thread_id", None) or message.message_id))
        if thread_id is not None and getattr(event, "name", None):
            self._topics[thread_id] = event.name

    async def _offer_pairing_code(self, message: Any, chat: Any, update: Any) -> None:
        user = getattr(update, "effective_user", None)
        user_name = str(getattr(user, "first_name", "") or getattr(chat, "title", "") or "")
        code = self._pairing.generate_code(PLATFORM, str(chat.id), user_name)
        if code is None:
            return
        await self._bot.send_message(
            chat_id=normalize_telegram_chat_id(chat.id), text=self._t("pairing_code_message", code=code))

    # ----- outgoing -----

    def _destination(self, thread_id: Optional[int] = None) -> tuple:
        """``(chat_id, thread_id)`` to send to. Without a group: the private chat. With one: the topic asked for,
        or Avisos when none is given (General, with no thread, when Avisos could not be created)."""
        chat_id = self.linked_chat_id()
        group_id = self.linked_group_id()
        if chat_id is None or group_id is None or thread_id == 0:
            return chat_id, None  # 0 is the private session (chat_id, 0)
        if thread_id is None:
            thread_id = (self._store.load() or {}).get("avisos_thread_id")
        return group_id, _message_thread_id_for_send(thread_id)

    async def send_text(self, text: str, thread_id: Optional[int] = None) -> SendResult:
        """Send a plain message: to ``thread_id`` in the linked group, ``0`` to the private chat, ``None`` to Avisos
        (or the private chat when there is no group)."""
        chat_id, thread_id = self._destination(thread_id)
        if self._bot is None or chat_id is None:
            return SendResult(success=False, error="Not connected")
        kwargs = {"message_thread_id": thread_id} if thread_id is not None else {}
        try:
            msg = await self._bot.send_message(chat_id=normalize_telegram_chat_id(chat_id), text=text, **kwargs)
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            return SendResult(success=False, error=_redact_telegram_error_text(e))

    async def send_exec_approval(self, command: str, request_id: str, description: str = "", **kwargs: Any) -> SendResult:
        """Send an approval prompt with buttons to Avisos, or to the linked private chat when there is no group."""
        chat_id, thread_id = self._destination()
        if self._ui is None or chat_id is None:
            return SendResult(success=False, error="Not connected")
        return await self._ui.send_exec_approval(chat_id, command, request_id, description, thread_id=thread_id, **kwargs)
