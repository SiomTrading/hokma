"""Telegram glue (SPEC-011 P7): the owner's text reaches the chat engine, a button answers a permission and leaves
`approvals.channel = 'telegram'`, a finished routine reaches the phone, and without a link nothing happens.
Telegram is a fake; the engine is a fake with the same public surface (store, subscribe, handle, sessions)."""
import asyncio
import functools
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.connections.store import KeyStore
from hokma.db import get_database, migrate
from hokma.engine.dbstore import DbChatStore
from hokma.telegram.api import create_router
from hokma.telegram.glue import TOKEN_ENTRY, TelegramGlue
from permissions_support import HEADERS

def aio(fn):
    """Run an async test to the end (the suite has no async plugin)."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


TOKEN = "123456789:" + "A" * 35


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(message_id=len(self.sent))


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()
        self.running = False
        self.updater = SimpleNamespace(running=False, start_polling=self._poll, stop=self._stop_poll)

    async def _poll(self, **kw):
        self.updater.running = True

    async def _stop_poll(self):
        self.updater.running = False

    def add_handler(self, handler, group=0):
        pass

    async def initialize(self):
        pass

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False

    async def shutdown(self):
        pass


class MemoryKeys(KeyStore):
    def __init__(self):
        self.items = {}

    def available(self):
        return True

    def get(self, provider_id):
        return self.items.get(provider_id)

    def set(self, provider_id, value):
        self.items[provider_id] = value

    def delete(self, provider_id):
        self.items.pop(provider_id, None)


class FakeSession:
    """The part of ChatSession the glue uses: `pending` and `resolve` (False once the request ended)."""

    def __init__(self, tool, target, request_id):
        self.pending = {request_id: SimpleNamespace(tool=tool, summary={"target": target})}
        self.resolved = []

    async def resolve(self, request_id, decision, by):
        if request_id not in self.pending:
            return False
        self.resolved.append((request_id, decision, by))
        del self.pending[request_id]
        return True


class FakeEngine:
    """Public surface of ChatEngine that the glue uses; `handle` records and plays scripted events."""

    def __init__(self):
        self.store = DbChatStore()
        self.sessions = {}
        self.handled = []
        self.conn = None
        self.script = []

    def subscribe(self, conn, conversation_id):
        self.conn = conn

    async def handle(self, conn, raw):
        msg = json.loads(raw)
        self.handled.append(msg)
        if msg["type"] == "send":
            for event in self.script:
                await conn.send(event)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOKMA_LANG", "en")
    (tmp_path / "data").mkdir()
    migrate()
    db, app, engine = get_database(), FakeApp(), FakeEngine()

    async def factory(token):
        return app

    glue = TelegramGlue(lambda: db, lambda: engine, keys=MemoryKeys(), application_factory=factory,
                        data_dir=tmp_path / "pairing")
    return SimpleNamespace(db=db, app=app, engine=engine, glue=glue)


async def linked(env, chat_id="111"):
    """Connect and link `chat_id` the way the student does: token, a message from the chat, the code in Hokma."""
    env.glue.keys.set(TOKEN_ENTRY, TOKEN)
    assert await env.glue.start()
    env.glue.store.save({"chat_id": chat_id, "user_name": "Ana"})


def text_update(chat_id, text):
    chat = SimpleNamespace(id=int(chat_id), type="private")
    msg = SimpleNamespace(text=text, chat=chat)
    return SimpleNamespace(effective_message=msg, effective_chat=chat, message=msg,
                           effective_user=SimpleNamespace(id=int(chat_id), first_name="Ana"))


def tap(data, chat_id):
    answers = []

    async def answer(text=None):
        answers.append(text)

    async def edit_message_text(**kw):
        pass

    query = SimpleNamespace(data=data, answer=answer, edit_message_text=edit_message_text,
                            from_user=SimpleNamespace(id=int(chat_id), first_name="Ana"),
                            message=SimpleNamespace(chat_id=int(chat_id), chat=SimpleNamespace(type="private"),
                                                    message_thread_id=None))
    return SimpleNamespace(callback_query=query), answers


def button(bot, position):
    return bot.sent[-1]["reply_markup"].inline_keyboard[0][position].callback_data


def approvals(db):
    with db.read() as u:
        return u.session.query("SELECT subject_type, verdict, channel, decided_by_kind FROM approvals")


@aio
async def test_without_a_link_nothing_happens(env):
    assert await env.glue.start() is False and not env.glue.link.connected
    env.glue.notify_job(SimpleNamespace(name="Daily"), SimpleNamespace(status="done", detail=""))
    assert env.app.bot.sent == [] and env.engine.handled == []


@aio
async def test_owner_text_reaches_the_engine_and_the_answer_returns(env):
    await linked(env)
    env.engine.script = [{"type": "delta", "turn_id": "t1", "text": "Hola "},
                         {"type": "delta", "turn_id": "t1", "text": "Ana"},
                         {"type": "done", "turn_id": "t1", "stopped": False}]
    await env.glue.link._handle_text_message(text_update(111, "dame el resumen"))
    send = env.engine.handled[0]
    assert send["type"] == "send" and send["text"] == "dame el resumen"
    assert env.app.bot.sent[-1]["text"] == "Hola Ana"
    with env.db.read() as u:                        # a fixed conversation per chat, saved as the private session
        assert u.session.one("SELECT conversation_id FROM telegram_session WHERE thread_id = 0")["conversation_id"] == send["conversation_id"]
    await env.glue.link._handle_text_message(text_update(111, "otra"))
    assert env.engine.handled[1]["conversation_id"] == send["conversation_id"]


@aio
async def test_text_from_another_chat_is_ignored(env):
    await linked(env)
    await env.glue.link._handle_text_message(text_update(999, "orden ajena"))
    assert env.engine.handled == [] and env.app.bot.sent == []


@aio
async def test_permission_arrives_with_buttons_and_a_tap_leaves_the_telegram_channel(env):
    await linked(env)
    env.engine.script = [{"type": "permission_request", "turn_id": "t1", "request_id": "r1", "tool": "Bash",
                          "input": {"command": "ls -la"}, "summary": {"kind": "run", "target": "ls -la"}}]
    await env.glue.link._handle_text_message(text_update(111, "lista"))
    prompt = env.app.bot.sent[-1]
    assert "ls -la" in prompt["text"] and len(prompt["reply_markup"].inline_keyboard[0]) == 2   # allow and deny only
    cid = env.engine.handled[0]["conversation_id"]
    session = env.engine.sessions[cid] = FakeSession("Bash", "ls -la", "r1")
    update, answers = tap(button(env.app.bot, 0), 111)
    await env.glue.link._ui.handle_callback_query(update)
    assert session.resolved == [("r1", "allow", "human")]
    assert approvals(env.db) == [{"subject_type": "permission", "verdict": "approved", "channel": "telegram",
                                  "decided_by_kind": "human"}]


@aio
async def test_a_command_too_long_to_show_is_denied_not_offered(env):
    await linked(env)
    cid = env.glue._conversation(0, None)[0]
    session = env.engine.sessions[cid] = FakeSession("Bash", "echo", "r9")
    env.engine.script = [{"type": "permission_request", "conversation_id": cid, "turn_id": "t1", "request_id": "r9",
                          "tool": "Bash", "input": {"command": "echo " + "x" * 5000}, "summary": {"target": "echo"}}]
    await env.glue.link._handle_text_message(text_update(111, "corre"))
    assert session.resolved == [("r9", "deny", "system")]
    assert "reply_markup" not in env.app.bot.sent[-1] and "too long" in env.app.bot.sent[-1]["text"]


@aio
async def test_deny_is_recorded_and_a_late_tap_says_expired(env):
    await linked(env)
    await env.glue.link.send_exec_approval("rm x", "r2", "Bash")
    cid = env.glue._conversation(0, None)[0]
    session = env.engine.sessions[cid] = FakeSession("Bash", "rm x", "r2")
    update, _ = tap(button(env.app.bot, 1), 111)
    await env.glue.link._ui.handle_callback_query(update)
    assert session.resolved == [("r2", "deny", "human")] and approvals(env.db)[0]["verdict"] == "rejected"
    # the permission already ended (the engine no longer holds it): a new prompt's tap resolves nothing
    await env.glue.link.send_exec_approval("rm y", "r3", "Bash")
    late, answers = tap(button(env.app.bot, 0), 111)
    await env.glue.link._ui.handle_callback_query(late)
    assert answers == ["⌛ Expired"] and len(approvals(env.db)) == 1


@aio
async def test_finished_and_failed_routines_notify_and_skipped_stays_silent(env):
    await linked(env)
    job = SimpleNamespace(name="Daily report")
    env.glue.notify_job(job, SimpleNamespace(status="done", detail="ok"))
    env.glue.notify_job(job, SimpleNamespace(status="failed", detail="timeout"))
    env.glue.notify_job(job, SimpleNamespace(status="skipped", detail="agent_off"))
    for task in list(env.glue._tasks):
        await task
    texts = [m["text"] for m in env.app.bot.sent]
    assert texts == ["Routine finished: Daily report\n\nok", "Routine failed: Daily report. timeout"]


def test_api_connects_links_and_unlinks_without_a_console(env):
    app = FastAPI()
    app.state.telegram = env.glue
    app.include_router(create_router())
    with TestClient(app) as client:
        assert client.get("/api/telegram").status_code == 401
        assert client.put("/api/telegram/token", json={"token": TOKEN},
                          headers={"X-Hokma-Token": HEADERS["X-Hokma-Token"]}).status_code == 403   # no owner tab
        assert client.get("/api/telegram", headers=HEADERS).json() == {
            "linked": False, "connected": False, "user_name": "", "group_linked": False, "avisos": None}
        bad = client.put("/api/telegram/token", json={"token": "nope"}, headers=HEADERS)
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "invalid_token"
        assert TOKEN_ENTRY not in env.glue.keys.items
        assert client.put("/api/telegram/token", json={"token": TOKEN}, headers=HEADERS).json() == {"connected": True}
        assert env.glue.keys.items[TOKEN_ENTRY] == TOKEN
        wrong = client.post("/api/telegram/approve", json={"code": "WRONG123"}, headers=HEADERS)
        assert wrong.status_code == 400 and wrong.json()["detail"]["code"] == "invalid_code"
        code = env.glue.link._pairing.generate_code("telegram", "111", "Ana")
        assert client.post("/api/telegram/approve", json={"code": code}, headers=HEADERS).json() == {
            "linked": True, "user_name": "Ana"}
        assert client.get("/api/telegram", headers=HEADERS).json()["linked"] is True
        assert client.delete("/api/telegram", headers=HEADERS).json() == {"linked": False}
        assert client.get("/api/telegram", headers=HEADERS).json()["linked"] is False
        assert TOKEN_ENTRY not in env.glue.keys.items


def _connect_failing_with(env, exc):
    async def boom():
        raise exc
    env.glue.start = boom
    app = FastAPI()
    app.state.telegram = env.glue
    app.include_router(create_router())
    with TestClient(app) as client:
        return client.put("/api/telegram/token", json={"token": TOKEN}, headers=HEADERS)


def test_connect_telling_rejected_token_blocked_network_and_other_failures_apart(env):
    from telegram.error import InvalidToken, NetworkError
    import httpx
    r = _connect_failing_with(env, InvalidToken("Unauthorized"))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_token"
    for exc in (NetworkError("[WinError 10054]"), httpx.ConnectError("reset")):
        r = _connect_failing_with(env, exc)
        assert r.status_code == 502 and r.json()["detail"]["code"] == "network"
        assert "another network" in r.json()["detail"]["message"]
    r = _connect_failing_with(env, RuntimeError("boom"))
    assert r.status_code == 502 and r.json()["detail"]["code"] == "cannot_connect"


def test_connect_failure_is_logged_without_the_token(env, monkeypatch):
    seen = []
    monkeypatch.setattr("hokma.engine.errlog.log", lambda message, exc=None, scope="chat": seen.append((scope, message)))
    _connect_failing_with(env, RuntimeError("bad " + TOKEN))
    assert seen and seen[0][0] == "telegram" and "RuntimeError" in seen[0][1] and TOKEN not in seen[0][1]


def test_core_copy_matches_its_manifest():
    """`core/` is a verbatim copy of the shared module: a local edit that skipped the sync fails here."""
    import hashlib
    from pathlib import Path

    core = Path(__file__).resolve().parents[1] / "hokma" / "telegram" / "core"
    listed = {}
    for line in (core / "MANIFEST.sha256").read_text().splitlines():
        digest, name = line.split("  ", 1)
        listed[name] = digest
    actual = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in core.glob("*.py")}
    assert actual == listed


def test_application_requests_use_tls12_context():
    import ssl
    from hokma.telegram.glue import build_application

    app = asyncio.run(build_application("123456:" + "a" * 35))
    for req in app.bot._request:
        transport = req._client._transport
        ctx = transport._primary._pool._ssl_context
        assert ctx.maximum_version == ssl.TLSVersion.TLSv1_2
