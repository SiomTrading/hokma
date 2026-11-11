"""ChatEngine: routes the panel's messages to conversations and their sessions."""
import asyncio
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from hokma.engine import attachments as att
from hokma.engine.memory import FileAgents, MemoryStore, WorkdirProjects
from hokma.engine.protocols import (DEFAULT_MODEL, GENERAL_PROJECT_ID, MODELS, AgentCatalog, ChatStore, ConversationRecord,
                                    ProjectRecord, ProjectResolver)
from hokma.engine.session import PERMISSION_TIMEOUT, STOP_GRACE, ChatSession, TurnError
from hokma.engines import catalog
from hokma.engines.base import EngineAdapter
from hokma.engines.registry import build_adapter, tier_of
from hokma.kit.launch import LaunchSpec, prepare_claude_launch
from hokma.permissions import modes

MAX_FRAME_CHARS = 2 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_ATTACHMENTS = 10
MAX_PROCESSES = 8
TITLE_WORDS, TITLE_CHARS = 8, 60
COMMANDS = {"modelo": "model", "model": "model", "agente": "agent", "agent": "agent",
            "proyecto": "project", "project": "project"}
_COMMAND = re.compile(r"^/([A-Za-z]+)(?:\s+(.*))?$", re.S)
_CLIENT_REF = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def parse_command(text: str) -> tuple[str, str | None] | None:
    """('model'|'agent'|'project', argument) for /modelo, /agente, /proyecto (and English names), else None."""
    match = _COMMAND.match(text.strip())
    if not match or match.group(1).lower() not in COMMANDS:
        return None
    arg = (match.group(2) or "").strip()
    return COMMANDS[match.group(1).lower()], arg or None


class Connection:
    """A panel socket. `send` never raises: a closed socket just stops receiving."""

    def __init__(self, send_text: Callable[[str], Awaitable[None]]) -> None:
        self._send_text = send_text
        self._lock = asyncio.Lock()
        self.closed = False

    async def send(self, event: dict[str, Any]) -> None:
        if self.closed:
            return
        async with self._lock:
            try:
                await self._send_text(json.dumps(event, ensure_ascii=False))
            except Exception:
                self.closed = True

    async def send_all(self, events: list[dict[str, Any]]) -> None:
        """Several events in a row with nothing from another task in between."""
        if self.closed:
            return
        async with self._lock:
            for event in events:
                try:
                    await self._send_text(json.dumps(event, ensure_ascii=False))
                except Exception:
                    self.closed = True
                    return


