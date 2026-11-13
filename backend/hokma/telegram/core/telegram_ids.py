"""Helpers for Telegram Bot API chat identifiers: a ``chat_id`` is a numeric ID (int) or an ``@username``
string for public channels/groups; a bare ``int(chat_id)`` crashes on the username form."""

from __future__ import annotations

from typing import Any, Union


def normalize_telegram_chat_id(chat_id: Any) -> Union[int, str]:
    """Bot API-compatible chat_id: numeric values (incl. negative channel IDs) as ``int``, anything
    else (e.g. ``@username``) as a stripped string; never raises."""
    chat_id_str = str(chat_id).strip()
    try:
        return int(chat_id_str)
    except (TypeError, ValueError):
        return chat_id_str
