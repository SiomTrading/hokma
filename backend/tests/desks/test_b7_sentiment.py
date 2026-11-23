"""Sam, the sentiment analyst (SPEC-019: B7): TA-02 and the scoring skill ECT-070."""
from datetime import datetime, timezone

import pytest
from b7_support import AS_OF, ScriptedChat, answer, call, cited_calculations, load_registry, piece_ids, skill_text

from hokma.data import sentinels
from hokma.desks.agents import analyst_kit as kit
from hokma.desks.agents import sentiment
from hokma.desks.core.tools import ToolNotAllowed
from hokma.quant import registry

DOCS = [
    {"received_at": "2024-06-13T10:00:00Z", "published_at": "2024-06-13T09:59:00Z", "p_positive": 0.2, "p_negative": 0.6},
    {"received_at": "2024-06-13T12:00:00Z", "published_at": "2024-06-13T11:00:00Z", "p_positive": 0.1, "p_negative": 0.7},
    {"received_at": "2024-06-14T00:10:00Z", "published_at": "2024-06-13T23:50:00Z", "p_positive": 0.3, "p_negative": 0.5},
]
SNAPSHOT = {"symbol": "AAPL", "as_of": "2024-06-14T20:00:00+00:00",
            "sections": {"news": {"status": "ok", "value": {"score": 0.35, "items": 12}},
                         "social": {"status": "ok", "value": {"score": -0.45, "items": 40}},
                         "cot": {"status": "no_data", "value": None}}}


@pytest.fixture(autouse=True)
def registry_ready():
    load_registry()


def report(**patch):
    base = {
        "summary": "Mood is negative in the posts and positive in the news, so the sources disagree.",
        "score": {"value": -0.45, "sample_size": 40, "window_days": 7},
        "sources": [{"name": "news", "score": 0.35, "sample_size": 12}, {"name": "social", "score": -0.45, "sample_size": 40}],
        "divergence": {"state": "diverge", "note": "News lean positive and social posts lean negative."},
        "event_cross": {"event_date": "2024-06-20", "relation": "before_event", "note": "The reading was taken six days before the event."},
        "data_gaps": ["cot"],
    }
    return {**base, **patch}


def run_sam(body, **kwargs):
    model = ScriptedChat(call("quant_call", name="stats.sentiment_score", params={"docs": DOCS}), answer(body))
    return model, sentiment.run(model, desk_id=1, symbol="aapl", as_of=AS_OF, snapshot=SNAPSHOT, **kwargs)


def test_TA_02_sam_gives_a_score_with_its_sample_size_and_the_divergence_between_sources():
    model, result = run_sam(report())
    assert result.valid, result.check.problems
    assert result.value["score"]["sample_size"] == 40 and result.value["divergence"]["state"] == "diverge"
    text = result.text
    assert "from 40 items" in text and "Sources diverge" in text and "cot" in result.report.data_gaps
    assert model.calls[0]["tools"] == ["data_snapshot", "quant_call"]


def test_TA_02_in_swo_the_reading_is_crossed_with_the_date_of_the_event():
    _, result = run_sam(report())
    assert result.valid and "Event 2024-06-20: before event" in result.text
    _, wrong = run_sam(report(event_cross={"event_date": "2024-06-10", "relation": "before_event", "note": "Wrong side."}))
    assert not wrong.valid and any("event_cross" in p for p in wrong.check.problems)
    _, none = run_sam(report(event_cross={"event_date": None, "relation": "no_event_known", "note": "No event is known."}))
    assert none.valid
    assert sentiment.event_relation("2024-06-14", AS_OF) == "after_event" and sentiment.event_relation("2024-06-15", AS_OF) == "before_event"
    assert sentiment.event_relation(None, AS_OF) == "no_event_known"


def test_TA_02_a_divergence_that_the_scores_do_not_support_is_rejected():
    _, result = run_sam(report(divergence={"state": "agree", "note": "All sources lean the same way."}))
    assert not result.valid and any("divergence" in p for p in result.check.problems)
    assert sentiment.divergence_state({"a": 0.2, "b": -0.1}) == "diverge" and sentiment.divergence_state({"a": 0.2, "b": 0.4}) == "agree"
    assert sentiment.divergence_state({"a": 0.2, "b": None}) == "mixed" and sentiment.divergence_state({"a": None}) == "unknown"
    assert sentiment.divergence_state({}) == "unknown"


