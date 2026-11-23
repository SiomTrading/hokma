"""Approval of an order by Telegram (SPEC-018: OA-52). In level 2 the ticket reaches the owner with the buttons Approve and Reject on
the approvals interface of the Telegram link. Only the linked chat of the owner can tap them; a tap approves exactly the
fingerprint printed on the ticket (the callback carries its first 12 characters and a stale one is refused); with no answer in 30
minutes the order expires (`broker.service.PENDING_MINUTES`).

`notice` queues a plain message in `telegram_outbox` (the link retries it); the buttons are sent only while a Telegram link is up."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable
from typing import Any

from hokma.exec import ticket as ticket_module

log = logging.getLogger("hokma.exec")

PREFIX = "ox:"
_CALLBACK = re.compile(r"ox:(ok|no):([0-9]{1,12}):([0-9a-f]{12})")

# Set by the Telegram link when it is up: an async function (proposal_id, fingerprint, text) that sends the ticket with its buttons.
_button_sender: Callable[[int, str, str], Any] | None = None


def set_button_sender(sender: Callable[[int, str, str], Any] | None) -> None:
    global _button_sender
    _button_sender = sender


def notice(u: Any, text: str) -> None:
    """Queue a message for the owner. Only queued: the link delivers it when Telegram accepts it. A failure never blocks the record."""
    from hokma.telegram.glue import order_notice

    try:
        if u.session.one("SELECT 1 AS x FROM telegram_link WHERE id = 1") is None:
            return
        order_notice(u, "order", text)
    except Exception:
        log.warning("the notice of an order could not be queued")


def request_buttons(proposal_id: int, fingerprint: str, text: str) -> None:
    """Send the ticket with its two buttons when the link is up; otherwise the queued notice is all the owner gets."""
    sender = _button_sender
    if sender is None:
        return
    try:
        outcome = sender(proposal_id, fingerprint, text)
        if asyncio.iscoroutine(outcome):
            try:
                asyncio.get_running_loop().create_task(outcome)
            except RuntimeError:
                with contextlib.suppress(Exception):
                    outcome.close()
    except Exception:
        log.warning("the buttons of an order could not be sent")


def parse_callback(data: str) -> tuple[str, int, str] | None:
    """(choice, proposal id, short fingerprint) of an `ox:` callback, or None for anything else."""
    match = _CALLBACK.fullmatch(data or "")
    return None if match is None else (match.group(1), int(match.group(2)), match.group(3))


def callback_data(choice: str, proposal_id: int, fingerprint: str) -> str:
    return f"{PREFIX}{'ok' if choice == 'approve' else 'no'}:{proposal_id}:{fingerprint[:12]}"


def answer(data: str, *, db: Any = None) -> str:
    """What happens when an authorized owner taps a button: the code of the result (approved, filled, rejected, stale, closed, expired...).
    Blocking: the approval sends the order."""
    from hokma.broker.service import BrokerError, owner_who
    from hokma.exec import approvals

    parsed = parse_callback(data)
    if parsed is None:
        return "invalid"
    choice, proposal_id, short = parsed
    try:
        row = approvals.proposal_row(proposal_id, db=db)
        if row is None or row["fingerprint"][:12] != short:
            return "stale"
        outcome = (approvals.approve if choice == "ok" else approvals.reject)(proposal_id, who=owner_who(), db=db)
    except BrokerError as exc:
        return {"decision_expired": "expired", "decision_closed": "closed", "decision_not_found": "stale"}.get(exc.code, exc.code)
    return outcome.status


def build_ticket_message(proposal_id: int, fingerprint: str, text: str, labels: tuple[str, str]) -> tuple[str, Any]:
    """The text (HTML escaped) and the keyboard of a ticket. `labels` are the two button texts in the language of the owner."""
    import html

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(labels[0], callback_data=callback_data("approve", proposal_id, fingerprint)),
                                      InlineKeyboardButton(labels[1], callback_data=callback_data("reject", proposal_id, fingerprint))]])
    return f"<pre>{html.escape(text[:3500])}</pre>", keyboard


def make_ui_class(base: type) -> type:
    """The approvals interface of the link with the order buttons added. `base` is the class the link already uses; its other buttons
    keep working. The tap of an unauthorized user is refused by the same gate as the other buttons."""

    class OrderTicketUI(base):                              # type: ignore[misc, valid-type]
        async def send_order_ticket(self, chat_id: Any, thread_id: Any, proposal_id: int, fingerprint: str, text: str, labels: tuple[str, str]):
            def build():
                message, keyboard = build_ticket_message(proposal_id, fingerprint, text, labels)
                return message, keyboard, None
            from telegram.constants import ParseMode

            return await self._send_prompt("send_order_ticket", chat_id, build, parse_mode=ParseMode.HTML, thread_id=thread_id)

        async def handle_callback_query(self, update: Any, context: Any = None) -> None:
            query = update.callback_query
            data = getattr(query, "data", None) or ""
            if not data.startswith(PREFIX):
                return await super().handle_callback_query(update, context)
            if parse_callback(data) is None:
                return await query.answer(text=self._t("approval_invalid"))
            if not await self._callback_authorized(query, self._callback_ctx(query), f"\u26d4 {self._t('approval_not_authorized')}"):
                return
            result = await asyncio.to_thread(answer, data)
            await query.answer(text=result)
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)

    return OrderTicketUI
