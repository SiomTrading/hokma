"""Localized text of the API error codes of D7. Literal keys, so the catalog test sees every one of them."""
from __future__ import annotations

from typing import Callable

from hokma.i18n import t

_TEXT: dict[str, Callable[[], str]] = {
    "agent_not_found": lambda: t("agents.error.agent_not_found"),
    "desk_not_found": lambda: t("agents.error.desk_not_found"),
    "not_desk_agent": lambda: t("agents.error.not_desk_agent"),
    "desk_has_no_chief": lambda: t("agents.error.desk_has_no_chief"),
    "too_many_runs": lambda: t("agents.error.too_many_runs"),
    "agent_invalid": lambda: t("agents.error.agent_invalid"),
    "model_invalid": lambda: t("agents.error.model_invalid"),
    "status_invalid": lambda: t("agents.error.status_invalid"),
    "orchestrator_required": lambda: t("agents.error.orchestrator_required"),
    "name_invalid": lambda: t("agents.error.name_invalid"),
    "description_required": lambda: t("agents.error.description_required"),
    "instructions_required": lambda: t("agents.error.instructions_required"),
    "agent_exists": lambda: t("agents.error.agent_exists"),
    "agent_file_invalid": lambda: t("agents.error.agent_file_invalid"),
    "agent_file_unsafe": lambda: t("agents.error.agent_file_unsafe"),
    "cap_invalid": lambda: t("agents.error.cap_invalid"),
    "profile_missing": lambda: t("agents.error.profile_missing"),
    "job_not_found": lambda: t("agents.error.job_not_found"),
    "job_name_taken": lambda: t("agents.error.job_name_taken"),
    "job_invalid": lambda: t("agents.error.job_invalid"),
    "prompt_required": lambda: t("agents.error.prompt_required"),
    "prompt_too_long": lambda: t("agents.error.prompt_too_long"),
    "kind_invalid": lambda: t("agents.error.kind_invalid"),
    "timezone_invalid": lambda: t("agents.error.timezone_invalid"),
    "schedule_invalid": lambda: t("agents.error.schedule_invalid"),
    "interval_out_of_range": lambda: t("agents.error.interval_out_of_range"),
    "confirmation_required": lambda: t("agents.error.confirmation_required"),
    "agent_off": lambda: t("agents.error.agent_off"),
    "job_running": lambda: t("agents.error.job_running"),
    "job_not_approved": lambda: t("agents.error.job_not_approved"),
    "scheduler_off": lambda: t("agents.error.scheduler_off"),
    "name_too_long": lambda: t("agents.error.name_too_long"),
    "run_in_past": lambda: t("agents.error.run_in_past"),
}


def message(code: str, fallback: str = "") -> str:
    """The text for `code` in the active language; `fallback` (English, for logs) when the code is unknown."""
    text = _TEXT.get(code)
    return text() if text else (fallback or code)
