"""AH-50, AH-49, TA-87, TA-90, AH-52: the client protocol, the retry, the cache and the factory, on a fake engine (no network)."""
import ast
import asyncio
import pathlib

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from llm.support import FakeAdapter, error_turn, text_turn

import hokma.llm as llm_pkg
from hokma.engines.base import EngineContext
from hokma.llm import ChatLLM, LLMClient, LLMError, make_llm
from hokma.llm.cache import CachedClient, PromptCache, messages_key, prompt_key
from hokma.llm.clients import AnthropicClient, LocalClient, OpenAIClient, client_class
from hokma.llm.factory import Resolved
from hokma.llm.retry import RetryPolicy, call_with_retry, coerce_max_retries, coerce_max_tokens, is_retryable

USER = [{"role": "user", "content": "Rate NQ"}]


def context(model="sonnet"):
    return EngineContext(conversation_id="", model=model, cwd=".", agent="desk-chief", mode="ask", tier="balanced")


def chat(adapter, **kwargs):
    kwargs.setdefault("sleep", lambda s: None)
    return ChatLLM(adapter, context(), **kwargs)


# ---- AH-50: the protocol --------------------------------------------------------------------------------------------

def test_chat_llm_follows_the_protocol_and_returns_text_and_usage_AH_50():
    adapter = FakeAdapter(text_turn("Hello, ", "world", usage={"tokens_in": 5, "tokens_out": 2}))
    client = chat(adapter)
    assert isinstance(client, LLMClient)
    reply = client.complete(USER, max_tokens=100)
    assert reply.text == "Hello, world" and reply.usage == {"tokens_in": 5, "tokens_out": 2} and reply.model == "sonnet"
    assert reply.attempts == 1 and reply.cached is False and adapter.sessions[0].closed
    assert adapter.sessions[0].sent == [[{"type": "text", "text": "Rate NQ"}]]


