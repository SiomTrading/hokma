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
# the codes of a tap of a notice that have a sentence in the language of the owner (the rest say "not completed"): the raw code never reaches his phone
ANSWER_CODES = frozenset({"scope", "close_confirm", "filled", "sent", "partially_filled", "canceled", "rejected_flat", "rejected_flatten_failed", "stale", "expired",
                          "closed", "invalid", "wrong_stage"})
# the codes of a tap of a notice of a cap that have a sentence in the language of the owner
CAP_ANSWER_CODES = frozenset({"followed", "blocked", "block_failed", "close_confirm", "closed", "close_failed", "back", "stale", "expired", "invalid", "wrong_stage", "changed"})
_CALLBACK = re.compile(r"ox:(ok|no):([0-9]{1,12}):([0-9a-f]{12})")

# Set by the Telegram link when it is up: an async function (proposal_id, fingerprint, text) that sends the ticket with its buttons.
_button_sender: Callable[[int, str, str], Any] | None = None
# The same for the notice of a rule (SPEC-026 3390b): an async function (proposal_id, nonce, text) that sends it with Follow, No and Close.
_notice_sender: Callable[[int, str, str], Any] | None = None


# The same for the notice of a cap with no order (SPEC-026 3390e4): an async function (cap_id, nonce, text) that sends it with its three buttons.
_cap_sender: Callable[[int, str, str], Any] | None = None


def set_cap_sender(sender: Callable[[int, str, str], Any] | None) -> None:
    global _cap_sender
    _cap_sender = sender


def request_cap_buttons(cap_id: int, nonce: str, text: str) -> bool | None:
    """Send the notice of a cap with its three buttons when the link is up; otherwise the queued text and the card of the chat are all the owner gets.
    True when the sender took it, False when it failed (or there is no loop to run it), None when no sender is set (this channel does not count)."""
    sender = _cap_sender
    if sender is None:
        return None
    return _run_sender(sender, (cap_id, nonce, text), "the buttons of a notice of a cap could not be sent")


def set_notice_sender(sender: Callable[[int, str, str], Any] | None) -> None:
    global _notice_sender
    _notice_sender = sender


def request_notice_buttons(proposal_id: int, nonce: str, text: str) -> None:
    """Send the notice with its three buttons when the link is up; otherwise the queued text and the card of the chat are all the owner gets."""
    sender = _notice_sender
    if sender is None:
        return
    _run_sender(sender, (proposal_id, nonce, text), "the buttons of a notice could not be sent")


def set_button_sender(sender: Callable[[int, str, str], Any] | None) -> None:
    global _button_sender
    _button_sender = sender


def notice(u: Any, text: str) -> None:
    """Queue a message for the owner. Only queued: the link delivers it when Telegram accepts it. A failure never blocks the record."""
    try:
        notice_queued(u, text)
    except Exception:
        log.warning("the notice of an order could not be queued")


def notice_queued(u: Any, text: str) -> bool | None:
    """`notice` that says what happened and does not swallow: True queued, None when there is no Telegram link (nothing to queue to), and it raises when the queue fails."""
    from hokma.telegram.glue import order_notice

    if u.session.one("SELECT 1 AS x FROM telegram_link WHERE id = 1") is None:
        return None
    order_notice(u, "order", text)
    return True


def request_buttons(proposal_id: int, fingerprint: str, text: str) -> None:
    """Send the ticket with its two buttons when the link is up; otherwise the queued notice is all the owner gets."""
    sender = _button_sender
    if sender is None:
        return
    _run_sender(sender, (proposal_id, fingerprint, text), "the buttons of an order could not be sent")


def _run_sender(sender: Callable[..., Any], args: tuple, failure: str) -> bool:
    """Run a sender without letting it raise. True when it ran (an async one: when its task was scheduled), False when it failed or had no loop to run in."""
    try:
        outcome = sender(*args)
        if asyncio.iscoroutine(outcome):
            try:
                task = asyncio.get_running_loop().create_task(outcome)
            except RuntimeError:
                with contextlib.suppress(Exception):
                    outcome.close()
                log.warning(failure)
                return False
            task.add_done_callback(lambda t: t.cancelled() or t.exception() is None or log.warning(failure))
        return True
    except Exception:
        log.warning(failure)
        return False


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


