"""Typed rows. Every repository returns these frozen dataclasses; `.to_dict()` is JSON-safe."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, ClassVar, Literal

ActorKind = Literal["human", "agent", "system"]


@dataclass(frozen=True, slots=True)
class Actor:
    """Who performs a change. The database triggers decide what each kind may do."""

    kind: ActorKind
    name: str

    @classmethod
    def human(cls, name: str = "owner") -> "Actor":
        return cls("human", name)

    @classmethod
    def agent(cls, slug: str) -> "Actor":
        return cls("agent", slug)

    @classmethod
    def system(cls, name: str = "hokma") -> "Actor":
        return cls("system", name)


def loads_json(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def dumps_json(value: Any) -> str | None:
    """Accept dict, list, str or None; strings are stored as given."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Record:
    """Mixin: build from a database row, export to a dict."""

    BOOLS: ClassVar[tuple[str, ...]] = ()
    JSONS: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def from_row(cls, row: dict[str, Any]):
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in row.items() if k in names}
        for key in cls.BOOLS:
            if key in data and data[key] is not None:
                data[key] = bool(data[key])
        for key in cls.JSONS:
            if key in data:
                data[key] = loads_json(data[key])
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[call-overload]


@dataclass(frozen=True, slots=True)
class Profile(Record):
    BOOLS = ("telemetry_enabled", "exists", "permission_mode_intro_seen", "no_active_project")
    exists: bool = False
    owner_name: str = ""
    fund_name: str | None = None
    timezone: str = "America/New_York"
    language: str = "es"
    engine: str = "claude_code"
    telemetry_enabled: bool = False
    max_parallel_agents: int | None = None
    workdir: str | None = None
    database_engine: str = "sqlite"
    claude_status: str = "unknown"
    claude_plan: str | None = None
    claude_checked_at: str | None = None
    weekly_token_cap: int | None = None
    theme: str = "dark"
    active_project_id: int | None = None
    no_active_project: bool = False
    permission_mode: str = "ask"
    permission_mode_intro_seen: bool = False
    update_checked_at: str | None = None
    setup_completed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class LicenseCheck(Record):
    id: int
    checked_at: str
    result: str
    latest_version: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Agent(Record):
    BOOLS = ("is_custom",)
    slug: str
    display_name: str
    description: str | None
    model_tier: str
    status: str
    enabled_session: int | None
    is_custom: bool
    file_path: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Project(Record):
    BOOLS = ("default_requires_review", "default_requires_approval")
    id: int
    slug: str
    name: str
    path: str
    kind: str
    status: str
    description: str | None
    default_requires_review: bool
    default_reviewer_agent: str | None
    default_requires_approval: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Conversation(Record):
    BOOLS = ("pinned", "archived")
    id: int
    engine: str
    engine_session: str | None
    title: str | None
    pinned: bool
    project_id: int | None
    model: str | None
    agent_slug: str | None
    started_at: str
    last_message_at: str | None
    message_count: int = 0
    archived: bool = False
    project_mode: str = "follow"
    connection_id: int | None = None


@dataclass(frozen=True, slots=True)
class Message(Record):
    JSONS = ("meta",)
    id: int
    conversation_id: int
    role: str
    agent_slug: str | None
    model: str | None
    content: str
    tokens_in: int
    tokens_out: int
    created_at: str
    status: str = "complete"
    meta: Any = None