def test_system_messages_and_history_go_to_the_session_not_the_prompt_AH_50():
    adapter = FakeAdapter()
    chat(adapter).complete([{"role": "system", "content": "Be brief."}, {"role": "user", "content": "q1"},
                            {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}])
    ctx = adapter.contexts[0]
    assert "Be brief." in ctx.instructions and ctx.history == [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    assert adapter.sessions[0].sent == [[{"type": "text", "text": "q2"}]]


def test_a_schema_is_asked_for_and_the_json_is_parsed_AH_50():
    schema = {"type": "object", "properties": {"rating": {"type": "string"}}}
    adapter = FakeAdapter(text_turn('<think>x</think>{"rating": "buy"}'))
    reply = chat(adapter).complete(USER, schema=schema)
    assert reply.parsed == {"rating": "buy"} and reply.json() == {"rating": "buy"}
    assert "JSON schema" in adapter.contexts[0].instructions and '"rating"' in adapter.contexts[0].instructions


def test_a_tool_permission_request_is_refused_AH_50():
    adapter = FakeAdapter([{"type": "permission_request", "request_id": "r1", "tool": "Bash", "input": {}}, *text_turn("done")])
    reply = chat(adapter).complete(USER)
    assert reply.text == "done" and adapter.sessions[0].answers == [("r1", False, "a model call cannot use tools")]


def test_the_final_result_is_used_when_no_text_was_streamed_AH_50():
    adapter = FakeAdapter([{"type": "done", "status": "complete", "result": "from result"}])
    assert chat(adapter).complete(USER).text == "from result"


def test_the_client_works_inside_a_running_event_loop_AH_50():
    async def main():
        return chat(FakeAdapter(text_turn("inside"))).complete(USER).text

    assert asyncio.run(main()) == "inside"


@pytest.mark.parametrize("messages", [[], [{"role": "assistant", "content": "x"}], [{"role": "system", "content": "x"}]])
def test_a_conversation_must_end_with_a_user_message_AH_50(messages):
    with pytest.raises(ValueError):
        chat(FakeAdapter()).complete(messages)


# ---- TA-87: retries and tokens --------------------------------------------------------------------------------------

def test_a_failure_that_can_pass_is_retried_with_backoff_and_succeeds_TA_87():
    adapter = FakeAdapter(error_turn("network", "reset"), error_turn("quota", "busy"), text_turn("third time"))
    waits = []
    client = ChatLLM(adapter, context(), policy=RetryPolicy(max_retries=3, base_delay=1.0, factor=2.0, max_delay=30), sleep=waits.append)
    reply = client.complete(USER)
    assert reply.text == "third time" and reply.attempts == 3 and waits == [1.0, 2.0] and len(adapter.sessions) == 3
    assert all(s.closed for s in adapter.sessions)


def test_retries_stop_at_the_cap_and_the_last_error_propagates_TA_87():
    adapter = FakeAdapter(error_turn("network", "down"))
    waits = []
    client = ChatLLM(adapter, context(), policy=RetryPolicy(max_retries=2, base_delay=1, max_delay=1.5), sleep=waits.append)
    with pytest.raises(LLMError) as info:
        client.complete(USER)
    assert info.value.code == "network" and len(adapter.sessions) == 3 and waits == [1.0, 1.5]       # the wait never exceeds the cap


@pytest.mark.parametrize("code", ["auth", "region", "model", "claude_not_found", "no_engine"])
def test_a_failure_that_cannot_pass_is_not_retried_TA_87(code):
    adapter = FakeAdapter(error_turn(code))
    waits = []
    with pytest.raises(LLMError) as info:
        ChatLLM(adapter, context(), sleep=waits.append).complete(USER)
    assert info.value.code == code and len(adapter.sessions) == 1 and waits == [] and is_retryable(code) is False


def test_the_policy_and_the_validators_TA_87():
    policy = RetryPolicy(max_retries=5, base_delay=2, factor=3, max_delay=20)
    assert [policy.delay(n) for n in (1, 2, 3, 4)] == [2, 6, 18, 20]
    assert RetryPolicy(jitter=0.5, base_delay=4, max_delay=100).delay(1, rng=lambda: 1.0) == 6.0
    assert [coerce_max_retries(v) for v in (0, "3", 2.0)] == [0, 3, 2] and coerce_max_tokens("2048") == 2048
    for bad in (True, -1, "x", 1.5, 11, None):
        with pytest.raises(ValueError):
            coerce_max_retries(bad)
    for bad in (True, 0, -5, "x", 2.5, 10**7):
        with pytest.raises(ValueError):
            coerce_max_tokens(bad)
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=-1)


def test_call_with_retry_uses_the_code_not_the_class_TA_87():
    seen = []

    def flaky():
        seen.append(1)
        if len(seen) < 2:
            raise OSError("x")
        return "ok"

    assert call_with_retry(flaky, code_of=lambda e: "network", sleep=lambda s: None) == "ok"
    with pytest.raises(OSError):
        call_with_retry(lambda: (_ for _ in ()).throw(OSError("x")), code_of=lambda e: "auth", sleep=lambda s: None)


# ---- AH-49: cache and audit -----------------------------------------------------------------------------------------

def test_prompt_keys_are_stable_and_distinguish_every_part_AH_49():
    base = prompt_key("a", "m", "s", "u")
    assert base == prompt_key("a", "m", "s", "u") and len(base) == 24
    assert len({base, prompt_key("b", "m", "s", "u"), prompt_key("a", "n", "s", "u"), prompt_key("a", "m", "t", "u"), prompt_key("a", "m", "s", "v")}) == 5
    assert messages_key("a", "m", [{"role": "user", "content": "x", "z": 1}]) == messages_key("a", "m", [{"z": 1, "content": "x", "role": "user"}])
    assert messages_key("a", "m", USER, {"type": "object"}) != messages_key("a", "m", USER, None)


