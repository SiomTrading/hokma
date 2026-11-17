"""Bug 1173 (Hokma 0.0.37): "Connect with my key" for Google answered "The engine answered with an error" whatever the
cause. Google answers a bad key with 400 (not 401), and the test turn used a model a new key no longer gets.

Since 0.0.39 Hokma keeps no model name: the models come from the provider's own list (with the student's key), the
levels are picked from it with one rule, a model that is gone is replaced with a notice, and a model the student chose
is never replaced. The doubles below answer like each provider documents its list and its errors. Model names in this
file are test data only (the engine code may not carry any: see the last test)."""
import asyncio
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hokma.core import config
from hokma.db import get_database
from hokma.engines import catalog, diagnose, models, service
from hokma.engines.base import EngineContext
from hokma.engines.direct.loop import DirectSession
from test_engine_direct import Server, _sse, oa_text  # noqa: F401
from test_engines_service import ready  # noqa: F401  (fixture)
from dbsupport import env  # noqa: F401  (fixture)

KEY = "AIzaSyTEST-not-a-real-key-000000000000"


def google(code, status, message, reason=None):
    error = {"code": code, "message": message, "status": status}
    if reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason,
                             "domain": "googleapis.com"}]
    return json.dumps([{"error": error}])


BAD_KEY = google(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.", "API_KEY_INVALID")
OLD_MODEL = google(404, "NOT_FOUND", "This model models/gemini-2.5-flash-lite is no longer available to new users. "
                                     "Please update your code to use a newer model for the latest features and improvements.")
QUOTA = google(429, "RESOURCE_EXHAUSTED", "You exceeded your current quota, please check your plan and billing details.")
REGION = google(400, "FAILED_PRECONDITION", "User location is not supported for the API use.")
DOWN = google(503, "UNAVAILABLE", "The model is overloaded. Please try again later.")


class Provider:
    """GET <models_path> answers `listing` (any shape); POST /chat/completions or /v1/messages answers OK for a model
    in `works`, and `refusal(model)` (status, body) for any other."""

    def __init__(self, listing, works, refusal=None, list_refusal=None):
        self.listing, self.works, self.sent, self.gets = listing, set(works), [], []
        self.list_refusal = list_refusal                   # (status, body): the list itself is refused
        self.refusal = refusal or (lambda m: (404, google(404, "NOT_FOUND", f"This model models/{m} is no longer "
                                                                              "available to new users.")))
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.gets.append({"path": self.path, "headers": dict(self.headers)})
                if outer.list_refusal:
                    self.send_response(outer.list_refusal[0])
                    self.end_headers()
                    self.wfile.write(outer.list_refusal[1].encode())
                    return
                body = json.dumps(outer.listing() if callable(outer.listing) else outer.listing)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.sent.append(body["model"])
                if body["model"] not in outer.works:
                    status, text = outer.refusal(body["model"])
                    self.send_response(status)
                    self.end_headers()
                    self.wfile.write(text.encode())
                    return
                _sse(self, oa_text("OK"))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()


def ids(names):
    return {"object": "list", "data": [{"id": n, "object": "model"} for n in names]}


# Google's OpenAI compatible list: ids with a `models/` prefix, no dates; open models and media models mixed in.
GOOGLE_LIST = ids(["models/gemini-2.5-flash", "models/gemini-3.5-flash-lite", "models/gemini-3.8-flash",
                   "models/gemini-3.8-flash-image", "models/gemini-3.1-pro-preview", "models/gemini-embedding-2",
                   "models/gemini-3.6-flash", "models/gemma-9-27b-it", "models/gemini-3.5-transcribe"])
GOOGLE_WORKS = {"gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.6-flash"}


def ping(view):
    return asyncio.run(service.ping(view["id"]))


def saved(view):
    with get_database().read() as u:
        return u.engine_connections.get(view["id"])


# ------------------------------------------------------------------ the reason of a refusal

def ping_refused(status, body):
    """The provider refuses the key's list and the chat call alike (a bad key, the region, the quota, an outage).
    A refused model is the one case where the list answers: the key is fine, the saved model is not."""
    model_case = "NOT_FOUND" in body
    srv = Provider(GOOGLE_LIST, set(), refusal=lambda m: (status, body),
                   list_refusal=None if model_case else (status, body))
    try:
        view = service.add_api("google", KEY, label=f"g{status}", base_url=srv.url)
        return ping(view)
    finally:
        srv.stop()


@pytest.mark.parametrize("status,body,expected", [
    (400, BAD_KEY, ("no_session", "auth")),
    (404, OLD_MODEL, ("error", "model")),
    (429, QUOTA, ("error", "quota")),
    (400, REGION, ("error", "region")),
    (503, DOWN, ("error", "unavailable")),
])
def test_each_google_refusal_has_its_own_reason(ready, env, status, body, expected):
    result = ping_refused(status, body)
    assert (result["status"], result["code"]) == expected


def test_the_real_reason_goes_to_engine_log_without_the_key(ready, env):
    ping_refused(400, BAD_KEY)
    log = Path(config.engine_log_path()).read_text(encoding="utf-8")
    assert "API_KEY_INVALID" in log and "HTTP 400" in log
    assert KEY not in log


@pytest.mark.parametrize("status,body,code", [
    (400, BAD_KEY, "auth"), (401, '{"error": {"message": "Incorrect API key provided"}}', "auth"),
    (404, '{"error": {"message": "The model `x` does not exist", "code": "model_not_found"}}', "model"),
    (400, '{"error": {"message": "Tool calling requires the Responses API."}}', "model"),
    (402, '{"error": {"message": "Insufficient balance"}}', "quota"),
    (500, "boom", "unavailable"), (400, '{"error": {"message": "bad tools"}}', "engine_error"),
])
def test_classify_reads_the_status_and_the_body(status, body, code):
    assert diagnose.classify("google" if body is BAD_KEY else "openai", status, body) == code


# ------------------------------------------------------------------ the levels come from the provider's list

def test_google_levels_from_its_list():
    picked = catalog.pick_tiers("google", catalog.normalize_models(GOOGLE_LIST))
    # flash-lite is the small one, the newest plain flash the balanced one, no stable pro: Deep takes the balanced
    assert picked == {"fast": "gemini-3.5-flash-lite", "balanced": "gemini-3.8-flash", "deep": "gemini-3.8-flash",
                      catalog.AUTO: "1"}
    assert catalog.pick_tiers("google", ["models/gemini-3.8-flash-image", "models/gemma-9-27b-it"]) == {}


def test_openai_levels_follow_created_and_skip_shutdowns_and_media():
    """OpenAI's list: `created` (unix) and `shutdown_date`. No size word in the newest names: every level takes the
    newest chat model; a dated shutdown and audio/image models are left out."""
    listing = {"object": "list", "data": [
        {"id": "gpt-a-old", "object": "model", "created": 100, "owned_by": "openai", "shutdown_date": None},
        {"id": "gpt-b-going", "object": "model", "created": 900, "owned_by": "openai", "shutdown_date": "2026-12-11"},
        {"id": "gpt-c-new", "object": "model", "created": 500, "owned_by": "openai", "shutdown_date": None},
        {"id": "gpt-c-new-mini", "object": "model", "created": 400, "owned_by": "openai", "shutdown_date": None},
        {"id": "gpt-realtime-z", "object": "model", "created": 999, "owned_by": "openai"},
        {"id": "gpt-image-z", "object": "model", "created": 999, "owned_by": "openai"}]}
    picked = catalog.pick_tiers("openai", catalog.normalize_models(listing))
    assert picked["balanced"] == "gpt-c-new" and picked["deep"] == "gpt-c-new" and picked["fast"] == "gpt-c-new-mini"


def test_anthropic_levels_use_lifecycle_and_created_at():
    listing = {"data": [
        {"type": "model", "id": "claude-x-opus", "created_at": "2026-09-21T00:00:00Z", "lifecycle": "active",
         "line": "opus", "deprecated_at": "2019-12-27T18:11:19.117Z"},
        {"type": "model", "id": "claude-y-sonnet", "created_at": "2026-09-01T00:00:00Z", "lifecycle": "active"},
        {"type": "model", "id": "claude-z-sonnet", "created_at": "2026-10-01T00:00:00Z", "lifecycle": "deprecated"},
        {"type": "model", "id": "claude-w-haiku", "created_at": "2026-08-01T00:00:00Z", "lifecycle": "active"}],
        "has_more": False}
    picked = catalog.pick_tiers("anthropic", catalog.normalize_models(listing))
    assert picked["fast"] == "claude-w-haiku" and picked["balanced"] == "claude-y-sonnet" \
        and picked["deep"] == "claude-x-opus"


def test_deepseek_and_kimi_shapes():
    deepseek = {"object": "list", "data": [
        {"id": "ds-flash", "object": "model", "owned_by": "deepseek", "output_modalities": ["text"]},
        {"id": "ds-v9-pro", "object": "model", "owned_by": "deepseek", "output_modalities": ["text"]},
        {"id": "ds-v9-paint", "object": "model", "owned_by": "deepseek", "output_modalities": ["image"]}]}
    assert catalog.pick_tiers("deepseek", catalog.normalize_models(deepseek))["deep"] == "ds-v9-pro"
    kimi = {"object": "list", "data": [
        {"id": "k-9", "object": "model", "created": 3, "owned_by": "moonshot"},
        {"id": "k-9.1-pro", "object": "model", "created": 5, "owned_by": "moonshot"},
        {"id": "k-9.1-pro-highspeed", "object": "model", "created": 5, "owned_by": "moonshot"}]}
    assert catalog.pick_tiers("moonshot", catalog.normalize_models(kimi)) == {
        "fast": "k-9.1-pro-highspeed", "balanced": "k-9", "deep": "k-9.1-pro", catalog.AUTO: "1"}


def test_a_name_with_a_trailing_newline_is_not_a_model():
    """Vera B1: an id with a newline is dropped, never cleaned into a model."""
    assert catalog.pick_tiers("google", ["gemini-9-flash\n"]) == {}
    assert catalog.pick_tiers("google", ["gemini-9-flash\n", "gemini-3.8-flash"])["balanced"] == "gemini-3.8-flash"


def test_ids_with_control_or_bidi_characters_are_dropped():
    """Vera B1 (second round): an ANSI escape or a right-to-left mark would show as another name in the panel."""
    listed = catalog.normalize_models(ids(["gpt-x\x1b[31m", "a‮b", "z​q", "gpt-ok-1"]))
    assert [m["id"] for m in listed] == ["gpt-ok-1"]


class Slow:
    """GET answers a body of `size` bytes, `step` bytes every `pause` seconds."""

    def __init__(self, size, step, pause):
        import time as _time

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                sent = 0
                try:
                    while sent < size:
                        self.wfile.write(b" " * step)
                        self.wfile.flush()
                        sent += step
                        if pause:
                            _time.sleep(pause)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()


def test_a_huge_model_list_is_refused_not_read_whole():
    """Vera B2: the list read has a size cap."""
    from hokma.engines.direct import wire

    s = Slow(wire.MAX_JSON_BYTES + 200_000, 64 * 1024, 0)
    try:
        with pytest.raises(wire.WireError) as info:
            wire.get_json(s.url + "/models", {})
    finally:
        s.stop()
    assert "larger than" in str(info.value)


def test_a_list_that_drips_is_cut_at_the_total_time():
    """Vera B2: one byte every 0.3 s never trips a per-read timeout; the total time does."""
    import time

    from hokma.engines.direct import wire

    s = Slow(10_000, 1, 0.3)
    start = time.monotonic()
    try:
        with pytest.raises(wire.WireError):
            wire.get_json(s.url + "/models", {}, timeout=1.5)
    finally:
        s.stop()
    assert time.monotonic() - start < 4


# ------------------------------------------------------------------ connect, chat, replace, manual, offline

def test_connect_reads_the_list_with_the_key_and_tests_the_model_the_chat_will_use(ready, env):
    p = Provider(GOOGLE_LIST, GOOGLE_WORKS)
    try:
        view = service.add_api("google", KEY, base_url=p.url)
        result = ping(view)
    finally:
        p.stop()
    assert p.gets[0]["path"] == "/models" and p.gets[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert result["ok"] and result["model"] == "gemini-3.5-flash-lite" == p.sent[0]
    c = saved(view)
    assert [catalog.tier_model({"engine": "api", "tier_models": c.tier_models}, t) for t in catalog.TIERS] == [
        "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.8-flash"]
    assert "gemini-3.6-flash" in [m["id"] for m in c.settings["models"]]          # the list is kept for the selector
    assert service.live_models.__name__ and "gemini-3.8-flash" in models.choices(view["id"], network=False)


def test_a_model_refused_in_the_chat_is_replaced_and_the_student_is_told(ready, env):
    """The saved Fast model is gone at the provider: the turn asks the list again, takes the next one, says so in the
    chat and answers. The replacement stays for the next turns and the notice does not come twice."""
    p = Provider(GOOGLE_LIST, GOOGLE_WORKS)
    try:
        view = service.add_api("google", KEY, base_url=p.url)
        ping(view)
        p.works.discard("gemini-3.5-flash-lite")                       # retired after it was saved
        events = run_turn(view, "fast")
    finally:
        p.stop()
    notices = [e["message"] for e in events if e["type"] == "engine_notice"]
    assert notices == ["The model gemini-3.5-flash-lite no longer exists; you now use gemini-3.8-flash."]
    assert events[-1]["type"] == "done" and "OK" in "".join(e.get("text", "") for e in events)
    c = saved(view)
    assert c.tier_models["fast"] == "gemini-3.8-flash" and "gemini-3.5-flash-lite" in c.settings["rejected"]
    assert models.take_notices(view["id"]) == []


def test_a_model_the_student_chose_is_never_replaced_and_the_chat_says_so(ready, env):
    p = Provider(GOOGLE_LIST, GOOGLE_WORKS)
    try:
        view = service.add_api("google", KEY, base_url=p.url, model="gemini-3.6-flash")
        ping(view)
        p.works.discard("gemini-3.6-flash")
        events = run_turn(view, "balanced")
    finally:
        p.stop()
    assert p.sent[0] == "gemini-3.6-flash" and set(p.sent) == {"gemini-3.6-flash"}
    assert events[-1]["type"] == "error" and events[-1]["code"] == "model_choice"


def test_at_start_a_gone_model_is_replaced_and_told_once_and_offline_keeps_the_saved_list(ready, env):
    p = Provider(GOOGLE_LIST, GOOGLE_WORKS)
    try:
        view = service.add_api("google", KEY, base_url=p.url)
        ping(view)
        p.listing = ids(["models/gemini-3.9-flash", "models/gemini-3.5-flash-lite"])   # the provider moved on
        service.refresh_all_tiers()
    finally:
        p.stop()
    assert saved(view).tier_models["balanced"] == "gemini-3.9-flash"
    told = models.take_notices(view["id"])
    assert told == ["The model gemini-3.8-flash no longer exists; you now use gemini-3.9-flash."]
    assert models.take_notices(view["id"]) == []
    # the provider is unreachable now (server stopped): the start keeps the last list and the levels
    service.refresh_all_tiers()
    c = saved(view)
    assert c.tier_models["balanced"] == "gemini-3.9-flash" and len(c.settings["models"]) == 2


def test_tiers_the_student_saves_while_the_list_is_read_are_kept(ready, env, monkeypatch):
    """Vera B2: the list takes a network round trip; a map the student saves meanwhile wins."""
    view = service.add_api("google", KEY, base_url="http://127.0.0.1:9")
    mine = {"fast": "gemini-mine", "balanced": "gemini-mine", "deep": "gemini-mine"}

    def slow_fetch(c):
        service.set_tiers(c.id, mine)
        return catalog.normalize_models(GOOGLE_LIST)

    monkeypatch.setattr(models, "fetch", slow_fetch)
    assert service.refresh_tiers(view["id"]) is None
    assert saved(view).tier_models == mine


def test_an_empty_map_goes_back_to_the_automatic_pick(ready, env):
    p = Provider(GOOGLE_LIST, GOOGLE_WORKS)
    try:
        view = service.add_api("google", KEY, base_url=p.url, model="gemini-3.6-flash")
        models.refresh(view["id"])
    finally:
        p.stop()
    assert saved(view).tier_models["balanced"] == "gemini-3.6-flash"          # the student's, kept
    service.set_tiers(view["id"], {})
    assert saved(view).tier_models["balanced"] == "gemini-3.8-flash"          # from the saved list, no network


def test_one_level_chosen_the_others_stay_automatic(ready, env):
    p = Provider(GOOGLE_LIST, GOOGLE_WORKS)
    try:
        view = service.add_api("google", KEY, base_url=p.url)
        ping(view)
        service.set_tiers(view["id"], {"fast": "gemini-3.6-flash", "balanced": "auto", "deep": "auto"})
        c = saved(view)
        assert c.tier_models["fast"] == "gemini-3.6-flash" and c.tier_models["balanced"] == "gemini-3.8-flash"
        assert models.pinned(c.tier_models) == {"fast"}
        p.listing = ids(["models/gemini-3.9-flash", "models/gemini-3.6-flash"])
        models.refresh(view["id"])
        c = saved(view)
        assert c.tier_models["fast"] == "gemini-3.6-flash" and c.tier_models["balanced"] == "gemini-3.9-flash"
        assert models.replace(view["id"], "fast", "gemini-3.6-flash") == (None, True)
    finally:
        p.stop()


def run_turn(view, tier):
    from hokma.engine.dbstore import DbChatStore
    from hokma.engines import secrets

    c = saved(view)
    conn = DbChatStore._conn(c)
    ctx = EngineContext(conversation_id=f"t-{tier}", model=catalog.tier_model(conn, tier) or "", cwd=str(env_dir()),
                        mode="auto", connection=conn, tier=tier, instructions="be brief")
    session = DirectSession(ctx, conn, secrets.get(c.id))

    async def go():
        return [ev async for ev in session.send([{"type": "text", "text": "hi"}])]

    return asyncio.run(go())


def env_dir():
    from hokma.core.config import hokma_home

    home = hokma_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


# ------------------------------------------------------------------ no model name in the engine code

ENGINE_CODE = [Path(__file__).resolve().parents[1] / "hokma" / p for p in ("engines", "engine")]
# Any provider (Vera C): no list of families. A model id as a string literal is one of these shapes:
#   a word, separators and a version number  (gpt-6.1-sol, grok-9, chatgpt-4o-latest, claude-sonnet-5-5, kimi-k3)
#   a letter and a number                      (o3, o4-mini)
#   a word with a model id suffix              (deepseek-chat, mistral-large-latest, any-model-instruct)
# CLI level aliases (haiku, sonnet, opus) and size words alone are none of these.
_ID = r"[a-z][a-z0-9]*(?:[-_/.][a-z0-9.]+)*"
MODEL_SHAPES = [
    re.compile(rf"""["']({_ID}[-_/.]v?\d[a-z0-9.\-_/]*|[a-z]\d+(?:-[a-z0-9.]+)*)["']""", re.I),
    re.compile(rf"""["']({_ID}-(?:chat|latest|instruct|reasoner|coder|preview|exp))["']""", re.I),
]
# Literals of the engine code with that shape that are not models (reviewed one by one): text encodings.
NOT_MODELS = {"utf-8", "latin-1"}


def model_literals(text: str) -> list[str]:
    return [m.group(1) for shape in MODEL_SHAPES for m in shape.finditer(text) if m.group(1).lower() not in NOT_MODELS]


def test_the_literal_check_catches_any_provider():
    for literal in ('"gpt-6.1-sol"', "'grok-9'", '"chatgpt-4o-latest"', '"claude-sonnet-5-5"', '"o3"', '"o4-mini"',
                    '"deepseek-chat"', '"mistral-large-latest"', '"kimi-k3.1-pro"', '"gemini-3.8-flash"',
                    '"llama-4-scout"', '"models/gemini-9-flash"'):
        assert model_literals(literal), literal
    for literal in ('"utf-8"', '"haiku"', '"fast"', '"stream-json"', '"anthropic-version"', '"2023-06-01"',
                    '"x-api-key"', '"flash-lite"'):
        assert not model_literals(literal), literal


def test_the_engine_code_names_no_model():
    """Requirement of 0.0.39: no provider has a model name in the code. Model names live only in the tests' doubles."""
    found = []
    for root in ENGINE_CODE:
        for path in sorted(list(root.rglob("*.py")) + list(root.rglob("*.json"))):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for literal in model_literals(line):
                    found.append(f"{path.name}:{n}: {literal}")
    assert not found, "model names in the engine code:\n" + "\n".join(found)
