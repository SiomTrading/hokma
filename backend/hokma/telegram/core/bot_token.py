"""Telegram bot-token shape check."""

from __future__ import annotations

import re

_TELEGRAM_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")


def is_valid_telegram_bot_token(token: object) -> bool:
    """Return True when *token* has Telegram's bot-token shape."""
    return isinstance(token, str) and bool(_TELEGRAM_BOT_TOKEN_RE.match(token))