def test_TA_02_the_output_has_no_entry_signal_and_a_score_always_carries_its_sample():
    for field in ("signal", "entry"):
        _, result = run_sam({**report(), field: "buy"})
        assert not result.valid and any(field in p for p in result.check.problems), field
    assert set(sentiment.SCHEMA["properties"]) == {"summary", "score", "sources", "divergence", "event_cross", "data_gaps"}
    _, no_sample = run_sam(report(score={"value": -0.45, "sample_size": None, "window_days": 7}))
    assert not no_sample.valid and any("sample" in p for p in no_sample.check.problems)


def test_TA_02_sources_enter_only_through_the_data_module_and_only_the_text_categories():
    box = sentiment.toolbox(kit.RunContext(1, "AAPL", AS_OF, "SWO"), sources={})
    refused = box.call("data_snapshot", {"symbol": "AAPL", "as_of": "2024-06-14", "categories": ["market"]})
    assert not refused.ok and refused.error_kind == "bad_input" and "news, social, cot" in refused.message
    got = {}

    def source(name):
        def read(symbol, cut):
            got[name] = (symbol, cut)
            return sentinels.ok({"score": 0.1}, source=f"fake-{name}", as_of=cut, received_at=cut)
        return read

    fakes = {name: source(name) for name in ("news", "social", "cot")}
    ok = sentiment.toolbox(kit.RunContext(1, "AAPL", AS_OF, "SWO"), sources=fakes).call(
        "data_snapshot", {"symbol": "aapl", "as_of": "2024-06-14T20:00:00Z", "categories": ["news", "social", "cot"]})
    assert ok.ok and set(ok.data["sections"]) == {"news", "social", "cot"} and ok.data["content_hash"]
    assert set(got) == {"news", "social", "cot"} and got["news"][0] == "AAPL"
    assert set(sentiment.data_sources()) == {"news", "social", "cot"}


def test_TA_02_a_number_that_no_module_or_snapshot_returned_is_unverified_and_does_not_validate():
    _, result = run_sam(report(summary="Mood fell 17.25 points this week."))
    assert not result.valid and result.check.unverified == ("17.25",)
    _, invented = run_sam(report(score={"value": -0.31, "sample_size": 40, "window_days": 7}))
    assert not invented.valid and any("-0.31" in p for p in invented.check.problems)


def test_TA_02_sam_gets_only_the_tools_of_his_role():
    box = sentiment.toolbox(kit.RunContext(1, "AAPL", AS_OF, "SWO"))
    assert box.names() == ("data_snapshot", "quant_call")
    for other in ("data_bars", "data_macro", "data_filings", "calendar_events"):
        with pytest.raises(ToolNotAllowed):
            box.call(other, {})
    blocked = box.call("quant_call", {"name": "indicators.screens.vcp", "params": {}})
    assert not blocked.ok and blocked.error_kind == "blocked"


def test_ECT_070_the_scoring_skill_is_in_the_prompt_of_sam_and_every_call_it_names_exists():
    assert "sentiment-scoring" in [s.name for s in kit.load_seat_file("A02").skills]
    assert "## Skill: sentiment-scoring\n" in sentiment.system_prompt(line="SWO", desk_id=1, as_of=AS_OF)
    assert "ECT-070" in piece_ids("stats.sentiment_score") and "ECT-070" in piece_ids("stats.mention_volume_zscore")
    assert cited_calculations("sentiment-scoring") <= {s.name for s in registry.list()}
    assert "publication time" in skill_text("sentiment-scoring")


def test_ECT_070_sam_reaches_the_daily_score_by_reception_time_and_an_empty_input_is_no_value_not_a_crash():
    box = sentiment.toolbox(kit.RunContext(1, "AAPL", AS_OF, "SWO"))
    scored = box.call("quant_call", {"name": "stats.sentiment_score", "params": {"docs": DOCS}})
    assert scored.ok and scored.data["value"]["date_basis"] == "received_at" and scored.data["value"]["dates"] == ["2024-06-13", "2024-06-14"]
    assert scored.data["value"]["score"][0] == pytest.approx(((0.2 - 0.6) + (0.1 - 0.7)) / 2, abs=1e-12)     # S = P(positive) - P(negative), per day
    empty = box.call("quant_call", {"name": "stats.sentiment_score", "params": {"docs": []}})
    assert empty.ok and empty.data["value"] is None and empty.data["warnings"]