def test_the_cache_stores_reads_and_audits_AH_49(tmp_path):
    cache = PromptCache(tmp_path / "llm")
    key = prompt_key("a", "m", "s", "u")
    assert cache.get(key) is None and cache.misses == 1
    cache.put(key, {"text": "answer"}, agent="a", model="m")
    record = cache.get(key, agent="a", model="m")
    assert record["text"] == "answer" and "created_at" in record and cache.hits == 1
    rows = cache.audit_rows()
    assert [r["event"] for r in rows] == ["store", "hit"] and rows[0]["key"] == key and rows[0]["agent"] == "a" and "at" in rows[0]
    assert not list((tmp_path / "llm").glob("*.tmp"))


def test_a_corrupt_entry_is_a_miss_and_is_rewritten_AH_49(tmp_path):
    cache = PromptCache(tmp_path)
    key = prompt_key("a", "m", "s", "u")
    (tmp_path / f"{key}.json").write_text("{broken", encoding="utf-8")
    assert cache.get(key) is None
    cache.put(key, {"text": "fresh"})
    assert cache.get(key)["text"] == "fresh"
    (tmp_path / "audit.jsonl").write_text("not json\n" + (tmp_path / "audit.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    assert len(cache.audit_rows()) >= 2


@pytest.mark.parametrize("key", ["", "../x", "ABC", "g" * 5, "a" * 65])
def test_a_bad_cache_key_is_refused_AH_49(tmp_path, key):
    with pytest.raises(ValueError):
        PromptCache(tmp_path).get(key)


def test_an_identical_question_is_answered_from_the_cache_without_the_engine_AH_49(tmp_path):
    adapter = FakeAdapter(text_turn("first answer", usage={"tokens_out": 4}), text_turn("second answer"))
    client = CachedClient(chat(adapter), PromptCache(tmp_path), agent="desk-chief")
    one, two = client.complete(USER), client.complete(USER)
    assert one.text == two.text == "first answer" and one.cached is False and two.cached is True and two.usage == {"tokens_out": 4}
    assert len(adapter.sessions) == 1
    assert client.complete([{"role": "user", "content": "other"}]).text == "second answer" and len(adapter.sessions) == 2


def test_an_empty_answer_is_never_cached_AH_49(tmp_path):
    adapter = FakeAdapter([{"type": "done", "status": "complete"}], text_turn("now text"))
    client = CachedClient(chat(adapter), PromptCache(tmp_path), agent="a")
    assert client.complete(USER).text == "" and client.complete(USER).text == "now text"


# ---- TA-90 and AH-52: clients and the factory -----------------------------------------------------------------------

def test_each_provider_has_its_client_and_unknown_ones_speak_the_openai_wire_TA_90():
    assert client_class("anthropic") is AnthropicClient and client_class("openai") is OpenAIClient
    assert client_class("ollama") is LocalClient and client_class("lmstudio") is LocalClient
    assert client_class("custom") is OpenAIClient and client_class(None) is OpenAIClient
    from hokma.llm.clients import CLIENTS
    assert len(CLIENTS) == 7


def test_a_client_carries_the_parameters_its_provider_takes_TA_90():
    config = {"temperature": 0.2, "max_tokens": 900, "llm_max_retries": 4, "reasoning_level": "high"}
    google = client_class("google")(FakeAdapter(), context("gemini-x"), config=config)
    assert google.params == {"thinking_level": "high", "temperature": 0.2, "max_retries": 4, "max_output_tokens": 900}
    assert google.policy.max_retries == 4 and google.provider == "google"
    reasoning = client_class("openai")(FakeAdapter(), context("o3"), config=config)
    assert "temperature" not in reasoning.params and reasoning.params["reasoning_effort"] == "high"
    assert client_class("anthropic")(FakeAdapter(), context("sonnet")).capabilities.supports_json_schema is False


def test_the_params_reach_the_engine_session_and_the_setting_is_a_ceiling_TA_90():
    adapter = FakeAdapter()
    client = client_class("openai")(adapter, context("gpt-x"), config={"max_tokens": 777, "temperature": 0.3})
    client.complete(USER, max_tokens=50)
    client.complete(USER, max_tokens=5000)
    assert [c.extra["max_tokens"] for c in adapter.contexts] == [50, 777] and adapter.contexts[0].extra["temperature"] == 0.3


def test_make_llm_resolves_the_engine_and_returns_a_client_AH_52(tmp_path):
    adapter, built = FakeAdapter(text_turn("hi")), []
    conn = {"id": 3, "engine": "api", "provider": "deepseek", "label": "DeepSeek"}

    def resolver(agent, desk_id):
        assert (agent, desk_id) == ("desk-chief", 9)
        return Resolved(conn, "deepseek-chat", "balanced", str(tmp_path))

    client = make_llm("desk-chief", desk_id=9, resolver=resolver, adapter_factory=lambda kind: built.append(kind) or adapter)
    assert built == ["api"] and client.model == "deepseek-chat" and client.provider == "deepseek"
    assert client.complete(USER).text == "hi"
    ctx = adapter.contexts[0]
    assert ctx.connection == conn and ctx.agent == "desk-chief" and ctx.cwd == str(tmp_path) and ctx.extra["desk_id"] == 9


def test_make_llm_without_a_connection_is_claude_code_and_can_cache_AH_52(tmp_path):
    adapter, built = FakeAdapter(text_turn("cached?")), []
    client = make_llm("risk", resolver=lambda a, d: Resolved(None, "sonnet", "balanced", "."),
                      adapter_factory=lambda kind: built.append(kind) or adapter, cache=PromptCache(tmp_path))
    assert built == ["claude_code"] and isinstance(client, CachedClient) and client.model == "sonnet"
    client.complete(USER)
    assert client.complete(USER).cached is True


def test_make_llm_reads_the_database_when_given_no_resolver_AH_52(db_with_connection):
    adapter = FakeAdapter(text_turn("from db"))
    client = make_llm("desk-chief", adapter_factory=lambda kind: adapter)
    assert client.complete(USER).text == "from db" and adapter.contexts[0].agent == "desk-chief"


@pytest.fixture
def db_with_connection(env, monkeypatch):
    from hokma.db import get_database, migrate
    from hokma.engines import effective

    migrate()
    with get_database().write() as u:
        u.engine_connections.add("api", "DeepSeek", "api_key", provider="deepseek", wire="openai", base_url="https://api.deepseek.com",
                                 tier_models={"balanced": "deepseek-chat", "fast": "deepseek-chat", "deep": "deepseek-chat"})
    monkeypatch.setattr(effective, "installed", lambda kind: True)
    monkeypatch.setattr(effective, "claude_connected", lambda: False)
    with get_database().write() as u:
        conn = u.engine_connections.default()
        u.engine_connections.set_status(conn.id, "connected", None)
    return get_database()


# ---- no HTTP client of its own --------------------------------------------------------------------------------------

FORBIDDEN = {"httpx", "requests", "urllib", "urllib3", "http", "aiohttp", "socket", "websockets", "websocket", "ssl", "anthropic",
             "openai", "google", "boto3", "ollama", "pycurl", "httpcore"}


def test_hokma_llm_creates_no_http_client_of_its_own_TA_90():
    package = pathlib.Path(llm_pkg.__file__).parent
    offenders = []
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 else [])
            offenders += [f"{path.name}: {n}" for n in names if n.split(".")[0] in FORBIDDEN]
    assert offenders == []


def test_the_scan_would_catch_an_http_client_TA_90():
    tree = ast.parse("import httpx\nfrom urllib.request import urlopen")
    found = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    found += [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert [n for n in found if n.split(".")[0] in FORBIDDEN] == ["httpx", "urllib.request"]
