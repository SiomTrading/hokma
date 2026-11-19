"""Bug 1212: Codex 0.170 removed approval_policy "untrusted" and refused to load Hokma's config.toml ("Error loading
configuration"), with nothing in engine.log. The config now carries a documented value (on-request, with a read-only
sandbox so writes and commands still go to Hokma's approval), a configuration error reaches engine.log with its real
text, and the chat shows a translated reason."""
import asyncio
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from hardblock_support import Box, box  # noqa: F401  (fixture)
from hokma.core import config
from hokma.core.config import codex_home_dir
from hokma.engines import diagnose
from hokma.engines.base import EngineContext
from hokma.engines.codex import CodexAdapter, parse_version, policy_for, write_config
from hokma.permissions.broker import BrokerCtx

REAL = ('Error loading configuration: approval_policy = "untrusted" is no longer supported; remove this setting')


def test_current_config_has_no_untrusted_and_never_drops_approvals(box):
    ctx = EngineContext(conversation_id="1", model="", cwd=str(box.work))
    for version in (None, (0, 170, 0), (0, 200, 1)):
        text = (write_config(ctx, {}, version) / "config.toml").read_text(encoding="utf-8")
        data = tomllib.loads(text)
        # on-request would let sandboxed commands run unasked: approval_policy is omitted and the project is untrusted,
        # the documented replacement (every command asks unless an execpolicy rule allows it)
        assert "approval_policy" not in data and "on-request" not in text
        assert data["sandbox_mode"] == "read-only"
        assert data["projects"][str(box.work)]["trust_level"] == "untrusted"


def test_older_codex_keeps_what_it_understands(box):
    ctx = EngineContext(conversation_id="1", model="", cwd=str(box.work))
    data = tomllib.loads((write_config(ctx, {}, (0, 169, 3)) / "config.toml").read_text(encoding="utf-8"))
    assert data["approval_policy"] == "untrusted" and data["sandbox_mode"] == "workspace-write"


def test_versions_are_read_from_the_cli_text():
    assert parse_version("codex-cli 0.170.2") == (0, 170, 2)
    assert parse_version("codex 1.0") == (1, 0)
    assert parse_version("nothing") is None
    assert policy_for((0, 169, 9))[0] == "untrusted" and policy_for((1, 0, 0))[0] is None and policy_for(None)[0] is None


def test_a_configuration_error_is_classified():
    assert diagnose.classify_codex(REAL) == "config"
    assert diagnose.classify_codex("stream disconnected before completion") == "engine_error"


FAKE = f'''#!{sys.executable}
import sys
if "--version" in sys.argv:
    print("codex-cli 0.170.0"); sys.exit(0)
sys.stderr.write({REAL!r} + "\\n"); sys.stderr.flush()
sys.exit(1)
'''


def test_a_config_error_reaches_the_log_and_the_chat(tmp_path, monkeypatch, box):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    exe = tmp_path / "codex"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    adapter = CodexAdapter(type("O", (), {"codex_bin": str(exe)})())
    bctx = BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="4")
    ctx = EngineContext(conversation_id="4", model="", cwd=str(box.work), mode="auto", extra={"broker": bctx})

    async def run():
        session = await adapter.open(ctx)
        events = [ev async for ev in session.send([{"type": "text", "text": "go"}])]
        await session.close()
        return events
    events = asyncio.run(run())
    err = [e for e in events if e["type"] == "error"]
    assert err and err[0]["code"] == "config" and "no longer supported" in err[0]["message"]
    log = Path(config.engine_log_path()).read_text(encoding="utf-8")
    assert "codex: config:" in log and "approval_policy" in log


def test_scrub_home_replaces_the_user_folder():
    from hokma.engines.codex import _failure, scrub_home
    home = str(Path.home())
    assert scrub_home(f"cannot read {home}/.codex/x and {home}") == "cannot read ~/.codex/x and ~"
    _, message = _failure(f"boom at {home}/proj")
    assert home not in message and "~/proj" in message


SERVER = r'''#!{py}
import json, sys
def send(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
if "--version" in sys.argv:
    print("codex-cli 0.170.0"); sys.exit(0)
cmd = ["python", "-c", "import os; print(os.listdir('.'))"]
while True:
    m = json.loads(sys.stdin.readline())
    if m.get("method") == "initialize": send({{"id": m["id"], "result": {{}}}})
    elif m.get("method") == "thread/start":
        send({{"id": m["id"], "result": {{"thread": {{"id": "th1"}}}}}})
        open({rec!r}, "w").write(json.dumps(m["params"]))
    elif m.get("method") == "turn/start":
        send({{"id": m["id"], "result": {{}}}})
        send({{"id": 5, "method": "item/commandExecution/requestApproval", "params": {{"itemId": "i1", "command": cmd}}}})
        r = json.loads(sys.stdin.readline())
        send({{"method": "item/agentMessage/delta", "params": {{"delta": "decision=" + r["result"]["decision"]}}}})
        send({{"method": "turn/completed", "params": {{"turn": {{"status": "completed"}}}}}})
        break
'''


def test_a_non_trivial_command_still_goes_to_hokma_with_the_new_config(tmp_path, monkeypatch, box):
    import json
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    rec = tmp_path / "thread.json"
    exe = tmp_path / "codex"
    exe.write_text(SERVER.format(py=sys.executable, rec=str(rec)))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    adapter = CodexAdapter(type("O", (), {"codex_bin": str(exe)})())
    bctx = BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="4")
    ctx = EngineContext(conversation_id="4", model="", cwd=str(box.work), mode="ask", extra={"broker": bctx})

    async def run():
        session = await adapter.open(ctx)
        events = []
        async for ev in session.send([{"type": "text", "text": "go"}]):
            events.append(ev)
            if ev["type"] == "permission_request":
                await session.answer_permission(ev["request_id"], False, "no")
        await session.close()
        return events
    events = asyncio.run(run())
    asks = [e for e in events if e["type"] == "permission_request"]
    assert asks and asks[0]["tool"] == "Bash" and "python -c" in json.dumps(asks[0]["input"])
    params = json.loads(rec.read_text())
    assert "approvalPolicy" not in params and params["sandbox"] == "read-only"
    toml = tomllib.loads((codex_home_dir() / "config.toml").read_text(encoding="utf-8"))
    assert "approval_policy" not in toml and toml["projects"][str(box.work)]["trust_level"] == "untrusted"
