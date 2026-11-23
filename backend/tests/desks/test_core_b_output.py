"""TA-35: structured output with degradation: invalid JSON degrades to text and the run goes on."""
import json

import pytest

from hokma.desks.core import output
from hokma.llm.protocol import LLMReply

SCHEMA = {"type": "object", "required": ["rating", "confidence"],
          "properties": {"rating": {"type": "string", "enum": ["buy", "hold", "sell"]},
                         "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                         "notes": {"type": "array", "items": {"type": "string"}}}}


class ScriptedLLM:
    model = "scripted"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def complete(self, messages, *, schema=None, max_tokens=4096):
        self.calls.append(schema)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return LLMReply(text=answer)


def test_valid_json_is_accepted_TA_35():
    result = output.parse_structured('{"rating": "buy", "confidence": 0.7}', SCHEMA, render=lambda v: f"{v['rating']} {v['confidence']}")
    assert not result.degraded and result.value == {"rating": "buy", "confidence": 0.7} and result.text == "buy 0.7"


def test_json_inside_a_fence_and_after_reasoning_is_accepted_TA_35():
    text = '<think>hmm {"rating": "sell"}</think>Here: ```json\n{"rating": "sell", "confidence": 1}\n```'
    assert output.parse_structured(text, SCHEMA).value["rating"] == "sell"


@pytest.mark.parametrize("text", ["I think you should buy.", '{"rating": "maybe", "confidence": 0.5}', '{"rating": "buy"}',
                                  '{"rating": "buy", "confidence": 3}', '{"rating": "buy", "confidence": "high"}',
                                  '{"rating": "buy", "confidence": 0.5, "notes": [1]}', "", "[1, 2]"])
def test_invalid_json_degrades_to_text_with_the_flag_TA_35(text):
    result = output.parse_structured(text, SCHEMA)
    assert result.degraded is True and result.value is None and result.text == text.strip() and result.warnings


def test_the_run_goes_on_after_a_degraded_answer_TA_35():
    llm = ScriptedLLM("not json at all", "free text answer")
    result = output.ask_structured(llm, [{"role": "user", "content": "rate it"}], SCHEMA)
    assert result.degraded and result.text == "free text answer" and result.value is None
    assert llm.calls == [SCHEMA, None]                                   # second try is free text, without the schema
    assert any("degraded" in w for w in result.warnings)


def test_a_valid_first_answer_makes_one_call_TA_35():
    llm = ScriptedLLM(json.dumps({"rating": "hold", "confidence": 0.2}))
    result = output.ask_structured(llm, [{"role": "user", "content": "rate it"}], SCHEMA)
    assert not result.degraded and result.value["rating"] == "hold" and len(llm.calls) == 1


def test_a_failed_structured_call_is_retried_as_free_text_TA_35():
    llm = ScriptedLLM(RuntimeError("provider rejected the schema"), "plain answer")
    result = output.ask_structured(llm, [{"role": "user", "content": "x"}], SCHEMA)
    assert result.degraded and result.text == "plain answer" and "RuntimeError" in result.warnings[0]


def test_two_failures_return_an_empty_degraded_result_without_raising_TA_35():
    llm = ScriptedLLM(RuntimeError("one"), RuntimeError("two"))
    result = output.ask_structured(llm, [{"role": "user", "content": "x"}], SCHEMA)
    assert result.degraded and result.text == "" and result.error == "two" and len(result.warnings) == 2


def test_schema_checks_nested_paths_TA_35():
    with pytest.raises(output.SchemaError) as info:
        output.validate_schema({"rating": "buy", "confidence": 0.5, "notes": ["a", 2]}, SCHEMA)
    assert info.value.path == ".notes[1]"
    output.validate_schema({"rating": "buy", "confidence": 1}, SCHEMA)           # an int is a number
    with pytest.raises(output.SchemaError):
        output.validate_schema(True, {"type": "integer"})                        # a boolean is not an integer
