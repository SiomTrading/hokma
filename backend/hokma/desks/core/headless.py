"""Unattended desk runs (SPEC-017: OA-69).

The scheduler starts a desk turn with no screen open. This module runs it to the end and always leaves a record: who ran,
for which desk, when it started and ended, whether it finished, and why not when it did not. Nothing is thrown at the
scheduler: a failed run is a record with `status="failed"`, a stable `code` and a notice for the owner.

- No engine connected (or none that can run): the run fails at once with code `no_engine` and a notice, without calling a model.
- A record left as `running` by a process that died is marked `interrupted` the next time Hokma starts (`reconcile`).
- What is stored of the prompt and the reply is cut and scrubbed of credentials: the record is a log, not an archive.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from hokma.desks.core.paths import runs_dir
from hokma.desks.core.safety import scrub_text
from hokma.llm.protocol import LLMClient, LLMError

REGISTRY_FILE = "headless-runs.json"
MAX_RECORDS = 500
PREVIEW = 500
ERROR_TAIL = 2000
STATUSES = ("running", "done", "failed", "interrupted")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class NoEngine(Exception):
    """No engine can answer: nothing is connected, or what is connected cannot run."""

    code = "no_engine"


@dataclass
class RunRecord:
    run_id: str
    desk_id: int
    agent: str
    trigger: str
    status: str
    started_at: str
    ended_at: str | None = None
    code: str = ""
    error: str = ""
    prompt_preview: str = ""
    reply_preview: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    notice_sent: bool = False


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def clean_text(text: str, limit: int) -> str:
    """Control characters and ANSI escapes removed, credentials scrubbed, the last `limit` characters kept."""
    text = _ANSI.sub("", str(text))
    text = "".join(c for c in text if c in "\n\t" or not unicodedata.category(c).startswith("C"))
    return scrub_text(text).strip()[-limit:]


def failure_summary(code: str, message: str, *, killed: bool = False) -> str:
    """One readable line of why a run failed (never empty)."""
    if killed:
        return "The run was stopped by the time limit."
    text = clean_text(message, ERROR_TAIL)
    return text or f"The run failed without a message (code {code or 'unknown'})."


class RunRegistry:
    """Records of unattended runs in one JSON file, written atomically. The oldest beyond `MAX_RECORDS` are dropped."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else runs_dir() / REGISTRY_FILE
        self._lock = threading.RLock()
        self._counter = 0

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = data.get("runs") if isinstance(data, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def _save(self, rows: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".runs.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as tmp:
                json.dump({"version": 1, "runs": rows[-MAX_RECORDS:]}, tmp, indent=1)
            os.replace(tmp_name, self._path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _new_id(self) -> str:
        self._counter += 1
        return f"run-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{os.getpid()}-{self._counter}"

    def start(self, desk_id: int, agent: str, trigger: str, prompt: str, *, started_at: str | None = None) -> RunRecord:
        with self._lock:
            record = RunRecord(self._new_id(), desk_id, agent, trigger, "running", started_at or _now(),
                               prompt_preview=clean_text(prompt, PREVIEW))
            rows = self._load()
            rows.append(asdict(record))
            self._save(rows)
            return record

    def update(self, record: RunRecord) -> RunRecord:
        with self._lock:
            rows = self._load()
            for index, row in enumerate(rows):
                if row.get("run_id") == record.run_id:
                    rows[index] = asdict(record)
                    break
            else:
                rows.append(asdict(record))
            self._save(rows)
            return record

    def get(self, run_id: str) -> RunRecord | None:
        return next((RunRecord(**r) for r in self._load() if r.get("run_id") == run_id), None)

    def list(self, *, desk_id: int | None = None, status: str | None = None) -> list[RunRecord]:
        """Records, oldest first."""
        out = [RunRecord(**r) for r in self._load()]
        return [r for r in out if (desk_id is None or r.desk_id == desk_id) and (status is None or r.status == status)]

    def reconcile(self, *, now: str | None = None) -> int:
        """Mark every run left as `running` as `interrupted` (the process that ran it is gone). Returns how many."""
        with self._lock:
            rows, changed = self._load(), 0
            for row in rows:
                if row.get("status") == "running":
                    row.update(status="interrupted", ended_at=now or _now(), code="interrupted",
                               error="Hokma stopped while this run was in progress.")
                    changed += 1
            if changed:
                self._save(rows)
            return changed


def default_engine_check() -> None:
    """Raises NoEngine when no connected engine can run (the rule the chat uses)."""
    from hokma.engine.dbstore import DbChatStore
    from hokma.db import get_database
    from hokma.engines import effective

    store = DbChatStore()
    with get_database().read() as u:
        default = u.engine_connections.default()
    if effective.is_stuck(store._conn(default) if default else None, store.connections()):
        raise NoEngine("no engine is connected that can run")


class HeadlessRunner:
    """Runs one desk turn without a screen and records it."""

    def __init__(self, registry: RunRegistry | None = None, *, llm_factory: Callable[[str, "int | None"], LLMClient] | None = None,
                 engine_check: Callable[[], None] | None = None, notify: Callable[[str], None] | None = None,
                 clock: Callable[[], str] = _now, max_tokens: int = 4096) -> None:
        self.registry = registry or RunRegistry()
        self._llm_factory = llm_factory
        self._engine_check = engine_check or default_engine_check
        self._notify = notify
        self._clock = clock
        self.max_tokens = max_tokens

    def _make(self, agent: str, desk_id: int | None) -> LLMClient:
        if self._llm_factory is not None:
            return self._llm_factory(agent, desk_id)
        from hokma.llm.factory import make_llm

        return make_llm(agent, desk_id=desk_id)

    def _fail(self, record: RunRecord, code: str, message: str) -> RunRecord:
        record.status, record.code = "failed", code
        record.error = failure_summary(code, message)
        record.ended_at = self._clock()
        if self._notify is not None:
            try:
                self._notify(f"Desk {record.desk_id}, run {record.run_id}: {record.error}")
                record.notice_sent = True
            except Exception:                                    # noqa: BLE001  (a broken notifier must not hide the record)
                record.notice_sent = False
        return self.registry.update(record)

    def run(self, desk_id: int, prompt: str, *, agent: str = "desk-chief", trigger: str = "scheduler") -> RunRecord:
        """Run `prompt` as `agent` of the desk and return the record. Never raises for a failed run."""
        record = self.registry.start(desk_id, agent, trigger, prompt, started_at=self._clock())
        try:
            self._engine_check()
            llm = self._make(agent, desk_id)
        except NoEngine as exc:
            return self._fail(record, NoEngine.code, f"No engine is configured. Connect one in Settings. {exc}")
        except Exception as exc:                                  # noqa: BLE001
            return self._fail(record, str(getattr(exc, "code", "") or "no_engine"), f"The engine could not start: {exc}")
        record.model = str(getattr(llm, "model", "") or "")
        self.registry.update(record)
        try:
            reply = llm.complete([{"role": "user", "content": prompt}], max_tokens=self.max_tokens)
        except LLMError as exc:
            return self._fail(record, exc.code, exc.message)
        except Exception as exc:                                  # noqa: BLE001
            return self._fail(record, "engine_error", f"{type(exc).__name__}: {exc}")
        record.status, record.code, record.error = "done", "", ""
        record.reply_preview = clean_text(reply.text, PREVIEW)
        record.usage = dict(reply.usage or {})
        record.ended_at = self._clock()
        return self.registry.update(record)

    def run_scheduled(self, desk_id: int, prompt: str, *, agent: str = "desk-chief") -> RunRecord:
        """The entry point of the scheduler."""
        return self.run(desk_id, prompt, agent=agent, trigger="scheduler")
