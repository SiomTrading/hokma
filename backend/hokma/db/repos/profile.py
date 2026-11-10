"""profile (settings, owner and fund) and license_checks."""
from __future__ import annotations

from typing import Any

from hokma.db.errors import ValidationError
from hokma.db.models import LicenseCheck, Profile
from hokma.db.repos.base import UNSET, Repo, flag, given, require_in
from hokma.db.timeutil import utcnow

_BOOL = ("telemetry_enabled",)
_ENUMS = {
    "language": ("es", "en"),
    "engine": ("claude_code", "codex", "gemini_cli", "kimi_code", "api"),
    "database_engine": ("sqlite", "postgres"),
    "claude_status": ("connected", "no_session", "no_cli", "unknown"),
    "theme": ("dark", "light"),
    "permission_mode": ("ask", "auto"),
}
_FIELDS = ("owner_name", "fund_name", "timezone", "language", "engine", "telemetry_enabled",
           "max_parallel_agents", "workdir", "database_engine", "claude_status", "claude_plan",
           "weekly_token_cap", "theme", "active_project_id", "no_active_project", "permission_mode",
           "permission_mode_intro_seen", "update_checked_at")


def _clean_name(value: Any, *, required: bool, field: str) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        if required:
            raise ValidationError(f"{field} is required")
        return None
    if len(text) > 60:
        raise ValidationError(f"{field} is at most 60 characters")
    return text


class ProfileRepo(Repo):
    def get(self) -> Profile | None:
        row = self.s.one("SELECT * FROM profile WHERE id = 1")
        return Profile.from_row({**row, "exists": True}) if row else None

    def get_or_default(self) -> Profile:
        return self.get() or Profile()

    def is_setup_complete(self) -> bool:
        profile = self.get()
        return bool(profile and profile.setup_completed_at)

    def save(self, *, owner_name: Any = UNSET, fund_name: Any = UNSET, timezone: Any = UNSET,
             language: Any = UNSET, engine: Any = UNSET, telemetry_enabled: Any = UNSET,
             max_parallel_agents: Any = UNSET, workdir: Any = UNSET, database_engine: Any = UNSET,
             claude_status: Any = UNSET, claude_plan: Any = UNSET, weekly_token_cap: Any = UNSET,
             theme: Any = UNSET, active_project_id: Any = UNSET, permission_mode: Any = UNSET,
             permission_mode_intro_seen: Any = UNSET, update_checked_at: Any = UNSET,
             no_active_project: Any = UNSET) -> Profile:
        values = given(owner_name=owner_name, fund_name=fund_name, timezone=timezone, language=language,
                       engine=engine, telemetry_enabled=telemetry_enabled, max_parallel_agents=max_parallel_agents,
                       workdir=workdir, database_engine=database_engine, claude_status=claude_status,
                       claude_plan=claude_plan, weekly_token_cap=weekly_token_cap, theme=theme,
                       active_project_id=active_project_id, permission_mode=permission_mode,
                       permission_mode_intro_seen=permission_mode_intro_seen, update_checked_at=update_checked_at,
                       no_active_project=no_active_project)
        for name, allowed in _ENUMS.items():
            if name in values:
                require_in(name, values[name], allowed)
        for name in ("max_parallel_agents", "weekly_token_cap"):
            if values.get(name) is not None:
                if isinstance(values[name], bool) or not isinstance(values[name], int) or values[name] < 1:
                    raise ValidationError(f"{name} must be a positive integer or null")
        if "owner_name" in values:
            values["owner_name"] = _clean_name(values["owner_name"], required=True, field="owner_name")
        if "fund_name" in values:
            values["fund_name"] = _clean_name(values["fund_name"], required=False, field="fund_name")
        if "telemetry_enabled" in values:
            values["telemetry_enabled"] = flag(values["telemetry_enabled"])
        if "no_active_project" in values:
            values["no_active_project"] = flag(values["no_active_project"])
        if "permission_mode_intro_seen" in values:
            values["permission_mode_intro_seen"] = flag(values["permission_mode_intro_seen"])
        if values.get("active_project_id") is not None:
            pid = values["active_project_id"]
            if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
                raise ValidationError("active_project_id must be a project id or null")
        if "timezone" in values and not values["timezone"]:
            raise ValidationError("timezone cannot be empty")

        existing = self.get()
        if existing is None:
            if "owner_name" not in values:
                raise ValidationError("owner_name is required to create the profile")
            self.s.insert_row("profile", {"id": 1, **values})
        elif values:
            self.s.update("profile", {"id": 1}, {**values, "updated_at": utcnow()})
        saved = self.get()
        assert saved is not None
        return saved

    def set_owner(self, owner_name: str, fund_name: str | None) -> Profile:
        return self.save(owner_name=owner_name, fund_name=fund_name)

    def set_active_project(self, project_id: int | None) -> Profile:
        """The project the chat uses when a conversation names none (null clears it)."""
        if self.get() is None:
            raise ValidationError("the profile does not exist yet")
        return self.save(active_project_id=project_id, no_active_project=False)

    def set_no_active_project(self) -> Profile:
        """"General": no active project at all. The next activation (or null project id with the flag off) ends it."""
        if self.get() is None:
            raise ValidationError("the profile does not exist yet")
        return self.save(active_project_id=None, no_active_project=True)

    def set_permission_mode(self, mode: str, *, intro_seen: bool | None = None) -> Profile:
        """'ask' or 'auto'. Only the owner's endpoint and the first-run step call this; never an agent."""
        require_in("permission_mode", mode, _ENUMS["permission_mode"])
        if self.get() is None:
            raise ValidationError("the profile does not exist yet")
        values: dict[str, Any] = {"permission_mode": mode}
        if intro_seen is not None:
            values["permission_mode_intro_seen"] = flag(intro_seen)
        return self.save(**values)

    def set_claude_status(self, status: str, plan: str | None = None) -> Profile:
        require_in("claude_status", status, _ENUMS["claude_status"])
        if self.get() is None:
            raise ValidationError("the profile does not exist yet")
        self.s.update("profile", {"id": 1}, {"claude_status": status, "claude_plan": plan,
                                              "claude_checked_at": utcnow(), "updated_at": utcnow()})
        return self.get()  # type: ignore[return-value]

    def complete_setup(self) -> Profile:
        if self.get() is None:
            raise ValidationError("the profile does not exist yet")
        self.s.execute("UPDATE profile SET setup_completed_at = COALESCE(setup_completed_at, :now), "
                       "updated_at = :now WHERE id = 1", {"now": utcnow()})
        return self.get()  # type: ignore[return-value]


class LicenseChecksRepo(Repo):
    def record(self, result: str, *, latest_version: str | None = None, detail: str | None = None) -> LicenseCheck:
        require_in("result", result, ("valid", "revoked", "expired", "offline", "error"))
        if detail is not None:
            detail = detail[:500]
        new_id = self.s.insert("license_checks", {"result": result, "latest_version": latest_version,
                                                  "detail": detail, "checked_at": utcnow()})
        return self._get("license_checks", "id", new_id, LicenseCheck)

    def latest(self) -> LicenseCheck | None:
        row = self.s.one("SELECT * FROM license_checks ORDER BY id DESC LIMIT 1")
        return LicenseCheck.from_row(row) if row else None

    def list(self, limit: int = 50) -> list[LicenseCheck]:
        rows = self.s.query("SELECT * FROM license_checks ORDER BY id DESC LIMIT :n", {"n": limit})
        return [LicenseCheck.from_row(r) for r in rows]
