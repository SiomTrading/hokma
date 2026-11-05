"""approvals: decisions of the owner. Human only, append-only (the table refuses anything else)."""
from __future__ import annotations

from hokma.db.errors import ValidationError
from hokma.db.models import Approval
from hokma.db.repos.base import Repo, require_in
from hokma.db.timeutil import utcnow

SUBJECTS = ("task", "decision", "strategy", "robot", "ips_change", "extension", "scheduled_job", "bug", "permission")
VERDICTS = ("approved", "rejected", "changes_requested")
CHANNELS = ("dashboard", "telegram", "cli")


class ApprovalsRepo(Repo):
    def record(self, subject_type: str, subject_id: int, verdict: str, *, decided_by: str,
               channel: str = "dashboard", note: str | None = None) -> Approval:
        require_in("subject_type", subject_type, SUBJECTS)
        require_in("verdict", verdict, VERDICTS)
        require_in("channel", channel, CHANNELS)
        if not decided_by or not str(decided_by).strip():
            raise ValidationError("decided_by is required")
        new_id = self.s.insert("approvals", {
            "subject_type": subject_type, "subject_id": subject_id, "verdict": verdict, "decided_by_kind": "human",
            "decided_by": decided_by, "channel": channel, "note": (note or None) and note[:1000],
            "created_at": utcnow()})
        return self.get(new_id)

    def record_permission(self, conversation_id: int, tool: str, verdict: str, *, decided_by: str = "owner",
                          channel: str = "dashboard", detail: str | None = None) -> Approval:
        """The owner's answer to a chat permission prompt (subject = the conversation; the note names the tool)."""
        require_in("verdict", verdict, ("approved", "rejected"))
        note = f"{tool}: {detail}" if detail else tool
        return self.record("permission", conversation_id, verdict, decided_by=decided_by, channel=channel,
                           note=note[:1000])

    def get(self, id: int) -> Approval:
        return self._get("approvals", "id", id, Approval)

    def list_for(self, subject_type: str, subject_id: int) -> list[Approval]:
        rows = self.s.query("SELECT * FROM approvals WHERE subject_type = :t AND subject_id = :i ORDER BY id",
                            {"t": subject_type, "i": subject_id})
        return [Approval.from_row(r) for r in rows]

    def latest_for(self, subject_type: str, subject_id: int) -> Approval | None:
        row = self.s.one("SELECT * FROM approvals WHERE subject_type = :t AND subject_id = :i ORDER BY id DESC LIMIT 1",
                         {"t": subject_type, "i": subject_id})
        return Approval.from_row(row) if row else None
