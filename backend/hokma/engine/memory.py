"""In-RAM implementations of the engine protocols, plus the default project and agent lookups."""
import itertools
import re
import time
from pathlib import Path
from typing import Any

from hokma.core.config import claude_config_dir, hokma_home
from hokma.engine.protocols import AttachmentRecord, ConversationRecord, MessageRecord, ProjectRecord

SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class MemoryStore:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.conversations: dict[str, ConversationRecord] = {}
        self.messages: dict[str, list[MessageRecord]] = {}
        self.attachments: dict[str, AttachmentRecord] = {}
        self.transcripts: dict[str, str] = {}
        self.permissions: list[dict[str, Any]] = []

    def _id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    def create_conversation(self, title, model, agent, project_id):
        rec = ConversationRecord(self._id("c"), title, model, agent, project_id)
        self.conversations[rec.id] = rec
        self.messages[rec.id] = []
        return rec

    def get_conversation(self, conversation_id):
        return self.conversations.get(conversation_id)

    def update_conversation(self, conversation_id, **fields):
        rec = self.conversations[conversation_id]
        for key, value in fields.items():
            if not hasattr(rec, key) or key == "id":
                raise KeyError(key)
            setattr(rec, key, value)

    def add_message(self, conversation_id, role, text, tokens_in=0, tokens_out=0, status="complete", meta=None):
        rec = MessageRecord(self._id("m"), conversation_id, role, text, tokens_in, tokens_out, status,
                            time.time(), dict(meta or {}))
        self.messages[conversation_id].append(rec)
        return rec

    def list_messages(self, conversation_id):
        return list(self.messages.get(conversation_id, []))

    def add_attachment(self, sha256, name, mime, size):
        rec = AttachmentRecord(self._id("a"), sha256, name, mime, size)
        self.attachments[rec.id] = rec
        return rec

    def set_transcript(self, attachment_id, text):
        self.transcripts[attachment_id] = text

    def get_attachment(self, attachment_id):
        return self.attachments.get(attachment_id)

    def record_permission(self, conversation_id, tool, summary, decision, decided_by_kind, channel):
        self.permissions.append({"conversation_id": conversation_id, "tool": tool, "summary": summary,
                                 "decision": decision, "decided_by_kind": decided_by_kind, "channel": channel})


class WorkdirProjects:
    """Default project list: the working folder is the default project; a project named X is the folder X
    directly inside it (never a path: a name with separators or a leading dot is refused)."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root or hokma_home()

    def get(self, project_id):
        if project_id:
            found = self.find_by_name(project_id)
            if found:
                return found
        return ProjectRecord("default", self.root.name or "Hokma", str(self.root))

    def find_by_name(self, name):
        if not name or not SLUG.match(name):
            return None
        path = self.root / name
        if path.is_dir() and not path.is_symlink():
            return ProjectRecord(name, name, str(path))
        return None


class FileAgents:
    """An agent exists when `<slug>.md` is in an `agents` folder of the project, the working folder or the
    Claude config (the CLI silently ignores an unknown --agent, so the engine checks it itself)."""

    def exists(self, slug: str, project: ProjectRecord) -> bool:
        if not SLUG.match(slug):
            return False
        roots = [Path(project.path) / ".claude" / "agents", hokma_home() / ".claude" / "agents",
                 claude_config_dir() / "agents"]
        return any((r / f"{slug}.md").is_file() for r in roots)
