"""Bug 1175 (Hokma 0.0.37): students with ChatGPT Business got a generic engine error. A Business or Enterprise workspace
can keep Codex off for its members ("Allow members to use Codex locally") or not allow device code sign-in. OpenAI
does not publish the exact text of those refusals, so the texts below are representative; what is tested is that the
words of a workspace or device refusal reach a translated reason and that the real text lands in engine.log."""
import asyncio
import json
import stat
import time
from pathlib import Path

import pytest

from hokma.core import config
from hokma.engines import catalog, codex_login, diagnose, service
from test_engine_direct import Server, oa_text  # noqa: F401
from test_engines_service import ready  # noqa: F401  (fixture)
from dbsupport import env  # noqa: F401  (fixture)


@pytest.mark.parametrize("text,code", [
    ("Error logging in with device code: device code authentication is not enabled for this workspace. "
     "Contact your workspace admin.", "device_auth"),
    ("unexpected status 403 Forbidden: Codex is not enabled for your workspace. Ask your admin.", "workspace"),
    ('{"message": "Your organization has disabled Codex local access for members"}', "workspace"),
    ("unexpected status 401 Unauthorized: Your session has expired. Please sign in again.", "auth"),
    ("You've hit your usage limit. Upgrade to Pro or try again later.", "quota"),
    ("unexpected status 403 Forbidden: unsupported_country_region_territory", "region"),
    ("stream disconnected before completion", "engine_error"),
])
def test_codex_refusals_become_reasons(text, code):
    assert diagnose.classify_codex(text) == code


def test_a_failed_device_login_says_why_and_logs_without_the_code(tmp_path, monkeypatch):
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\nprintf 'Open https://auth.example/device\\nCode AB12-CD34E\\n'\nsleep 0.2\n"
                    "printf 'Error logging in with device code: device code authentication is not enabled for this "
                    "workspace\\n'\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    codex_login.start()
    for _ in range(40):
        if codex_login.status()["state"] != "waiting":
            break
        time.sleep(0.1)
    got = codex_login.status()
    codex_login.cancel()
    assert got["state"] == "failed" and got["reason"] == "device_auth"
    log = Path(config.engine_log_path()).read_text(encoding="utf-8")
    assert "codex login failed" in log and "device code authentication is not enabled" in log
    assert "AB12-CD34E" not in log


def test_a_new_openai_project_connects_even_when_its_newest_model_refuses_tools(ready, env):
    """A new OpenAI project: the list comes from GET /models with the key; some current models take tools in Chat
    Completions only through the Responses API. The test turn sends tools, so such a model is refused with 400 and
    the next one of the list is used, with no model name in Hokma."""
    from test_engine_key_errors_1173 import Provider

    listing = {"object": "list", "data": [
        {"id": "gpt-new-only-responses", "object": "model", "created": 900, "owned_by": "openai"},
        {"id": "gpt-chat-ok", "object": "model", "created": 800, "owned_by": "openai"}]}
    refusal = (lambda m: (400, json.dumps({"error": {"message": "Tool calling requires the Responses API.",
                                                     "type": "invalid_request_error"}})))
    p = Provider(listing, {"gpt-chat-ok"}, refusal)
    try:
        view = service.add_api("openai", "sk-proj-new", base_url=p.url)
        result = asyncio.run(service.ping(view["id"]))
    finally:
        p.stop()
    assert p.gets[0]["headers"]["Authorization"] == "Bearer sk-proj-new"
    assert p.sent == ["gpt-new-only-responses", "gpt-chat-ok"]
    assert result["ok"] and result["model"] == "gpt-chat-ok"


def test_ping_keeps_the_workspace_reason(ready, env, monkeypatch):
    from hokma.engines.base import EngineError

    class Adapter:
        async def open(self, ctx):
            raise EngineError(*json.loads(json.dumps(["workspace", "Codex is not enabled for your workspace"])))

    monkeypatch.setattr(service, "build_adapter", lambda kind, owner: Adapter())
    view = service.add_cli("codex", "subscription", "ChatGPT (Codex)")
    result = asyncio.run(service.ping(view["id"]))
    assert (result["ok"], result["code"]) == (False, "workspace")
