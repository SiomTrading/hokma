"""What Hokma adds on top of `core/` (a verbatim copy of a shared module, see MANIFEST.sha256): the pairing store
tells when it is locked or rate limited and records the rate limit only once the code was delivered (bug 1154),
and the approval prompts name where a permission comes from and say so when a button outlived a restart (1155, 1157)."""
from __future__ import annotations

import contextlib
import secrets
import time
from typing import Any, Optional

from hokma.telegram.core.approvals_ui import ApprovalsUI
from hokma.telegram.core.pairing import (
    RATE_LIMIT_SECONDS, PairingStore, _is_hashed_entry, _user_id_aliases)


class HokmaPairingStore(PairingStore):
    def __init__(self, data_dir) -> None:
        super().__init__(data_dir)
        self._defer_rate_limit = False
        self._deferred: Optional[tuple[str, str]] = None

    # ----- lockout and rate limit, readable -----

    def lockout_seconds_left(self, platform: str) -> int:
        """Seconds until the platform accepts codes again (0 when it is not locked out)."""
        with self._lock:
            return max(0, int(self._limits().get(f"_lockout:{platform}", 0) - time.time() + 0.999))

    def is_locked_out(self, platform: str) -> bool:
        return self.lockout_seconds_left(platform) > 0

    def rate_limit_seconds_left(self, platform: str, user_id: str) -> int:
        """Seconds until this user may ask for another code (0 when free)."""
        with self._lock:
            limits, now = self._limits(), time.time()
            left = [RATE_LIMIT_SECONDS - (now - limits.get(f"{platform}:{alias}", 0))
                    for alias in _user_id_aliases(platform, user_id)]
            return max(0, int(max(left, default=0) + 0.999))

    # ----- a code whose delivery is confirmed before it counts against the user -----

    def _record_rate_limit(self, platform: str, user_id: str) -> None:
        if self._defer_rate_limit:
            self._deferred = (platform, user_id)
            return
        super()._record_rate_limit(platform, user_id)

    def generate_code_deferred(self, platform: str, user_id: str, user_name: str = "") -> Optional[str]:
        """Same as `generate_code`, but the rate limit is not recorded: call `commit_rate_limit` after the code was
        delivered, or `discard_code` when it was not."""
        with self._lock:
            self._defer_rate_limit, self._deferred = True, None
            try:
                return self.generate_code(platform, user_id, user_name)
            finally:
                self._defer_rate_limit = False

    def commit_rate_limit(self, platform: str, user_id: str) -> None:
        with self._lock:
            super()._record_rate_limit(platform, user_id)

    def discard_code(self, platform: str, code: str) -> bool:
        """Forget a pending code that never reached the user (it would only take one of the pending places)."""
        with self._lock:
            pending = self._load_json(self._pending_path(platform))
            for entry_id, entry in list(pending.items()):
                if not _is_hashed_entry(entry):
                    continue
                with contextlib.suppress(ValueError):
                    if self._hash_code(code, bytes.fromhex(entry["salt"])) == entry["hash"]:
                        del pending[entry_id]
                        self._save_json(self._pending_path(platform), pending)
                        return True
        return False


class HokmaApprovalsUI(ApprovalsUI):
    """Prompts say where the permission comes from; a button of an earlier run says so and loses its keyboard."""

    _origin: Optional[str] = None

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._claimed: set[str] = set()         # approval ids already answered in this run

    def _format_exec_approval(self, command: str, description: str = "dangerous command") -> str:
        text = super()._format_exec_approval(command, description)
        if self._origin:
            return f"<b>{self._ea_escape(self._origin)}</b>\n" + text
        return text

    async def send_exec_approval(self, chat_id, command, request_id, description="dangerous command",
                                 thread_id=None, origin: Optional[str] = None):
        # The text is built synchronously, before the first await of the send: no other prompt can interleave.
        self._origin = origin
        try:
            return await super().send_exec_approval(chat_id, command, request_id, description, thread_id=thread_id)
        finally:
            self._origin = None

    async def _claim_callback_state(self, query, cb, state, key, denial, resolved):
        if not await self._callback_authorized(query, cb, denial):
            return None
        request_id = state.pop(key, None)
        if request_id:
            self._claimed.add(key)
            return request_id
        if key in self._claimed:                          # a second tap on a prompt answered in this run
            await query.answer(text=resolved)
        else:                                             # a prompt from before a restart: its state died with Hokma
            await query.answer(text=self._t("approval_gone"))
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)
        return None


class LateRunUI(HokmaApprovalsUI):
    """The two buttons under "this routine was missed and is too old": `lr:run:<token>` runs it now, `lr:skip:<token>`
    leaves it. The token is random, single use and kept only here: a button of an earlier run (or a forged one) finds
    nothing and is refused, like the `ea:` buttons."""

    on_late: Any = None                      # async (job_id, choice) -> the text that replaces the question

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._late: dict[str, int] = {}      # token -> routine the question was about

    async def send_late_prompt(self, chat_id, thread_id, text: str, job_id: int, run_label: str, skip_label: str):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        token = secrets.token_hex(6)

        def build():
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(run_label, callback_data=f"lr:run:{token}"),
                                              InlineKeyboardButton(skip_label, callback_data=f"lr:skip:{token}")]])
            return text, keyboard, lambda _msg: self._late.__setitem__(token, job_id)
        return await self._send_prompt("send_late_prompt", chat_id, build, parse_mode=None, thread_id=thread_id)

    async def handle_callback_query(self, update: Any, context: Any = None) -> None:
        query = update.callback_query
        data = getattr(query, "data", None) or ""
        if not data.startswith("lr:"):
            return await super().handle_callback_query(update, context)
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in ("run", "skip") or self.on_late is None:
            return await query.answer(text=self._t("approval_invalid"))
        if not await self._callback_authorized(query, self._callback_ctx(query), f"⛔ {self._t('approval_not_authorized')}"):
            return
        job_id = self._late.pop(parts[2], None)
        if job_id is None:                                # answered already, or from before a restart
            await query.answer(text=self._t("approval_gone"))
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)
            return
        answer = await self.on_late(job_id, parts[1])
        await query.answer()
        with contextlib.suppress(Exception):
            await query.edit_message_text(text=answer, reply_markup=None)