class ChatEngine:
    def __init__(self, store: ChatStore | None = None, projects: ProjectResolver | None = None,
                 agents: AgentCatalog | None = None, launch: Callable[[], LaunchSpec] = prepare_claude_launch,
                 claude_bin: str | None = None, session_token: str = "", permission_timeout: float = PERMISSION_TIMEOUT,
                 stop_grace: float = STOP_GRACE, max_processes: int = MAX_PROCESSES,
                 permission_mode: Callable[[], str] | None = None,
                 checkpoint: Callable[[str, str], Any] | None = None,
                 blocks: Callable[[], list[dict[str, Any]]] | None = None) -> None:
        self.store: ChatStore = store or MemoryStore()
        self.projects: ProjectResolver = projects or WorkdirProjects()
        self.agents: AgentCatalog = agents or FileAgents()
        self.launch, self.claude_bin, self.session_token = launch, claude_bin, session_token
        self.permission_timeout, self.stop_grace, self.max_processes = permission_timeout, stop_grace, max_processes
        self.sessions: dict[str, ChatSession] = {}
        self.subscribers: dict[str, set[Connection]] = {}
        self.permission_mode: Callable[[], str] = permission_mode or modes.current_mode
        self._checkpoint, self._blocks = checkpoint, blocks
        self.adapters: dict[str, EngineAdapter] = {}

    def adapter_for(self, kind: str) -> EngineAdapter:
        adapter = self.adapters.get(kind)
        if adapter is None:
            adapter = self.adapters[kind] = build_adapter(kind, self)
        return adapter

    def resolve_engine(self, conv: ConversationRecord) -> tuple[EngineAdapter, dict[str, Any] | None, str]:
        """The adapter, the saved connection (None = Claude Code as before 0.0.9) and the concrete model."""
        getter = getattr(self.store, "connection_for", None)
        conn = getter(conv.id) if getter else None
        if conn is None:
            return self.adapter_for("claude_code"), None, conv.model
        if conn.get("status") == "removed":
            raise TurnError("connection_removed", "the connection of this conversation was removed; pick another")
        tier = tier_of(conv.model)
        model = catalog.tier_model(conn, tier) or ""
        return self.adapter_for(conn["engine"]), conn, model

    def history_for(self, conv: ConversationRecord) -> list[dict[str, str]]:
        """The last turns of the conversation, for an engine that starts a session with no memory of them."""
        try:
            rows = self.store.list_messages(conv.id)[-21:]
        except Exception:
            return []
        if rows and rows[-1].role == "user":
            rows = rows[:-1]                       # the message being sent now
        return [{"role": r.role, "text": r.text} for r in rows if r.text and r.role in ("user", "assistant")]

    @staticmethod
    def instructions_for(project_path: str, agent: str | None) -> str:
        """What every engine is told: identity, HOKMA.md with the names filled in, the student's instruction file and
        the persona of the active agent. Built here, not discovered by the engine in the folder."""
        from hokma.kit import instructions

        return instructions.build(project_path, agent)

    async def take_checkpoint(self, project_path: str, conversation_id: str) -> None:
        """Restore point before a turn (auto mode). A failure never stops the turn."""
        try:
            if self._checkpoint is not None:
                await asyncio.to_thread(self._checkpoint, project_path, conversation_id)
            else:
                from hokma.permissions import checkpoints

                await asyncio.to_thread(checkpoints.take, project_path, conversation_id)
        except Exception:
            pass

    async def drain_blocks(self) -> list[dict[str, Any]]:
        try:
            if self._blocks is not None:
                return self._blocks()
            from hokma.db import get_database
            from hokma.permissions import guard_log

            return await asyncio.to_thread(guard_log.drain, get_database())
        except Exception:
            return []

    # ------------------------------------------------------------ plumbing

    @staticmethod
    def make_title(text: str) -> str:
        words = text.split()
        title = " ".join(words[:TITLE_WORDS])
        return (title[:TITLE_CHARS].rstrip() + ("..." if len(title) > TITLE_CHARS or len(words) > TITLE_WORDS else ""))

    async def broadcast(self, conversation_id: str, event: dict[str, Any]) -> None:
        for conn in list(self.subscribers.get(conversation_id, ())):
            await conn.send(event)

    def subscribe(self, conn: Connection, conversation_id: str) -> None:
        self.subscribers.setdefault(conversation_id, set()).add(conn)

    def detach(self, conn: Connection) -> None:
        conn.closed = True
        for subs in self.subscribers.values():
            subs.discard(conn)

    def make_room(self, asking: ChatSession) -> None:
        """Free a process slot: close the idle session that has been quiet the longest."""
        live = [s for s in self.sessions.values() if s.has_process]
        if len(live) < self.max_processes:
            return
        idle = sorted((s for s in live if s.state == "idle" and s is not asking), key=lambda s: s.last_used)
        if not idle:
            raise TurnError("too_many_sessions", "too many conversations are running at the same time")
        asyncio.get_running_loop().create_task(idle[0]._drop_process())

    def _session(self, conversation_id: str) -> ChatSession:
        if conversation_id not in self.sessions:
            self.sessions[conversation_id] = ChatSession(self, conversation_id)
        return self.sessions[conversation_id]

    async def shutdown(self) -> None:
        await asyncio.gather(*(s.close() for s in self.sessions.values()), return_exceptions=True)

    # ------------------------------------------------------------- dispatch

    async def handle(self, conn: Connection, raw: str) -> None:
        if len(raw) > MAX_FRAME_CHARS:
            return await self._error(conn, "bad_message", "message too large")
        try:
            msg = json.loads(raw)
        except ValueError:
            return await self._error(conn, "bad_json", "the message is not valid JSON")
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            return await self._error(conn, "bad_message", "the message needs a string `type`")
        handler = {"send": self._on_send, "stop": self._on_stop, "permission_response": self._on_permission,
                   "command": self._on_command, "open": self._on_open, "ping": self._on_ping}.get(msg["type"])
        if handler is None:
            return await self._error(conn, "bad_message", f"unknown type {msg['type'][:40]!r}")
        try:
            await handler(conn, msg)
        except Exception as exc:                               # a bug must not close the socket
            await self._error(conn, "engine_error", f"{type(exc).__name__}: {exc}", msg.get("conversation_id"),
                              self._ref(msg) if msg["type"] == "send" else None)

    @staticmethod
    def _ref(msg: dict) -> str | None:
        """The panel's `client_ref` of a message that creates a conversation; a malformed one is ignored."""
        ref = msg.get("client_ref")
        if msg.get("conversation_id") is None and isinstance(ref, str) and _CLIENT_REF.match(ref):
            return ref
        return None

    @staticmethod
    async def _error(conn: Connection, code: str, message: str, conversation_id: Any = None,
                     client_ref: str | None = None) -> None:
        event: dict[str, Any] = {"type": "error", "code": code, "message": message}
        if isinstance(conversation_id, str):
            event["conversation_id"] = conversation_id
        elif client_ref:
            event["client_ref"] = client_ref
        await conn.send(event)

    async def _on_ping(self, conn: Connection, msg: dict) -> None:
        await conn.send({"type": "pong"})

    def _existing(self, msg: dict) -> ConversationRecord | None:
        cid = msg.get("conversation_id")
        return self.store.get_conversation(cid) if isinstance(cid, str) else None

    async def _on_open(self, conn: Connection, msg: dict) -> None:
        conv = self._existing(msg)
        if conv is None:
            return await self._error(conn, "unknown_conversation", "no such conversation", msg.get("conversation_id"))
        self.subscribe(conn, conv.id)
        await conn.send({"type": "conversation", "conversation_id": conv.id, **self._conv_body(conv, False)})
        session = self.sessions.get(conv.id)
        # Built without awaiting, then sent as one block: a delta of the live turn cannot land between the saved
        # history and the partial answer, so the panel never shows a piece twice or loses one.
        history = {"type": "history", "conversation_id": conv.id, "messages": [
            self._history_row(m) for m in self.store.list_messages(conv.id)]}
        state: dict[str, Any] = {"type": "state", "conversation_id": conv.id,
                                 "state": session.state if session else "idle",
                                 "pending_permissions": [p.event() for p in session.pending.values()] if session else []}
        partial = session.snapshot() if session else None
        if partial is not None:
            state["partial"] = partial
        await conn.send_all([history, state])

    def _history_row(self, m) -> dict[str, Any]:
        row: dict[str, Any] = {"id": m.id, "role": m.role, "text": m.text, "status": m.status,
                               "tokens_in": m.tokens_in, "tokens_out": m.tokens_out, "created_at": m.created_at}
        tools = m.meta.get("tools")
        if tools:
            row["tools"] = tools
        if m.meta.get("blocks"):
            row["blocks"] = m.meta["blocks"]
        files = []
        for attachment_id in m.meta.get("attachments") or []:
            record = self.store.get_attachment(attachment_id) if isinstance(attachment_id, str) else None
            if record is not None:
                files.append({"id": record.id, "name": record.name, "mime": record.mime, "size": record.size,
                              "kind": "image" if att.is_image(record.mime) else "file"})
        if files:
            row["attachments"] = files
        return row

    async def _on_stop(self, conn: Connection, msg: dict) -> None:
        session = self.sessions.get(msg.get("conversation_id")) if isinstance(msg.get("conversation_id"), str) else None
        if session is not None:
            await session.stop()

    async def _on_permission(self, conn: Connection, msg: dict) -> None:
        session = self.sessions.get(msg.get("conversation_id")) if isinstance(msg.get("conversation_id"), str) else None
        decision = msg.get("decision")
        if decision not in ("allow", "deny") or not isinstance(msg.get("request_id"), str):
            return await self._error(conn, "bad_message", "needs request_id and decision allow|deny",
                                     msg.get("conversation_id"))
        # Only this handler passes by="human": it is reachable only through the authenticated panel socket.
        if session is None or not await session.resolve(msg["request_id"], decision, "human"):
            await self._error(conn, "unknown_request", "no such pending permission", msg.get("conversation_id"))

    # --------------------------------------------------------- conversations

    def _project_of(self, conv: ConversationRecord) -> ProjectRecord:
        """The project a turn of this conversation runs in. A 'fixed' conversation without a project (or whose
        project is gone) is general: the working folder. A 'follow' one (made before 0.0.7) keeps tracking the
        active project through the resolver's default."""
        if conv.project_mode == "fixed":
            if conv.project_id is None:
                return self.projects.general()
            found = self.projects.get(conv.project_id)
            return found if found is not None and found.id == conv.project_id else self.projects.general()
        return self.projects.get(conv.project_id)

    def _conv_body(self, conv: ConversationRecord, created: bool) -> dict[str, Any]:
        project = self._project_of(conv)
        general = project is None or project.id == GENERAL_PROJECT_ID
        return {"title": conv.title, "model": conv.model, "agent": conv.agent, "created": created,
                "project_mode": conv.project_mode,
                "project": None if general else {"id": project.id, "name": project.name}}

    async def _conversation_for(self, conn: Connection, msg: dict) -> tuple[ConversationRecord, bool] | None:
        """Existing or new conversation with the optional model, agent and project_id of the message applied."""
        cid = msg.get("conversation_id")
        ref = self._ref(msg)
        conv = self._existing(msg)
        if cid is not None and conv is None:
            await self._error(conn, "unknown_conversation", "no such conversation", cid)
            return None
        model = msg.get("model")
        if model is not None and model not in MODELS:
            await self._error(conn, "unknown_model", f"model must be one of {', '.join(MODELS)}", cid, ref)
            return None
        project_id = msg.get("project_id")
        if project_id is not None:
            found = self.projects.get(project_id) if isinstance(project_id, str) else None
            if found is None or found.id != project_id:
                await self._error(conn, "unknown_project", "no such project", cid, ref)
                return None
        agent = msg.get("agent") if "agent" in msg else conv.agent if conv else None
        if agent is not None:
            # a new conversation without project_id is born general; an existing one keeps its own project
            project = (self.projects.get(project_id) if project_id else
                       self._project_of(conv) if conv else self.projects.general())
            if not isinstance(agent, str) or project is None or not self.agents.exists(agent, project):
                await self._error(conn, "unknown_agent", "no such agent", cid, ref)
                return None
        if conv is None:
            conv = self.store.create_conversation("", model or DEFAULT_MODEL, agent, project_id)
            created = True
        else:
            created = False
            changes = {k: v for k, v in (("model", model), ("project_id", project_id)) if v is not None}
            if "agent" in msg:
                changes["agent"] = agent
            if changes:
                self.store.update_conversation(conv.id, **changes)
                conv = self.store.get_conversation(conv.id)
        self.subscribe(conn, conv.id)
        return conv, created

    # ------------------------------------------------------------------ send

    async def _on_send(self, conn: Connection, msg: dict) -> None:
        text, ids = msg.get("text", ""), msg.get("attachments") or []
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS or not isinstance(ids, list) \
                or len(ids) > MAX_ATTACHMENTS or not all(isinstance(i, str) for i in ids):
            return await self._error(conn, "bad_message", "invalid text or attachments", msg.get("conversation_id"),
                                     self._ref(msg))
        command = parse_command(text)
        if command and not ids:
            return await self._run_command(conn, msg, *command)
        if not text.strip() and not ids:
            return await self._error(conn, "bad_message", "empty message", msg.get("conversation_id"), self._ref(msg))
        resolved = await self._conversation_for(conn, msg)
        if resolved is None:
            return
        conv, created = resolved
        session = self._session(conv.id)
        if session.state == "running":
            return await self._error(conn, "busy", "a turn is already running", conv.id)
        if not conv.title and text.strip():
            self.store.update_conversation(conv.id, title=self.make_title(text))
            conv = self.store.get_conversation(conv.id)
        project = self._project_of(conv)
        if project is None:
            return await self._error(conn, "unknown_project", "no such project", conv.id)
        built = self._build_content(text, ids, project, self._inline_images(conv))
        if isinstance(built, str):
            return await self._error(conn, "attachment_not_found", f"unknown attachment {built}", conv.id)
        blocks, meta = built
        event = {"type": "conversation", "conversation_id": conv.id, **self._conv_body(conv, created)}
        if created and self._ref(msg):
            event["client_ref"] = self._ref(msg)
        await conn.send(event)
        session.start_turn(conv, project, blocks, text, meta)

    def _inline_images(self, conv: ConversationRecord) -> bool:
        """True when the engine of this conversation takes image blocks. An engine that does not (Codex, Gemini)
        would drop them without a word, so its images go into the project as files it can open with its tools."""
        try:
            adapter = self.resolve_engine(conv)[0]
            return bool(adapter.caps().get("images", True))
        except Exception:
            return True                          # the turn reports the engine problem itself

    def _build_content(self, text: str, ids: list[str], project: ProjectRecord, inline_images: bool = True):
        """(content blocks, message meta), or the id of the attachment that does not exist."""
        blocks: list[dict[str, Any]] = []
        files: list[str] = []
        meta: dict[str, Any] = {"attachments": ids} if ids else {}
        for attachment_id in ids:
            record = self.store.get_attachment(attachment_id)
            path = att.attachments_dir() / record.sha256 if record else None
            if record is None or path is None or not path.is_file():
                return attachment_id
            data = path.read_bytes()
            if inline_images and att.is_image(record.mime) and record.size <= att.MAX_INLINE_IMAGE_BYTES:
                blocks.append(att.image_block(record, data))
            else:
                files.append(att.copy_into_project(record, data, Path(project.path)))
        body = text
        if files:
            body += ("\n\n" if body else "") + "Attached files (relative to the project folder):\n" + \
                "\n".join(f"- {f}" for f in files)
        blocks.append({"type": "text", "text": body})
        return blocks, meta

    # -------------------------------------------------------------- commands

    async def _on_command(self, conn: Connection, msg: dict) -> None:
        name, arg = msg.get("name"), msg.get("arg")
        if name not in ("model", "agent", "project") or not (arg is None or isinstance(arg, str)):
            return await self._error(conn, "bad_message", "name must be model, agent or project",
                                     msg.get("conversation_id"))
        await self._run_command(conn, msg, name, (arg or "").strip() or None)

    async def _run_command(self, conn: Connection, msg: dict, name: str, arg: str | None) -> None:
        cid = msg.get("conversation_id")
        conv = self._existing(msg)
        if cid is not None and conv is None:
            return await self._error(conn, "unknown_conversation", "no such conversation", cid)
        session = self.sessions.get(conv.id) if conv else None
        if name == "project" and arg and session is not None and session.state == "running":
            return await self._error(conn, "busy", "the project cannot change while a turn runs", conv.id)
        if arg is None:                                        # read-only: report the current value
            if conv is None:
                return await conn.send({"type": "command_result", "name": name, "ok": True, "value": None,
                                        **({"options": list(MODELS)} if name == "model" else {})})
            body = self._conv_body(conv, False)
            value = {"model": conv.model, "agent": conv.agent, "project": body["project"]}[name]
            self.subscribe(conn, conv.id)
            return await conn.send({"type": "command_result", "conversation_id": conv.id, "name": name, "ok": True,
                                    "value": value, **({"options": list(MODELS)} if name == "model" else {})})
        if name == "project":
            target = self.projects.find_by_name(arg)
            if target is None:
                return await self._error(conn, "unknown_project", "no such project", cid)
        update = {"model": {"model": arg.lower()},
                  "agent": {"agent": None if arg.lower() in ("none", "default") else arg},
                  "project": {"project_id": target.id if name == "project" else None}}[name]
        resolved = await self._conversation_for(conn, {"conversation_id": cid, **update})
        if resolved is None:
            return
        conv, created = resolved
        body = self._conv_body(conv, created)
        await conn.send({"type": "conversation", "conversation_id": conv.id, **body})
        value = {"model": conv.model, "agent": conv.agent, "project": body["project"]}[name]
        await conn.send({"type": "command_result", "conversation_id": conv.id, "name": name, "ok": True,
                         "value": value})

