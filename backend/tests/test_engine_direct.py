"""The direct engine against a fake OpenAI-compatible server and a fake Anthropic one (CA-06, CA-07, CA-08)."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hardblock_support import Box, box  # noqa: F401  (fixture)
from hokma.engines import secrets
from hokma.engines.base import EngineContext, EngineError
from hokma.engines.direct.loop import DirectAdapter
from hokma.permissions.broker import BrokerCtx


def _sse(handler, events):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    for ev in events:
        handler.wfile.write(f"data: {ev}\n\n".encode())
    handler.wfile.flush()


class Server:
    """Replies with scripted event lists, one script per request; records the requests."""

    def __init__(self, scripts, status=200, error_body=""):
        self.scripts, self.requests, self.status, self.error_body = list(scripts), [], status, error_body
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    self.wfile.write(outer.error_body.encode())
                    return
                _sse(self, outer.scripts.pop(0))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()


def oa_text(*parts):
    return [json.dumps({"choices": [{"delta": {"content": p}}]}) for p in parts] + [
        json.dumps({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}), "[DONE]"]


def oa_call(call_id, name, args):
    return [json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id,
                                                                "function": {"name": name, "arguments": args[:5]}}]}}]}),
            json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[5:]}}]}}]}),
            "[DONE]"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(secrets, "get", lambda cid: "sk-test")
    return tmp_path


def make_ctx(box, url, mode="auto", wire="openai", provider="custom"):
    conn = {"id": 1, "engine": "api", "provider": provider, "wire": wire, "base_url": url, "auth_kind": "api_key"}
    bctx = BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="9")
    return EngineContext(conversation_id="9", model="m1", cwd=str(box.work), mode=mode, connection=conn,
                         instructions="be brief", extra={"broker": bctx})


async def run(adapter, ctx, text="hi", answer=None):
    session = await adapter.open(ctx)
    events = []
    async for ev in session.send([{"type": "text", "text": text}]):
        events.append(ev)
        if ev["type"] == "permission_request" and answer is not None:
            await session.answer_permission(ev["request_id"], answer, "no")
    await session.close()
    return events, session


def test_streams_text_and_reports_usage(env, box):
    srv = Server([oa_text("Hel", "lo")])
    try:
        events, _ = asyncio.run(run(DirectAdapter(), make_ctx(box, srv.url)))
    finally:
        srv.stop()
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Hello"
    done = events[-1]
    assert done["type"] == "done" and done["status"] == "complete" and done["usage"]["tokens_in"] == 7
    req = srv.requests[0]
    assert req["path"] == "/chat/completions" and req["headers"]["Authorization"] == "Bearer sk-test"
    assert req["body"]["messages"][0] == {"role": "system", "content": "be brief"}


def test_tool_loop_writes_a_file_then_answers(env, box):
    args = json.dumps({"path": "made.txt", "content": "hello"})
    srv = Server([oa_call("c1", "write_file", args), oa_text("done")])
    try:
        events, _ = asyncio.run(run(DirectAdapter(), make_ctx(box, srv.url)))
    finally:
        srv.stop()
    assert (box.work / "made.txt").read_text() == "hello"
    kinds = [e["type"] for e in events]
    assert kinds.index("tool_use") < kinds.index("tool_result") < kinds.index("done")
    second = srv.requests[1]["body"]["messages"]
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "c1"


def test_a_delete_command_is_blocked_by_the_broker_in_auto_mode(env, box):
    srv = Server([oa_call("c1", "run_command", json.dumps({"command": "rm f"})), oa_text("ok")])
    try:
        events, _ = asyncio.run(run(DirectAdapter(), make_ctx(box, srv.url, mode="auto")))
    finally:
        srv.stop()
    res = next(e for e in events if e["type"] == "tool_result")
    assert res["is_error"] and res["text"] == "HOKMA_BLOCKED delete"
    assert (box.work / "f").exists()
    assert "delete" in (box.data / "guard" / "blocks.jsonl").read_text()


def test_ask_mode_asks_before_a_write_and_a_no_stops_it(env, box):
    args = json.dumps({"path": "nope.txt", "content": "x"})
    srv = Server([oa_call("c1", "write_file", args), oa_text("fine")])
    try:
        events, _ = asyncio.run(run(DirectAdapter(), make_ctx(box, srv.url, mode="ask"), answer=False))
    finally:
        srv.stop()
    assert any(e["type"] == "permission_request" and e["tool"] == "write_file" for e in events)
    assert not (box.work / "nope.txt").exists()
    assert next(e for e in events if e["type"] == "tool_result")["is_error"]


def test_ask_mode_lets_reads_through_without_asking(env, box):
    srv = Server([oa_call("c1", "read_file", json.dumps({"path": "notes.md"})), oa_text("ok")])
    try:
        events, _ = asyncio.run(run(DirectAdapter(), make_ctx(box, srv.url, mode="ask")))
    finally:
        srv.stop()
    assert not any(e["type"] == "permission_request" for e in events)
    assert "some notes" in next(e for e in events if e["type"] == "tool_result")["text"]


def test_anthropic_wire_streams_and_calls_tools(env, box):
    first = [json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}),
             json.dumps({"type": "content_block_start", "index": 0,
                         "content_block": {"type": "tool_use", "id": "t1", "name": "list_dir"}}),
             json.dumps({"type": "content_block_delta", "index": 0,
                         "delta": {"type": "input_json_delta", "partial_json": "{\"path\":"}}),
             json.dumps({"type": "content_block_delta", "index": 0,
                         "delta": {"type": "input_json_delta", "partial_json": "\".\"}"}}),
             json.dumps({"type": "message_delta", "usage": {"output_tokens": 2}})]
    second = [json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
              json.dumps({"type": "message_delta", "usage": {"output_tokens": 1}})]
    srv = Server([first, second])
    try:
        events, _ = asyncio.run(run(DirectAdapter(), make_ctx(box, srv.url, wire="anthropic", provider="anthropic")))
    finally:
        srv.stop()
    assert srv.requests[0]["path"] == "/v1/messages" and {k.lower(): v for k, v in srv.requests[0]["headers"].items()}["x-api-key"] == "sk-test"
    assert "notes.md" in next(e for e in events if e["type"] == "tool_result")["text"]
    assert events[-1]["status"] == "complete"


@pytest.mark.parametrize("status,body,code", [
    (403, "Country, region, or territory not supported", "region"),
    (401, "invalid api key", "auth"), (429, "rate", "quota"), (500, "boom", "engine_error")])
def test_provider_failures_become_codes(env, box, status, body, code):
    srv = Server([], status=status, error_body=body)
    try:
        events, _ = asyncio.run(run(DirectAdapter(), make_ctx(box, srv.url)))
    finally:
        srv.stop()
    assert events[-1]["type"] == "error" and events[-1]["code"] == code


def test_transcript_survives_a_new_session(env, box):
    srv = Server([oa_text("one"), oa_text("two")])
    try:
        asyncio.run(run(DirectAdapter(), make_ctx(box, srv.url), "first"))
        asyncio.run(run(DirectAdapter(), make_ctx(box, srv.url), "second"))
    finally:
        srv.stop()
    roles = [m["role"] for m in srv.requests[1]["body"]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


def test_no_connection_or_key_or_model_is_an_engine_error(env, box, monkeypatch):
    ctx = make_ctx(box, "http://127.0.0.1:1")
    ctx.connection = None
    with pytest.raises(EngineError):
        asyncio.run(DirectAdapter().open(ctx))
    ctx = make_ctx(box, "http://127.0.0.1:1")
    ctx.model = ""
    with pytest.raises(EngineError) as info:
        asyncio.run(DirectAdapter().open(ctx))
    assert info.value.code == "no_model"
    monkeypatch.setattr(secrets, "get", lambda cid: None)
    with pytest.raises(EngineError) as info:
        asyncio.run(DirectAdapter().open(make_ctx(box, "http://127.0.0.1:1")))
    assert info.value.code == "auth"


def test_run_command_gets_the_allow_listed_environment(env, box, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "LEAK-ME")
    monkeypatch.setenv("HOKMA_SESSION_TOKEN", "LEAK-TOKEN")
    cmd = "python3 -c \"import os;print(sorted(k for k in os.environ if 'KEY' in k or 'TOKEN' in k))\""
    srv = Server([oa_call("c1", "run_command", json.dumps({"command": cmd})), oa_text("ok")])
    try:
        events, _ = asyncio.run(run(DirectAdapter(), make_ctx(box, srv.url)))
    finally:
        srv.stop()
    out = next(e for e in events if e["type"] == "tool_result")["text"]
    assert "LEAK" not in out and "OPENAI_API_KEY" not in out and "HOKMA_SESSION_TOKEN" not in out


def test_run_command_ignores_a_cwd_argument(env, box):
    (box.work / "d").mkdir(exist_ok=True)
    srv = Server([oa_call("c1", "run_command", json.dumps({"command": "pwd", "cwd": str(box.work / "d")})),
                  oa_text("ok")])
    try:
        events, _ = asyncio.run(run(DirectAdapter(), make_ctx(box, srv.url)))
    finally:
        srv.stop()
    out = next(e for e in events if e["type"] == "tool_result")
    assert str(box.work) in out["text"] and "/d\n" not in out["text"].split("[exit")[0]


def test_web_fetch_refuses_private_hosts_and_redirects(env, box):
    from hokma.engines.direct import tools
    for url in ("http://127.0.0.1:1/", "http://localhost/", "http://169.254.169.254/latest", "http://10.0.0.5/",
                "file:///etc/passwd"):
        text, is_error = tools.run("web_fetch", str(box.work), {"url": url})
        assert is_error, url
    import urllib.request
    handler = tools._NoRedirect()
    with pytest.raises(ValueError):
        handler.redirect_request(None, None, 302, "x", {}, "http://evil/")


def test_glob_rejects_escapes_and_protected_results(env, box):
    from hokma.engines.direct import tools
    for pattern in ("../*", "/etc/*", "a/../../*", "C:/x/*"):
        assert tools.run("glob", str(box.work), {"pattern": pattern})[1], pattern
    (box.work / "ok.txt").write_text("x")
    link = box.work / "linkdir"
    link.symlink_to(box.data)
    (box.data / "secret.txt").write_text("s")
    tools.PROTECTED[:] = [str(box.data)]
    text, is_error = tools.run("glob", str(box.work), {"pattern": "**/*.txt"})
    assert not is_error and "ok.txt" in text and "secret.txt" not in text


def test_web_fetch_refuses_shared_address_space(env, box, monkeypatch):
    import socket
    from hokma.engines.direct import tools
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("100.64.1.1", 0))])
    assert tools.run("web_fetch", str(box.work), {"url": "http://carrier.example/"})[1]
