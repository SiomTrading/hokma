"""One error type for desks. `code` is stable; the message is localized from the catalog."""
from __future__ import annotations

from typing import Callable

from hokma.i18n import t

# One literal t(...) per message, so the catalog check can see that every key is used.
_MESSAGES: dict[str, Callable[[], str]] = {
    "not_found": lambda: t("desks.err.not_found"),
    "bad_name": lambda: t("desks.err.bad_name"),
    "bad_focus": lambda: t("desks.err.bad_focus"),
    "bad_template": lambda: t("desks.err.bad_template"),
    "bad_capital": lambda: t("desks.err.bad_capital"),
    "bad_objective": lambda: t("desks.err.bad_objective"),
    "bad_mandate": lambda: t("desks.err.bad_mandate"),
    "bad_budget": lambda: t("desks.err.bad_budget"),
    "bad_status": lambda: t("desks.err.bad_status"),
    "bad_model": lambda: t("desks.err.bad_model"),
    "bad_role": lambda: t("desks.err.bad_role"),
    "bad_assets": lambda: t("desks.err.bad_assets"),
    "agent_missing": lambda: t("desks.err.agent_missing"),
    "agent_in_desk": lambda: t("desks.err.agent_in_desk"),
    "agent_not_in_desk": lambda: t("desks.err.agent_not_in_desk"),
    "agent_off": lambda: t("desks.err.agent_off"),
    "chief_required": lambda: t("desks.err.chief_required"),
    "folder_failed": lambda: t("desks.err.folder_failed"),
    "db_failed": lambda: t("desks.err.db_failed"),
    "bad_symbol": lambda: t("desks.err.bad_symbol"),
    "bad_connection": lambda: t("desks.err.bad_connection"),
    "desk_archived": lambda: t("desks.err.desk_archived"),
    "desk_budget_exhausted": lambda: t("desks.err.desk_budget_exhausted"),
}


class DeskError(Exception):
    """A desk operation failed. `code` is stable for the API; `status` is the HTTP status it maps to."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status

    @property
    def message(self) -> str:
        text = _MESSAGES.get(self.code)
        return text() if text else self.code
