"""One error type for desks. `code` is stable; the message is localized from the catalog."""
from __future__ import annotations

from typing import Any, Callable

from hokma.i18n import t

# One literal t(...) per message, so the catalog check can see that every key is used.
_MESSAGES: dict[str, Callable[[], str]] = {
    "not_found": lambda: t("desks.err.not_found"),
    "bad_name": lambda: t("desks.err.bad_name"),
    "bad_focus": lambda: t("desks.err.bad_focus"),
    "bad_template": lambda: t("desks.err.bad_template"),
    "bad_capital": lambda: t("desks.err.bad_capital"),
    "bad_account": lambda: t("desks.err.bad_account"),
    "account_not_found": lambda: t("desks.err.account_not_found"),
    "no_demo_broker": lambda: t("desks.err.no_demo_broker"),
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
    "delete_failed": lambda: t("desks.err.delete_failed"),
    "delete_in_use": lambda: t("desks.err.delete_in_use"),
    "delete_move_failed": lambda: t("desks.err.delete_move_failed"),
    "delete_db_failed": lambda: t("desks.err.delete_db_failed"),
    "bad_panels": lambda: t("desks.err.bad_panels"),
    "panel_fixed": lambda: t("desks.err.panel_fixed"),
    "bad_range": lambda: t("desks.err.bad_range"),
    "bad_filter": lambda: t("desks.err.bad_filter"),
    "desk_archived": lambda: t("desks.err.desk_archived"),
    "desk_budget_exhausted": lambda: t("desks.err.desk_budget_exhausted"),
    "analysis_unavailable": lambda: t("desks.err.analysis_unavailable"),
    "analysis_running": lambda: t("desks.err.analysis_running"),
    "analysis_busy": lambda: t("desks.err.analysis_busy_total"),
    "analysis_not_found": lambda: t("desks.err.analysis_not_found"),
    "bad_strategy": lambda: t("desks.err.bad_strategy"),
    "bad_limit": lambda: t("desks.err.bad_limit"),
    "bad_strategy_args": lambda: t("desks.err.bad_strategy_args"),
    "bad_option_detail_args": lambda: t("desks.err.bad_option_detail_args"),
    "bad_saved_strategy": lambda: t("desks.err.bad_saved_strategy"),
    "saved_strategies_full": lambda: t("desks.err.saved_strategies_full"),
    "saved_strategy_not_found": lambda: t("desks.err.saved_strategy_not_found"),
}

# Why a desk has no analysis (SPEC-028, delivery 4): the reason shown to the owner instead of the generic message.
_ANALYSIS_REASONS: dict[str, Callable[[], str]] = {
    "not_options_desk": lambda: t("desks.err.analysis_not_options_desk"),
    "desk_archived": lambda: t("desks.err.analysis_desk_archived"),
    "no_runtime": lambda: t("desks.err.analysis_no_runtime"),
}


class DeskError(Exception):
    """A desk operation failed. `code` is stable for the API; `status` is the HTTP status it maps to."""

    def __init__(self, code: str, status: int = 400, **extra: Any) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.extra = extra                      # stable fields the API adds to the error (the run id of a live analysis, the reason a desk has none)

    @property
    def message(self) -> str:
        if self.code == "analysis_busy":
            return t("desks.err.analysis_busy_desk" if self.extra.get("scope") == "desk" else "desks.err.analysis_busy_total", limit=self.extra.get("limit"))
        if self.code == "analysis_unavailable" and self.extra.get("reason") in _ANALYSIS_REASONS:
            return _ANALYSIS_REASONS[self.extra["reason"]]()
        text = _MESSAGES.get(self.code)
        return text() if text else self.code
