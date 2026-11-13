"""Inline-keyboard approval prompts for Telegram.

A prompt carries buttons; a tap resolves the pending request exactly once through the
injected ``on_decision`` port. Taps from any chat other than the linked one are refused.
Button labels and messages come from the injected ``t`` (i18n) callable.
"""

from __future__ import annotations

import contextlib
import html as _html
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from .telegram_ids import normalize_telegram_chat_id

logger = logging.getLogger(__name__)

# Every char MarkdownV2 requires backslash-escaped outside code spans/fences.
_MDV2_ESCAPE_RE = re.compile(r'([_*\[\]()~`>#\+\-=|{}.!\\])')

_APPROVAL_ID_RE = re.compile(r"[0-9a-f]{12}")
_BOT_TOKEN_IN_TEXT_RE = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")


def _escape_mdv2(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters with a preceding backslash."""
    return _MDV2_ESCAPE_RE.sub(r'\\\1', text)


def _redact_telegram_error_text(error: object) -> str:
    """Redact secrets from Telegram transport errors before logging or returning them."""
    text = "" if error is None else str(error)
    return _BOT_TOKEN_IN_TEXT_RE.sub("<redacted>", text)


@dataclass
class SendResult:
    """Result of sending a message."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


class ApprovalsUI:
    """Sends approval prompts and resolves button taps.

    Ports: ``is_authorized(chat_id) -> bool``; ``on_decision(request_id, choice)`` returning how
    many pending requests it resolved (0 when none was waiting); ``t(key, **kw) -> str``;
    optional ``on_rejected(chat_id)`` called when a tap comes from an unauthorized chat.
    """

    _EA_CMD_BUDGET = 3800

    def __init__(
        self, bot: Any, *, is_authorized: Callable[[str], bool],
        on_decision: Callable[[str, str], "int | Awaitable[int]"],
        t: Callable[..., str], on_rejected: Optional[Callable[[str], None]] = None) -> None:
        self._bot = bot
        self._is_authorized = is_authorized
        self._on_decision = on_decision
        self._t = t
        self._on_rejected = on_rejected
        self._approval_state: Dict[str, str] = {}  # approval_id → request_id
        self._EA_HEADER = f"⚠️ <b>{t('approval_header')}</b>\n\n"
        self._EA_CODE_OPEN = "<pre>"
        self._EA_CODE_CLOSE = "</pre>\n\n"
        self._EA_REASON_LABEL = f"{t('approval_reason')}: "

    def set_bot(self, bot: Any) -> None:
        self._bot = bot

    # ----- copied prompt machinery -----

    def format_message(self, content: str) -> str:
        """Plain text to MarkdownV2: every special character is escaped."""
        return _escape_mdv2(content) if content else content

    @staticmethod
    def _truncate_preview(text: str, budget: int, suffix: str = "...") -> str:
        """Truncate ``text`` to ``budget`` chars, appending ``suffix`` when cut."""
        text = str(text or "")
        return text[:budget] + suffix if len(text) > budget else text

    def _ea_escape(self, text: str) -> str:
        return _html.escape(text)

    def _format_exec_approval(self, command: str, description: str = "dangerous command") -> str:
        """Prompt text: header + fenced (truncated) command + reason."""
        cmd_preview = self._truncate_preview(str(command or ""), self._EA_CMD_BUDGET)
        return (f"{self._EA_HEADER}"
                f"{self._EA_CODE_OPEN}{self._ea_escape(cmd_preview)}{self._EA_CODE_CLOSE}"
                f"{self._EA_REASON_LABEL}{self._ea_escape(description)}")

    async def _send_prompt(self, what: str, chat_id: str, build, *, parse_mode: Any = None) -> SendResult:
        """Shared control-prompt shell: not-connected guard, ``build()`` → ``(text, keyboard, on_sent)`` (or a
        SendResult to return as-is), send, state hook, redacted failure log."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        try:
            built = build()
            if isinstance(built, SendResult):
                return built
            text, keyboard, on_sent = built
            kwargs: Dict[str, Any] = {
                "chat_id": normalize_telegram_chat_id(chat_id), "text": text,
                "parse_mode": parse_mode if parse_mode is not None else ParseMode.MARKDOWN_V2}
            if keyboard is not None:
                kwargs["reply_markup"] = keyboard
            msg = await self._bot.send_message(**kwargs)
            if on_sent is not None:
                on_sent(msg)
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning("%s failed: %s", what, _redact_telegram_error_text(e))
            return SendResult(success=False, error=_redact_telegram_error_text(e))

    async def send_exec_approval(
        self, chat_id: str, command: str, request_id: str, description: str = "dangerous command") -> SendResult:
        """Send an inline-keyboard approval prompt (allow once, deny); buttons call ``on_decision``."""
        def build():
            text = self._format_exec_approval(command, description)
            # Short random ids in callback_data map back to request_id; never reused, so a keyboard left
            # over from an earlier connection cannot resolve a newer request.
            approval_id = secrets.token_hex(6)
            buttons = [InlineKeyboardButton(f"✅ {self._t('btn_allow_once')}", callback_data=f"ea:once:{approval_id}"),
                       InlineKeyboardButton(f"❌ {self._t('btn_deny')}", callback_data=f"ea:deny:{approval_id}")]
            return text, InlineKeyboardMarkup(
                [buttons]), lambda msg: self._approval_state.__setitem__(approval_id, request_id)
        return await self._send_prompt("send_exec_approval", chat_id, build, parse_mode=ParseMode.HTML)

    async def _edit_md_quiet(self, query, text_md: str) -> None:
        """MarkdownV2 edit with the keyboard removed; failures ignored (non-fatal)."""
        with contextlib.suppress(Exception):
            await query.edit_message_text(text=self.format_message(text_md), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=None)

    # ----- callbacks -----

    @staticmethod
    def _callback_ctx(query) -> Dict[str, Any]:
        """Chat/thread/user context of a button tap, for the callback auth gate."""
        query_message = getattr(query, "message", None)
        query_chat = getattr(query_message, "chat", None)
        return {
            "chat_id": getattr(query_message, "chat_id", None), "chat_type": getattr(query_chat, "type", None),
            "thread_id": getattr(query_message, "message_thread_id", None), "user_name": getattr(query.from_user, "first_name", None)}

    def _is_callback_user_authorized(self, user_id: str, *, chat_id: Optional[str] = None) -> bool:
        """Return whether a Telegram inline-button caller may perform gated actions: only the linked chat."""
        if not str(user_id or "").strip() or chat_id is None:
            return False
        if str(user_id) != str(chat_id):  # only a private chat: in a group the chat id is not the sender
            return False
        return bool(self._is_authorized(str(chat_id)))

    async def _callback_authorized(self, query, cb: Dict[str, Any], denial_text: str) -> bool:
        """Gate a button tap on the callback allowlist; answers ``denial_text`` when refused."""
        if self._is_callback_user_authorized(str(getattr(query.from_user, "id", "")), chat_id=cb["chat_id"]):
            return True
        if self._on_rejected is not None:
            with contextlib.suppress(Exception):
                self._on_rejected(str(cb["chat_id"]))
        await query.answer(text=denial_text)
        return False

    async def handle_callback_query(self, update: Any, context: Any = None) -> None:
        """Dispatch inline keyboard button clicks on the callback_data prefix."""
        query = update.callback_query
        if not query or not query.data:
            return
        data = query.data
        cb = self._callback_ctx(query)
        for prefix, handler in (("ea:", self._handle_exec_approval_callback),):
            if data.startswith(prefix):
                await handler(query, data, cb)
                return

    async def _claim_callback_state(self, query, cb: Dict[str, Any], state: dict, key, denial: str, resolved: str):
        """Auth-gate a button tap, then claim its pending entry; None (after answering) when refused or expired."""
        if not await self._callback_authorized(query, cb, denial):
            return None
        request_id = state.pop(key, None)
        if not request_id:
            await query.answer(text=resolved)
        return request_id

    async def _handle_exec_approval_callback(self, query, data: str, cb: Dict[str, Any]) -> None:
        """``ea:<choice>:<approval_id>``: resolve a pending exec approval."""
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        choice = parts[1]  # once, deny
        approval_id = parts[2]
        if choice not in ("once", "deny") or not _APPROVAL_ID_RE.fullmatch(approval_id):
            await query.answer(text=self._t("approval_invalid"))
            return
        request_id = await self._claim_callback_state(
            query, cb, self._approval_state, approval_id, f"⛔ {self._t('approval_not_authorized')}",
            self._t("approval_already_resolved"))
        if not request_id:
            return
        user_display = getattr(query.from_user, "first_name", "User")
        # Resolve FIRST, render after: a tap landing after the wait timed out (count == 0) must NOT
        # claim "Approved", the command was already denied.
        try:
            count = self._on_decision(request_id, choice)
            if hasattr(count, "__await__"):
                count = await count
            logger.info("Telegram button resolved %d approval(s) (choice=%s)", count or 0, choice)
        except Exception as exc:
            logger.error("Failed to resolve approval from Telegram button: %s", _redact_telegram_error_text(exc))
            count = 0
        if count:
            label = f"✅ {self._t('label_once')}" if choice == "once" else f"❌ {self._t('label_deny')}"
            edit_text = self._t("approval_label_by", label=label, user=user_display)
        else:
            label = f"⌛ {self._t('approval_expired')}"
            edit_text = f"{label}: {self._t('approval_expired_detail')}"
        await query.answer(text=label)
        await self._edit_md_quiet(query, edit_text)
