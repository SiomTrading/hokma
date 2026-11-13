"""Telegram link: long-poll loop, pairing with one owner chat, connect and disconnect.

Ports injected by the host application:
  store            object with ``load() -> dict | None``, ``save(dict)``, ``clear()`` (the single link record)
  is_authorized    ``(chat_id) -> bool``; defaults to "the chat saved in the store"
  on_owner_text    ``(text) -> str | None | awaitable``; the reply, if any, goes back to the chat
  on_decision      ``(request_id, choice) -> int | awaitable``; how many pending requests were resolved
  t                ``(key, **kw) -> str``; i18n
  on_ignored       ``(chat_id) -> None``; called for every message or tap from a chat that is not the linked one
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

from .approvals_ui import ApprovalsUI, SendResult, _redact_telegram_error_text
from .bot_token import is_valid_telegram_bot_token
from .pairing import PairingStore
from .telegram_ids import normalize_telegram_chat_id
from .telegram_network import (
    SEED_FALLBACK_IPS, TelegramFallbackTransport, discover_fallback_ips, tcp_keepalive_socket_options)

logger = logging.getLogger(__name__)

PLATFORM = "telegram"
_UPDATER_START_TIMEOUT = 30.0
_UPDATER_STOP_TIMEOUT = 10.0


class InvalidBotToken(ValueError):
    """The token does not have Telegram's bot-token shape."""


class AlreadyLinked(RuntimeError):
    """A chat is already linked; unlink it first."""


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
        application_factory: Callable[[str], Awaitable[Any]] = build_application) -> None:
        self._pairing = PairingStore(data_dir)
        self._store = store
        self._t = t
        self._on_owner_text = on_owner_text
        self._on_decision = on_decision
        self._is_authorized = is_authorized or self._default_is_authorized
        self._on_ignored = on_ignored
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
            on_rejected=self._note_ignored)
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
        from telegram.ext import CallbackQueryHandler, MessageHandler, filters
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

    def unlink(self) -> bool:
        """Remove the link and every pairing grant. Returns True when a link existed."""
        existed = self.linked_chat_id() is not None
        for entry in self._pairing.list_approved(PLATFORM):
            self._pairing.revoke(PLATFORM, entry["user_id"])
        self._pairing.clear_pending(PLATFORM)
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
        # Only a private chat can be linked or obeyed: in a group the chat id says nothing about who wrote.
        if getattr(chat, "type", None) != "private" or str(getattr(user, "id", chat_id)) != chat_id:
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

    async def _offer_pairing_code(self, message: Any, chat: Any, update: Any) -> None:
        user = getattr(update, "effective_user", None)
        user_name = str(getattr(user, "first_name", "") or getattr(chat, "title", "") or "")
        code = self._pairing.generate_code(PLATFORM, str(chat.id), user_name)
        if code is None:
            return
        await self._bot.send_message(
            chat_id=normalize_telegram_chat_id(chat.id), text=self._t("pairing_code_message", code=code))

    # ----- outgoing -----

    async def send_text(self, text: str) -> SendResult:
        """Send a plain message to the linked chat."""
        chat_id = self.linked_chat_id()
        if self._bot is None or chat_id is None:
            return SendResult(success=False, error="Not connected")
        try:
            msg = await self._bot.send_message(chat_id=normalize_telegram_chat_id(chat_id), text=text)
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            return SendResult(success=False, error=_redact_telegram_error_text(e))

    async def send_exec_approval(self, command: str, request_id: str, description: str = "", **kwargs: Any) -> SendResult:
        """Send an approval prompt with buttons to the linked chat."""
        chat_id = self.linked_chat_id()
        if self._ui is None or chat_id is None:
            return SendResult(success=False, error="Not connected")
        return await self._ui.send_exec_approval(chat_id, command, request_id, description, **kwargs)