@dataclass(frozen=True, slots=True)
class Attachment(Record):
    id: int
    message_id: int | None
    project_id: int | None
    kind: str
    stored_path: str
    mime_type: str
    size_bytes: int
    sha256: str
    transcript: str | None
    created_at: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Task(Record):
    BOOLS = ("requires_review", "requires_approval", "touches_real_money")
    JSONS = ("result_metadata",)
    id: int
    project_id: int | None
    title: str
    body: str | None
    status: str
    priority: int
    assigned_agent: str | None
    implementer_agent: str | None
    requires_review: bool
    reviewer_agent: str | None
    requires_approval: bool
    touches_real_money: bool
    created_by_kind: str
    created_by: str
    changed_by_kind: str
    changed_by: str
    change_reason: str | None
    claim_token: str | None
    claimed_by: str | None
    claimed_at: str | None
    claim_expires_at: str | None
    heartbeat_at: str | None
    max_runtime_seconds: int
    attempts: int
    max_attempts: int
    block_kind: str | None
    block_reason: str | None
    block_count: int
    block_limit: int
    review_cycles: int
    review_limit: int
    result_summary: str | None
    result_metadata: Any
    run_at: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    parent_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskRun(Record):
    JSONS = ("metadata",)
    id: int
    task_id: int
    agent_slug: str
    role: str
    model: str
    pid: int | None
    started_at: str
    heartbeat_at: str | None
    ended_at: str | None
    outcome: str
    summary: str | None
    metadata: Any
    error: str | None
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True, slots=True)
class TaskEvent(Record):
    id: int
    task_id: int
    from_status: str | None
    to_status: str
    actor_kind: str
    actor: str
    reason: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class TaskComment(Record):
    id: int
    task_id: int
    author_kind: str
    author: str
    body: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TaskDetail(Record):
    task: Task
    parents: list[Task] = field(default_factory=list)
    children: list[Task] = field(default_factory=list)
    runs: list[TaskRun] = field(default_factory=list)
    comments: list[TaskComment] = field(default_factory=list)
    events: list[TaskEvent] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Approval(Record):
    id: int
    subject_type: str
    subject_id: int
    verdict: str
    decided_by_kind: str
    decided_by: str
    channel: str
    note: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class Decision(Record):
    JSONS = ("options",)
    id: int
    project_id: int | None
    topic: str
    context: str | None
    options: Any
    proposal: str
    proposed_by: str
    status: str
    decided_by_kind: str | None
    decided_by: str | None
    approval_id: int | None
    superseded_by: int | None
    created_at: str
    decided_at: str | None


@dataclass(frozen=True, slots=True)
class JournalEntry(Record):
    JSONS = ("refs",)
    id: int
    kind: str
    outcome: str
    agent_slug: str | None
    actor_kind: str
    task_id: int | None
    project_id: int | None
    title: str
    body: str | None
    refs: Any
    created_at: str


@dataclass(frozen=True, slots=True)
class GuardEvent(Record):
    id: int
    guard: str
    decision: str
    agent_slug: str | None
    tool: str | None
    task_id: int | None
    reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TrashItem(Record):
    id: int
    original_path: str
    stored_path: str
    size_bytes: int | None
    project_id: int | None
    conversation_id: int | None
    reason: str | None
    moved_at: str
    restored_at: str | None = None
    purged_at: str | None = None


@dataclass(frozen=True, slots=True)
class Bug(Record):
    id: int
    project_id: int | None
    title: str
    description: str | None
    severity: str
    status: str
    found_by_kind: str
    found_by: str
    fixed_by_task_id: int | None
    changed_by_kind: str
    changed_by: str
    created_at: str
    updated_at: str
    closed_at: str | None


@dataclass(frozen=True, slots=True)
class Extension(Record):
    BOOLS = ("enabled", "is_preinstalled")
    id: int
    name: str
    marketplace: str
    kind: str
    version: str | None
    license: str | None
    enabled: bool
    is_preinstalled: bool
    approval_id: int | None
    installed_at: str


@dataclass(frozen=True, slots=True)
class ExtensionRequest(Record):
    id: int
    action: str
    kind: str
    name: str
    marketplace: str
    version: str | None
    license: str | None
    reason: str
    requested_by_kind: str
    requested_by: str
    task_id: int | None
    status: str
    approval_id: int | None
    extension_id: int | None
    error: str | None
    requested_at: str
    decided_at: str | None
    applied_at: str | None


@dataclass(frozen=True, slots=True)
class ScheduledJob(Record):
    BOOLS = ("is_active",)
    id: int
    name: str
    agent_slug: str
    prompt: str
    schedule_kind: str
    schedule_expr: str
    timezone: str
    is_active: bool
    approval_id: int | None
    last_run_at: str | None
    next_run_at: str | None
    last_task_id: int | None
    created_at: str


@dataclass(frozen=True, slots=True)
class Progress(Record):
    done: int
    total: int
    steps: dict[str, bool]


@dataclass(frozen=True, slots=True)
class UsageTotal(Record):
    tokens_in: int
    tokens_out: int
    tokens: int
    messages: int
    runs: int


@dataclass(frozen=True, slots=True)
class UsageRow(Record):
    key: str
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True, slots=True)
class WeekUsage(Record):
    used: int
    cap: int | None
    pct: float | None
    starts_at: str
    ends_at: str


@dataclass(frozen=True, slots=True)
class EngineConnection(Record):
    BOOLS = ("is_default",)
    JSONS = ("tier_models", "settings")
    id: int
    engine: str
    label: str
    auth_kind: str
    provider: str | None
    wire: str | None
    base_url: str | None
    tier_models: dict | None
    status: str
    status_detail: str | None
    is_default: bool
    settings: dict | None
    checked_at: str | None
    created_at: str
    updated_at: str