def build_notice_message(proposal_id: int, nonce: str, text: str, labels: tuple[str, ...], *, stage: str = "ask") -> tuple[str, Any]:
    """The text (HTML escaped) and the keyboard of a notice. `labels` = Follow, No, Close, Only this order, The rest of the day, Confirm close. In the first
    stage the buttons are Follow, No and Close; once Follow was tapped, the question of the scope has Only this order, The rest of the day and No; once Close was
    tapped, the list of what would be closed has Confirm close and No."""
    import html

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    from hokma.exec import notices

    def button(label: str, choice: str):
        return InlineKeyboardButton(label, callback_data=notices.callback_data(choice, proposal_id, nonce))

    if stage == "close":
        keyboard = InlineKeyboardMarkup([[button(labels[5], notices.CONFIRM_CLOSE), button(labels[1], notices.NO)]])
    elif stage == "scope":
        keyboard = InlineKeyboardMarkup([[button(labels[3], notices.ONLY_ORDER), button(labels[4], notices.REST_OF_DAY)], [button(labels[1], notices.NO)]])
    else:
        keyboard = InlineKeyboardMarkup([[button(labels[0], notices.FOLLOW), button(labels[1], notices.NO), button(labels[2], notices.CLOSE)]])
    return f"<pre>{html.escape(text[:3500])}</pre>", keyboard


def build_cap_message(cap_id: int, nonce: str, text: str, labels: tuple[str, ...], *, stage: str = "ask") -> tuple[str, Any]:
    """The text (HTML escaped) and the keyboard of a notice of a cap. `labels` = Keep trading today, Block entries today, Close positions, Confirm close, Back. In the first stage the
    three answers; once Close was tapped, the list of what would be closed has Confirm close and Back."""
    import html

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    from hokma.exec import cap_notices

    def button(label: str, choice: str):
        return InlineKeyboardButton(label, callback_data=cap_notices.callback_data(choice, cap_id, nonce))

    if stage == "close":
        keyboard = InlineKeyboardMarkup([[button(labels[3], cap_notices.CONFIRM_CLOSE), button(labels[4], cap_notices.BACK)]])
    else:
        keyboard = InlineKeyboardMarkup([[button(labels[0], cap_notices.FOLLOW)], [button(labels[1], cap_notices.BLOCK)], [button(labels[2], cap_notices.CLOSE)]])
    return f"<pre>{html.escape(text[:3500])}</pre>", keyboard


def answer_cap(data: str, *, db: Any = None) -> Any:
    """What happens when an authorized owner taps a button of a notice of a cap: a `cap_notices.Tap`. The token is the one of this moment and the answer spends it."""
    from hokma.broker.service import owner_who
    from hokma.exec import cap_notices

    return cap_notices.tap(data, who=owner_who(), db=db)


def answer_notice(data: str, *, db: Any = None) -> Any:
    """What happens when an authorized owner taps a button of a notice: a `notices.Tap`. The token of the callback is the one of this moment and
    the answer spends it."""
    from hokma.broker.service import owner_who
    from hokma.exec import notices

    return notices.tap(data, who=owner_who(), db=db)


