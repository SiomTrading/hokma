"""The Codex adapter against a fake `codex app-server` (CA-05, CA-08, CA-10, CA-16). The wire names follow the
published protocol; a real Codex confirms them in task T0."""
import asyncio
import json
import stat
import sys

import pytest

from hardblock_support import Box, box  # noqa: F401  (fixture)
from hokma.core.config import codex_home_dir
from hokma.engines.base import EngineContext, EngineError
from hokma.engines.codex import CodexAdapter
from hokma.permissions.broker import BrokerCtx

FAKE = r'''#!{py}
import json, sys
def send(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def recv(): return json.loads(sys.stdin.readline())
cmd = {cmd!r}
while True:
    m = recv()
    if m.get("method") == "initialize": send({{"id": m["id"], "result": {{}}}})
    elif m.get("method") == "thread/start": send({{"id": m["id"], "result": {{"thread": {{"id": "th1"}}}}}})
    elif m.get("method") == "turn/start":
        send({{"id": m["id"], "result": {{}}}})
        send({{"method": "item/started", "params": {{"item": {{"type": "commandExecution", "id": "i1", "command": cmd}}}}}})
        send({{"id": 77, "method": "item/commandExecution/requestApproval", "params": {{"itemId": "i1", "command": cmd}}}})
        r = recv()
        verdict = r["result"]["decision"]
        send({{"method": "item/completed", "params": {{"item": {{"type": "commandExecution", "id": "i1", "exitCode": 0, "aggregatedOutput": verdict}}}}}})
        send({{"method": "item/agentMessage/delta", "params": {{"delta": "decision=" + verdict}}}})
        send({{"method": "turn/completed", "params": {{"turn": {{"status": "completed"}}}}}})
        break
'''


@pytest.fixture
def setup(tmp_path, monkeypatch, box):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))

    def make(command):
        exe = tmp_path / "codex"
        exe.write_text(FAKE.format(py=sys.executable, cmd=command))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        owner = type("O", (), {"codex_bin": str(exe)})()
        bctx = BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="4")
        return CodexAdapter(owner), bctx
    return make


def ctx_for(box, bctx, mode):
    return EngineContext(conversation_id="4", model="", cwd=str(box.work), mode=mode, tier="deep",
                         instructions="Persona text", extra={"broker": bctx})


async def collect(adapter, ctx, answer=None):
    session = await adapter.open(ctx)
    events = []
    async for ev in session.send([{"type": "text", "text": "go"}]):
        events.append(ev)
        if ev["type"] == "permission_request":
            await session.answer_permission(ev["request_id"], answer, "no")
    await session.close()
    return events


@pytest.mark.parametrize("mode", ["ask", "auto"])
def test_a_delete_is_declined_in_both_modes(setup, box, mode):
    adapter, bctx = setup("rm f")
    events = asyncio.run(collect(adapter, ctx_for(box, bctx, mode)))
    assert "decision=decline" in "".join(e.get("text", "") for e in events if e["type"] == "text")
    assert not any(e["type"] == "permission_request" for e in events)
    assert "delete" in (box.data / "guard" / "blocks.jsonl").read_text()
    assert events[-1]["status"] == "complete"


def test_auto_mode_accepts_by_itself_after_the_broker_and_config_never_drops_approvals(setup, box):
    adapter, bctx = setup("git status")
    events = asyncio.run(collect(adapter, ctx_for(box, bctx, "auto")))
    assert "decision=accept" in "".join(e.get("text", "") for e in events if e["type"] == "text")
    toml = (codex_home_dir() / "config.toml").read_text()
    assert 'approval_policy = "untrusted"' in toml and "never" not in toml and 'model_reasoning_effort = "high"' in toml
    assert (codex_home_dir() / "AGENTS.md").read_text() == "Persona text"


def test_ask_mode_asks_the_owner(setup, box):
    adapter, bctx = setup("git status")
    events = asyncio.run(collect(adapter, ctx_for(box, bctx, "ask"), answer=False))
    assert any(e["type"] == "permission_request" and e["tool"] == "Bash" for e in events)
    assert "decision=decline" in "".join(e.get("text", "") for e in events if e["type"] == "text")


def test_config_is_rebuilt_on_every_start(setup, box):
    adapter, bctx = setup("git status")
    asyncio.run(collect(adapter, ctx_for(box, bctx, "auto")))
    (codex_home_dir() / "config.toml").write_text('approval_policy = "never"\n')
    (codex_home_dir() / "AGENTS.md").write_text("tampered")
    asyncio.run(collect(adapter, ctx_for(box, bctx, "auto")))
    assert 'approval_policy = "untrusted"' in (codex_home_dir() / "config.toml").read_text()
    assert (codex_home_dir() / "AGENTS.md").read_text() == "Persona text"


def test_missing_binary_is_an_engine_error(tmp_path, monkeypatch, box):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(EngineError) as info:
        asyncio.run(CodexAdapter().open(EngineContext(conversation_id="1", model="", cwd=str(box.work))))
    assert info.value.code == "codex_not_found"
_ = json


def test_command_lists_are_joined_safely_and_wrappers_are_unwrapped():
    from hokma.engines.codex import command_request
    assert command_request(["bash", "-lc", "rm f"]) == ("Bash", {"command": "rm f"})
    assert command_request(["sh", "-c", "git status"]) == ("Bash", {"command": "git status"})
    assert command_request(["powershell.exe", "-Command", "Remove-Item f"]) == (
        "PowerShell", {"command": "Remove-Item f"})
    assert command_request(["echo", "a b", "c;d"]) == ("Bash", {"command": "echo 'a b' 'c;d'"})
    assert command_request("ls -la") == ("Bash", {"command": "ls -la"})


def test_a_wrapped_delete_is_declined(setup, box):
    adapter, bctx = setup(["bash", "-lc", "rm f"])
    events = asyncio.run(collect(adapter, ctx_for(box, bctx, "auto")))
    assert "decision=decline" in "".join(e.get("text", "") for e in events if e["type"] == "text")


def test_only_the_exact_wrapper_forms_are_unwrapped():
    from hokma.engines.codex import command_request
    assert command_request(["bash", "-lc", "ls", "extra"]) == ("Bash", {"command": "bash -lc ls extra"})
    assert command_request(["bash", "-c", "ls", "-c", "rm f"])[1]["command"].startswith("bash -c ls")
    assert command_request(["pwsh", "-Command", "ls", "-NoProfile"]) == ("Bash", {"command": "pwsh -Command ls -NoProfile"})


def test_powershell_is_unwrapped_only_in_the_exact_three_part_form():
    from hokma.engines.codex import command_request
    assert command_request(["pwsh", "-c", "ls"]) == ("PowerShell", {"command": "ls"})
    for argv in (["pwsh", "-File", "x.ps1"], ["pwsh", "-EncodedCommand", "AAA"], ["pwsh", "-NoProfile", "-Command", "ls"]):
        assert command_request(argv) == ("Bash", {"command": " ".join(argv)})
