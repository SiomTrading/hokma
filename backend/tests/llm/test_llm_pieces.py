"""AH-53, AH-54, TA-89, TA-88, AH-56, OA-68: JSON extraction, reasoning blocks, capabilities, parameters, providers, CLI adapters."""
import json

import pytest

from hokma.llm import LLMParseError, capabilities, extract_json, jsonx, params, registry
from hokma.llm import adapters as cli


# ---- AH-53 extract_json ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('  \n{"a": 1}\n ', {"a": 1}),
    ('Sure! Here it is:\n```json\n{"a": {"b": [1, 2]}}\n```\nAnything else?', {"a": {"b": [1, 2]}}),
    ('```\n{"a": 2}\n```', {"a": 2}),
    ('My answer is {"a": 3} and that is final.', {"a": 3}),
    ('prefix {"a": "text with } brace and \\" quote"} suffix', {"a": 'text with } brace and " quote'}),
    ('{not json} then {"a": 4}', {"a": 4}),
    ('{"outer": {"inner": 1}} {"second": 2}', {"outer": {"inner": 1}}),
])
def test_the_first_json_object_is_extracted_AH_53(text, expected):
    assert extract_json(text) == expected


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2, 3]", "42", '{"a": 1', "{{", None])
def test_an_answer_without_an_object_raises_AH_53(text):
    with pytest.raises(LLMParseError):
        extract_json(text)


def test_extract_json_reads_a_block_list_too_AH_53():
    blocks = [{"type": "thinking", "thinking": '{"a": "wrong"}'}, {"type": "text", "text": 'Result: {"a": "right"}'}]
    assert extract_json(blocks) == {"a": "right"}


# ---- AH-54 _flatten and reasoning blocks ----------------------------------------------------------------------------

def test_flatten_keeps_only_the_text_blocks_AH_54():
    blocks = [{"type": "thinking", "thinking": "let me think"}, {"type": "text", "text": "A"}, {"type": "tool_use", "name": "x"},
              "B", {"type": "text", "text": "C"}, {"type": "redacted_thinking", "data": "zzz"}]
    assert jsonx._flatten(blocks) == "A\nB\nC"
    assert jsonx._flatten(blocks, sep="") == "ABC" and jsonx.flatten is jsonx._flatten


@pytest.mark.parametrize("content,expected", [("plain", "plain"), (None, ""), ([], ""), ({"type": "text", "text": "one"}, "one"),
                                              ({"type": "thinking", "thinking": "x"}, ""), (5, "5"), ({"type": "output_text", "text": "o"}, "o")])
def test_flatten_handles_every_shape_AH_54(content, expected):
    assert jsonx._flatten(content) == expected


def test_thinking_tags_in_the_text_do_not_break_the_parse_AH_54():
    assert extract_json('<think>maybe {"a": 0}</think>\n{"a": 1}') == {"a": 1}
    assert extract_json('<thinking>\nstep\n</thinking>{"b": 2}') == {"b": 2}
    assert jsonx.strip_reasoning("<think>never closed {\"a\": 1}") == ""
    assert jsonx.strip_reasoning("before <think>x</think> after") == "before  after"


# ---- TA-89 capabilities ---------------------------------------------------------------------------------------------

def test_the_default_row_accepts_everything_TA_89():
    caps = capabilities.get_capabilities("some-new-model")
    assert caps == capabilities.DEFAULT and caps.supports_tool_choice and caps.supports_json_schema
    assert capabilities.get_capabilities(None) == capabilities.DEFAULT == capabilities.get_capabilities("")


@pytest.mark.parametrize("model", ["deepseek-v9-flash", "deepseek-reasoner", "deepseek/deepseek-v4-pro"])
def test_thinking_families_suppress_tool_choice_and_roundtrip_reasoning_TA_89(model):
    caps = capabilities.get_capabilities(model)
    assert caps.supports_tool_choice is False and caps.requires_reasoning_roundtrip is True
    assert capabilities.drop_unsupported(model, {"tool_choice": "auto", "x": 1}) == {"x": 1}


def test_a_third_party_finetune_keeps_the_default_TA_89():
    assert capabilities.get_capabilities("tngtech/deepseek-finetune") == capabilities.DEFAULT


def test_exact_ids_win_over_patterns_and_families_inherit_TA_89():
    assert capabilities.get_capabilities("deepseek-chat").supports_tool_choice is True
    assert capabilities.get_capabilities("MiniMax-M7").requires_reasoning_split is True
    assert capabilities.get_capabilities("claude-anything").supports_json_schema is False
    assert capabilities.get_capabilities("sonnet") == capabilities.CLAUDE == capabilities.get_capabilities("opus")
    assert capabilities.get_capabilities("llama-whatever").supports_tool_choice is False


def test_reasoning_openai_models_drop_temperature_TA_89():
    assert capabilities.drop_unsupported("o3-mini", {"temperature": 0.2, "max_tokens": 5}) == {"max_tokens": 5}
    assert capabilities.drop_unsupported("sonnet", {"temperature": 0.2}) == {"temperature": 0.2}


@pytest.mark.parametrize("model,want,expected", [("sonnet", True, "function_calling"), ("o3", True, "json_schema"),
                                                 ("llama-x", True, "json_mode"), ("llama-x", False, "json_mode"),
                                                 ("sonnet", False, "prompt_only"), ("brand-new", True, "function_calling")])
def test_the_structured_method_falls_back_to_what_the_model_supports_TA_89(model, want, expected):
    assert capabilities.structured_method(model, want_schema=want) == expected


# ---- TA-88 parameters per provider ----------------------------------------------------------------------------------

def test_each_provider_spells_the_same_wish_its_own_way_TA_88():
    config = {"temperature": 0.2, "llm_max_retries": 3, "max_tokens": 1000, "reasoning_level": "High"}
    assert params.provider_kwargs("google", config) == {"thinking_level": "high", "temperature": 0.2, "max_retries": 3, "max_output_tokens": 1000}
    assert params.provider_kwargs("openai", config) == {"reasoning_effort": "high", "temperature": 0.2, "max_retries": 3, "max_tokens": 1000}
    assert params.provider_kwargs("anthropic", config)["effort"] == "high"
    assert params.provider_kwargs("deepseek", config) == {"temperature": 0.2, "max_retries": 3, "max_tokens": 1000}   # no reasoning key


def test_unset_values_are_left_out_so_the_provider_keeps_its_default_TA_88():
    assert params.provider_kwargs("openai", {}) == {} == params.provider_kwargs("openai", {"temperature": None, "max_tokens": "", "reasoning_level": None})


@pytest.mark.parametrize("config", [{"temperature": 3}, {"temperature": -0.1}, {"max_tokens": 0}, {"max_tokens": True}, {"llm_max_retries": -1},
                                    {"llm_max_retries": "x"}, {"reasoning_level": "extreme"}])
def test_a_bad_value_fails_loudly_TA_88(config):
    with pytest.raises(ValueError):
        params.provider_kwargs("openai", config)


def test_a_level_a_provider_does_not_have_is_refused_TA_88():
    assert params.reasoning_level("anthropic", "medium") == "medium"
    with pytest.raises(ValueError):
        params.reasoning_level("anthropic", "minimal")
    assert params.reasoning_level("deepseek", "high") is None


# ---- AH-56 providers ------------------------------------------------------------------------------------------------

def test_the_registry_lists_engines_first_and_reads_the_catalog_AH_56():
    ids = [p.id for p in registry.providers()]
    assert ids[:3] == ["claude_code", "codex", "gemini_cli"] and {"openai", "anthropic", "google", "deepseek", "openrouter"} <= set(ids)
    assert len(ids) == len(set(ids))
    assert registry.get("claude_code").kind == "engine" and registry.get("openai").kind == "api" and registry.get("ollama").local is True
    assert registry.get("nope") is None and registry.is_supported("google") and not registry.is_supported("nope")


@pytest.mark.parametrize("model,provider", [("claude-x", "anthropic"), ("sonnet", "anthropic"), ("gpt-x", "openai"), ("o3", "openai"),
                                            ("gemini-x", "google"), ("deepseek-chat", "deepseek"), ("kimi-k", "moonshot"),
                                            ("meta/llama-3", "openrouter"), ("my-local-model", None), ("", None), (None, None)])
def test_the_provider_is_read_from_the_shape_of_the_id_AH_56(model, provider):
    assert registry.provider_for(model) == provider


def test_the_engine_kind_of_a_provider_AH_56():
    assert registry.engine_kind_for("codex") == "codex" and registry.engine_kind_for("deepseek") == "api" and registry.engine_kind_for(None) == "api"


# ---- OA-68 CLI adapters ---------------------------------------------------------------------------------------------

def test_claude_headless_command_OA_68():
    argv = cli.compose_headless("claude_code", cli.HeadlessRequest("Review the desk", model="sonnet"))
    assert argv == ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", "sonnet", "--", "Review the desk"]
    assert cli.compose_headless("claude_code", cli.HeadlessRequest("x", resume="abc"), binary_path="/opt/claude")[:1] == ["/opt/claude"]
    assert "--resume" in cli.compose_headless("claude_code", cli.HeadlessRequest("x", resume="abc"))
    assert "--continue" in cli.compose_headless("claude_code", cli.HeadlessRequest("x", resume="last"))


def test_codex_and_gemini_commands_OA_68():
    assert cli.compose_headless("codex", cli.HeadlessRequest("Do it", model="m")) == ["codex", "exec", "--model", "m", "--", "Do it"]
    assert cli.compose_headless("codex", cli.HeadlessRequest("Do it", resume="sess-1")) == ["codex", "exec", "resume", "sess-1", "--", "Do it"]
    assert cli.compose_headless("codex", cli.HeadlessRequest("Do it", resume="last"))[:4] == ["codex", "exec", "resume", "--last"]
    assert cli.compose_headless("gemini_cli", cli.HeadlessRequest("Do it")) == ["gemini", "--prompt=Do it"]


def test_a_prompt_that_starts_with_a_dash_is_never_an_option_OA_68():
    for kind in cli.SPECS:
        argv = cli.compose_headless(kind, cli.HeadlessRequest("--dangerous -x"))
        position = next(i for i, a in enumerate(argv) if "--dangerous" in a)
        assert argv[position].startswith("--prompt=") or argv[position - 1] == "--"


@pytest.mark.parametrize("kind,request_", [("gemini_cli", cli.HeadlessRequest("x", resume="id")), ("gemini_cli", cli.HeadlessRequest("x", resume="last")),
                                           ("claude_code", cli.HeadlessRequest("  ")), ("api", cli.HeadlessRequest("x"))])
def test_what_the_cli_cannot_do_is_an_error_OA_68(kind, request_):
    with pytest.raises(cli.AdapterError):
        cli.compose_headless(kind, request_)


def test_capabilities_and_availability_OA_68(tmp_path):
    assert cli.SPECS["claude_code"].capabilities.resume_by_id and not cli.SPECS["gemini_cli"].capabilities.resume_last
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    assert cli.available(str(tmp_path)) == ["claude_code"]
    assert set(cli.SPECS) <= set(registry.ENGINE_KINDS)


def test_json_round_trip_of_a_command_is_plain_text_OA_68():
    assert json.loads(json.dumps(cli.compose_headless("codex", cli.HeadlessRequest("x"))))[0] == "codex"