def make_ui_class(base: type) -> type:
    """The approvals interface of the link with the order buttons added. `base` is the class the link already uses; its other buttons
    keep working. The tap of an unauthorized user is refused by the same gate as the other buttons."""

    class OrderTicketUI(base):                              # type: ignore[misc, valid-type]
        async def send_order_notice(self, chat_id: Any, thread_id: Any, proposal_id: int, nonce: str, text: str, labels: tuple[str, ...]):
            def build():
                message, keyboard = build_notice_message(proposal_id, nonce, text, labels)
                return message, keyboard, None
            from telegram.constants import ParseMode

            return await self._send_prompt("send_order_notice", chat_id, build, parse_mode=ParseMode.HTML, thread_id=thread_id)

        async def send_cap_notice(self, chat_id: Any, thread_id: Any, cap_id: int, nonce: str, text: str, labels: tuple[str, ...]):
            def build():
                message, keyboard = build_cap_message(cap_id, nonce, text, labels)
                return message, keyboard, None
            from telegram.constants import ParseMode

            return await self._send_prompt("send_cap_notice", chat_id, build, parse_mode=ParseMode.HTML, thread_id=thread_id)

        async def _handle_cap_callback(self, query: Any, data: str) -> None:
            from hokma.exec import cap_notices
            from hokma.i18n import t

            if cap_notices.parse_callback(data) is None:
                return await query.answer(text=self._t("approval_invalid"))
            if not await self._callback_authorized(query, self._callback_ctx(query), f"\u26d4 {self._t('approval_not_authorized')}"):
                return
            result = await asyncio.to_thread(answer_cap, data)
            await query.answer(text=t(f"telegram.cap_answer.{result.code}") if result.code in CAP_ANSWER_CODES else t("telegram.cap_answer.other"))
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)
            if result.nonce and result.cap_id is not None and result.stage in ("close", "ask"):
                labels = (t("telegram.cap_btn_follow"), t("telegram.cap_btn_block"), t("telegram.cap_btn_close"), t("telegram.cap_btn_confirm"), t("telegram.cap_btn_back"))
                message, keyboard = build_cap_message(result.cap_id, result.nonce, result.text, labels, stage=result.stage)
                from telegram.constants import ParseMode

                with contextlib.suppress(Exception):
                    await query.message.reply_text(message, parse_mode=ParseMode.HTML, reply_markup=keyboard)

        async def _handle_notice_callback(self, query: Any, data: str) -> None:
            from hokma.exec import approvals, notices
            from hokma.i18n import t

            if notices.parse_callback(data) is None:
                return await query.answer(text=self._t("approval_invalid"))
            if not await self._callback_authorized(query, self._callback_ctx(query), f"\u26d4 {self._t('approval_not_authorized')}"):
                return
            result = await asyncio.to_thread(answer_notice, data)
            await query.answer(text=t(f"telegram.notice_answer.{result.code}") if result.code in ANSWER_CODES else t("telegram.notice_answer.other"))
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)
            if result.ask_close and result.nonce and result.proposal_id is not None:
                labels = (t("telegram.notice_btn_follow"), t("telegram.notice_btn_no"), t("telegram.notice_btn_close"), t("telegram.notice_btn_one"), t("telegram.notice_btn_day"),
                          t("telegram.notice_btn_close_confirm"))
                message, keyboard = build_notice_message(result.proposal_id, result.nonce, result.text, labels, stage="close")
                from telegram.constants import ParseMode

                with contextlib.suppress(Exception):
                    await query.message.reply_text(message, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            if result.ask_scope and result.nonce and result.proposal_id is not None:
                row = await asyncio.to_thread(approvals.proposal_row, result.proposal_id)
                notice = notices.notice_of(row or {}) or {}
                labels = (t("telegram.notice_btn_follow"), t("telegram.notice_btn_no"), t("telegram.notice_btn_close"), t("telegram.notice_btn_one"), t("telegram.notice_btn_day"))
                message, keyboard = build_notice_message(result.proposal_id, result.nonce, notices.scope_message(notice), labels, stage="scope")
                from telegram.constants import ParseMode

                with contextlib.suppress(Exception):
                    await query.message.reply_text(message, parse_mode=ParseMode.HTML, reply_markup=keyboard)

        async def send_order_ticket(self, chat_id: Any, thread_id: Any, proposal_id: int, fingerprint: str, text: str, labels: tuple[str, str]):
            def build():
                message, keyboard = build_ticket_message(proposal_id, fingerprint, text, labels)
                return message, keyboard, None
            from telegram.constants import ParseMode

            return await self._send_prompt("send_order_ticket", chat_id, build, parse_mode=ParseMode.HTML, thread_id=thread_id)

        async def handle_callback_query(self, update: Any, context: Any = None) -> None:
            query = update.callback_query
            data = getattr(query, "data", None) or ""
            if data.startswith("on:"):
                return await self._handle_notice_callback(query, data)
            if data.startswith("oc:"):
                return await self._handle_cap_callback(query, data)
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
