"""The Gemini (ACP) adapter against a fake `gemini --acp` (CA-25). Method names follow the published
protocol; a real Gemini confirms them in task T0."""
import asyncio
import stat
import sys

import pytest

from hardblock_support import Box, box  # noqa: F401  (fixture)
from hokma.engines.acp import AcpAdapter
from hokma.engines.base import EngineContext, EngineError
from hokma.engines.codex import RpcProcess  # noqa: F401
from hokma.permissions.broker import BrokerCtx

FAKE = r'''#!{py}
import json, sys
def send(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def recv(): return json.loads(sys.stdin.readline())
cmd = {cmd!r}
while True:
    m = recv()
    meth = m.get("method")
    if meth == "initialize": send({{"id": m["id"], "result": {{"authMethods": [{{"id": "oauth-personal"}}]}}}})
    elif meth == "authenticate": send({{"id": m["id"], "result": {{}}}})
    elif meth == "session/new": send({{"id": m["id"], "result": {{"sessionId": "s1"}}}})
    elif meth == "session/prompt":
        pid = m["id"]
        send({{"method": "session/update", "params": {{"update": {{"sessionUpdate": "tool_call", "toolCallId": "t1", "kind": "execute", "title": cmd, "rawInput": {{"command": cmd}}}}}}}})
        send({{"id": 50, "method": "session/request_permission", "params": {{"sessionId": "s1", "toolCall": {{"toolCallId": "t1", "kind": "execute"}}, "options": [{{"optionId": "yes", "kind": "allow_once"}}, {{"optionId": "no", "kind": "reject_once"}}]}}}})
        r = recv()
        opt = r["result"]["outcome"].get("optionId")
        send({{"method": "session/update", "params": {{"update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": "picked=" + str(opt)}}}}}}}})
        send({{"id": pid, "result": {{"stopReason": "end_turn"}}}})
        break
'''


@pytest.fixture
def setup(tmp_path, monkeypatch, box):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))

    def make(command):
        exe = tmp_path / "gemini"
        exe.write_text(FAKE.format(py=sys.executable, cmd=command))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        owner = type("O", (), {"gemini_bin": str(exe)})()
        bctx = BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="5")
        return AcpAdapter(owner), bctx
    return make


async def collect(adapter, ctx, answer=None):
    session = await adapter.open(ctx)
    events = []
    async for ev in session.send([{"type": "text", "text": "go"}]):
        events.append(ev)
        if ev["type"] == "permission_request":
            await session.answer_permission(ev["request_id"], answer, "no")
    await session.close()
    return events


def ctx_for(box, bctx, mode):
    return EngineContext(conversation_id="5", model="", cwd=str(box.work), mode=mode, extra={"broker": bctx})


def said(events):
    return "".join(e.get("text", "") for e in events if e["type"] == "text")


@pytest.mark.parametrize("mode", ["ask", "auto"])
def test_a_delete_is_rejected_by_the_broker_in_both_modes(setup, box, mode):
    adapter, bctx = setup("rm f")
    events = asyncio.run(collect(adapter, ctx_for(box, bctx, mode)))
    assert said(events) == "picked=no" and not any(e["type"] == "permission_request" for e in events)
    assert "delete" in (box.data / "guard" / "blocks.jsonl").read_text()


def test_auto_accepts_after_the_broker_and_ask_asks(setup, box):
    adapter, bctx = setup("git status")
    assert said(asyncio.run(collect(adapter, ctx_for(box, bctx, "auto")))) == "picked=yes"
    events = asyncio.run(collect(adapter, ctx_for(box, bctx, "ask"), answer=False))
    assert any(e["type"] == "permission_request" for e in events) and said(events) == "picked=no"
    assert events[-1]["status"] == "complete"


def test_missing_binary(tmp_path, monkeypatch, box):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(EngineError) as info:
        asyncio.run(AcpAdapter().open(EngineContext(conversation_id="1", model="", cwd=str(box.work))))
    assert info.value.code == "gemini_not_found"


def test_delete_and_move_use_quoted_sources_and_check_destinations(box):
    from hokma.engines.acp import to_requests
    reqs = to_requests("move", {"rawInput": {"path": "a b.txt", "destination": "/etc/x"}})
    assert reqs[0] == ("Bash", {"command": "rm 'a b.txt'"}) and ("Write", {"file_path": "/etc/x"}) in reqs
    assert to_requests("delete", {"rawInput": {}}) == [("acp_invalid", {})]


def test_every_path_field_is_collected_and_a_pathless_call_is_refused(box):
    from hokma.engines.acp import paths_of, to_requests
    call = {"rawInput": {"absolute_path": "/a", "paths": ["/b"], "dir_path": "/c"}, "locations": [{"path": "/d"}]}
    assert set(paths_of(call)) == {"/a", "/b", "/c", "/d"}
    assert len(to_requests("read", call)) == 4
    for kind in ("read", "edit", "search", "delete"):
        assert to_requests(kind, {"rawInput": {"command": "x"}}) == [("acp_invalid", {})]


def test_a_pathless_read_is_declined_by_the_broker(setup, box):
    from hokma.engines.acp import to_requests
    from hokma.permissions.broker import check
    _, bctx = setup("x")
    name, request = to_requests("read", {"rawInput": {}})[0]
    assert not check(name, request, cwd=str(box.work), ctx=bctx).allowed


def test_closed_by_default_shapes(box, tmp_path):
    from hokma.engines.acp import INVALID, to_requests
    cwd = str(box.work)
    assert to_requests("read", {"rawInput": {"path": 5}}) == INVALID
    assert to_requests("read", {"rawInput": {"File_Path": None}}) == INVALID
    assert to_requests("read", {"rawInput": {"paths": "a"}}) == INVALID
    assert to_requests("read", {"rawInput": {"paths": ["a", 3]}}) == INVALID
    assert to_requests("read", {"rawInput": {"path": "a"}, "locations": [{"path": 1}]}) == INVALID
    assert to_requests("fetch", {"rawInput": {}}) == INVALID
    assert to_requests("move", {"rawInput": {"path": "a"}}) == INVALID
    assert to_requests("execute", {"rawInput": {"command": "ls", "cwd": "/"}}, cwd) == INVALID
    assert to_requests("execute", {"rawInput": {"command": "ls", "Work_Dir": str(tmp_path)}}, cwd) == INVALID
    assert to_requests("execute", {"rawInput": {"command": "ls", "cwd": cwd}}, cwd) == [("Bash", {"command": "ls"})]
    reqs = to_requests("delete", {"rawInput": {"Absolute-Path": "/a"}, "locations": [{"path": "/b"}]})
    assert ("Bash", {"command": "rm /a"}) in reqs and ("Write", {"file_path": "/b"}) in reqs
    assert to_requests("read", {"rawInput": {"FilePath": "/a"}}) == [("Read", {"file_path": "/a"})]


def test_colliding_keys_are_refused(box):
    from hokma.engines.acp import INVALID, to_requests
    assert to_requests("read", {"rawInput": {"file_path": "/ok", "filePath": "/etc/passwd"}}) == INVALID
    assert to_requests("execute", {"rawInput": {"command": "ls", "Command": "rm f"}}) == INVALID
