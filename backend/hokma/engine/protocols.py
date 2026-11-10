"""What the engine needs from persistence and from the project list. Satisfied by the SQLite repositories (D1)
and the projects module (D6); `hokma.engine.memory` implements them in RAM for tests and for first boot."""
from dataclasses import dataclass, field
from typing import Any, Protocol

MODELS = ("haiku", "sonnet", "opus")
DEFAULT_MODEL = "sonnet"
GENERAL_PROJECT_ID = "general"   # id of the record that stands for "no project": the working folder


@dataclass
class ConversationRecord:
    id: str
    title: str
    model: str = DEFAULT_MODEL
    agent: str | None = None
    project_id: str | None = None
    engine_session: str | None = None
    project_mode: str = "fixed"     # "follow": no project_id means the active project (before 0.0.7); "fixed": it means general


@dataclass
class MessageRecord:
    id: str
    conversation_id: str
    role: str                       # "user" | "assistant"
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    status: str = "complete"        # "complete" | "stopped" | "error"
    created_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttachmentRecord:
    id: str
    sha256: str
    name: str
    mime: str
    size: int


@dataclass
class ProjectRecord:
    id: str
    name: str
    path: str


class ChatStore(Protocol):
    def create_conversation(self, title: str, model: str, agent: str | None,
                            project_id: str | None) -> ConversationRecord: ...
    def get_conversation(self, conversation_id: str) -> ConversationRecord | None: ...
    def update_conversation(self, conversation_id: str, **fields: Any) -> None: ...
    def add_message(self, conversation_id: str, role: str, text: str, tokens_in: int = 0,
                    tokens_out: int = 0, status: str = "complete",
                    meta: dict[str, Any] | None = None) -> MessageRecord: ...
    def list_messages(self, conversation_id: str) -> list[MessageRecord]: ...
    def add_attachment(self, sha256: str, name: str, mime: str, size: int) -> AttachmentRecord: ...
    def get_attachment(self, attachment_id: str) -> AttachmentRecord | None: ...
    def record_permission(self, conversation_id: str, tool: str, summary: dict[str, str], decision: str,
                          decided_by_kind: str, channel: str) -> None: ...


class ProjectResolver(Protocol):
    def get(self, project_id: str | None) -> ProjectRecord | None:
        """The project with that id; None or unknown id gives the default (active) project."""

    def find_by_name(self, name: str) -> ProjectRecord | None: ...

    def general(self) -> ProjectRecord:
        """The record of a conversation without a project: the working folder. Its id is GENERAL_PROJECT_ID."""


class AgentCatalog(Protocol):
    def exists(self, slug: str, project: ProjectRecord) -> bool: ...
