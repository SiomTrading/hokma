"""Persona base (AH-03): prompt with the common rules, computed criteria, failure contract and cache."""
import json
from datetime import datetime, timezone

import pytest

from hokma.desks.core import persona as p
from hokma.desks.core.prompts import PromptComposer, Piece
from hokma.desks.core.signal import Criterion, QuantModel, SignalContext, contains_execution_terms

AS_OF = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
CRIT = (Criterion("roe_10y_min", 0.21, 0.15, True, "metrics.mean"), Criterion("debt_to_equity", None, 1.0, None, "data.fundamentals"))


def ctx(**over):
    base = dict(source="A16", desk_id=3, line="LP", symbol="AAPL", as_of=AS_OF, horizon="years", snapshot_hash="h1",
                facts={"revenue_growth": 0.12}, criteria=CRIT)
    base.update(over)
    return SignalContext(**base)


class Quality(p.PersonaAgent):
    code, name = "A16", "quality"
    voice = "You judge businesses by the durability of their returns on capital and the strength of their balance sheet."
    confidence_cap = 90


class Fake:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def complete(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.answer


GOOD = json.dumps({"signal": "bullish", "confidence": 80, "reasoning": "Returns are durable. The balance sheet is sound. Valuation is fair."})


def test_prompt_holds_common_rules_and_computed_criteria_AH_03():
    model = Fake(GOOD)
    agent = Quality(model)
    agent.signal(ctx())
    system, user = model.calls[0]
    for marker in ("## Rule: citations", "## Rule: source-policy", "## Rule: no-lookahead", "## Rule: scope-honesty", "## Language", "## Confidence scale"):
        assert marker in system
    assert "Write your entire final answer in" in system and "durability of their returns" in system
    assert "- roe_10y_min: 0.21, threshold 0.15, passed (source: metrics.mean)" in user
    assert "debt_to_equity: not available" in user and "AAPL" in user and "revenue_growth" in user


def test_prompt_has_no_sizing_or_exit_instruction_AH_03():
    model = Fake(GOOD)
    Quality(model).signal(ctx())
    system, user = model.calls[0]
    assert contains_execution_terms(system) is None
    assert contains_execution_terms(user) is None


def test_language_follows_the_owner_AH_03():
    model = Fake(GOOD)
    Quality(model, language="es").signal(ctx())
    assert "in Spanish" in model.calls[0][0]


def test_view_is_built_from_the_answer_AH_03():
    s = Quality(Fake(GOOD)).signal(ctx())
    assert s.value == pytest.approx(0.8) and s.confidence == 80 and not s.abstained
    assert s.criteria == CRIT and s.snapshot_hash == "h1" and s.source == "A16"
    assert isinstance(Quality(Fake(GOOD)), QuantModel)
    bear = json.dumps({"signal": "bearish", "confidence": 55, "reasoning": "Returns are fading. Debt is rising."})
    assert Quality(Fake(bear)).signal(ctx()).value == pytest.approx(-0.55)
    neutral = json.dumps({"signal": "neutral", "confidence": 30, "reasoning": "No edge here. Evidence is mixed."})
    s = Quality(Fake(neutral)).signal(ctx())
    assert s.value == 0.0 and not s.abstained


def test_confidence_is_capped_by_the_profile_AH_03():
    big = json.dumps({"signal": "bullish", "confidence": 99, "reasoning": "Strong. Very strong."})
    s = Quality(Fake(big)).signal(ctx())
    assert s.confidence == 90 and s.value == pytest.approx(0.9)
    assert Quality(Fake(big)).signal(ctx(confidence_cap=60)).confidence == 60


def test_failed_call_is_an_abstention_not_a_neutral_view_AH_03():
    s = Quality(Fake(error=RuntimeError("timeout"))).signal(ctx())
    assert s.abstained and s.value is None and s.data_missing == ("model_call",)


@pytest.mark.parametrize("answer", ["not json at all", '{"signal": "maybe", "confidence": 5, "reasoning": "A. B."}',
                                    '{"signal": "bullish", "confidence": 500, "reasoning": "A. B."}', "[1, 2]"])
def test_unreadable_answer_is_an_abstention_AH_03(answer):
    s = Quality(Fake(answer)).signal(ctx())
    assert s.abstained and s.data_missing == ("model_answer",)


def test_reason_too_short_or_with_forbidden_text_abstains_AH_03():
    short = json.dumps({"signal": "bullish", "confidence": 60, "reasoning": "Looks good."})
    assert Quality(Fake(short)).signal(ctx()).data_missing == ("reason",)
    bad = json.dumps({"signal": "bullish", "confidence": 60, "reasoning": "Good business. Put a stop below the low."})
    assert Quality(Fake(bad)).signal(ctx()).data_missing == ("reason_content",)


def test_long_reason_is_cut_to_four_sentences_AH_03():
    long = json.dumps({"signal": "bullish", "confidence": 60, "reasoning": "One. Two. Three. Four. Five. Six."})
    assert Quality(Fake(long)).signal(ctx()).reason == "One. Two. Three. Four."


def test_required_criterion_missing_abstains_without_a_model_call_AH_03():
    model = Fake(GOOD)
    s = Quality(model).signal(ctx(required=("debt_to_equity",)))
    assert s.abstained and s.data_missing == ("debt_to_equity",) and model.calls == []


def test_fenced_json_is_read_AH_03():
    fenced = f"Here it is:\n```json\n{GOOD}\n```"
    assert Quality(Fake(fenced)).signal(ctx()).value == pytest.approx(0.8)


def test_unchanged_snapshot_never_pays_for_a_second_call_AH_03():
    model = Fake(GOOD)
    agent = Quality(model)
    a, b = agent.signal(ctx()), agent.signal(ctx())
    assert a == b and len(model.calls) == 1
    agent.signal(ctx(facts={"revenue_growth": 0.30}))
    assert len(model.calls) == 2


def test_each_agent_has_its_own_cache_and_a_desk_piece_stays_in_its_desk_AH_03():
    one, two = Fake(GOOD), Fake(GOOD)
    comp = PromptComposer()
    comp.add_piece(Piece("house-view", "Desk three prefers cash generative companies.", desk_id=3))
    Quality(one, composer=comp).signal(ctx(desk_id=3))
    Quality(two, composer=comp).signal(ctx(desk_id=9))
    assert "cash generative" in one.calls[0][0] and "cash generative" not in two.calls[0][0]


def test_persona_needs_code_name_and_voice():
    class Empty(p.PersonaAgent):
        pass

    with pytest.raises(ValueError):
        Empty(Fake(GOOD))
