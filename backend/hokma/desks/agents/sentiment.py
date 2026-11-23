"""Sam, the sentiment analyst (SPEC-019: TA-02; skill ECT-070).

Sam reports a score with the size of its sample, the score of each source and whether the sources agree, and crosses the reading with
the date of the next event. Every source reaches him through the data module (news, social posts, positioning reports); the score
and the abnormal volume of mentions come from the statistics module. He gives no signal.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping

from hokma.desks.agents.analyst_kit import AnalystAgent, EvidenceLog, OutputSpec, RunContext, merge_results
from hokma.desks.plantel import AnalystReport

CODE = "A02"
KEY = "sentiment"
SOURCES = ("news", "social", "cot", "mentions")
DIVERGENCE = ("agree", "mixed", "diverge", "unknown")
RELATIONS = ("before_event", "after_event", "no_event_known")
SNAPSHOT_CATEGORIES = ("news", "social", "cot")

_TEXT = {"type": "string", "maxLength": 600}
_NUM = {"type": ["number", "null"]}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "score", "sources", "divergence", "event_cross", "data_gaps"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 1200},
        "score": {"type": "object", "required": ["value", "sample_size", "window_days"],
                  "properties": {"value": _NUM, "sample_size": {"type": ["integer", "null"], "minimum": 0},
                                 "window_days": {"type": ["integer", "null"], "minimum": 0}}},
        "sources": {"type": "array", "items": {
            "type": "object", "required": ["name", "score", "sample_size"],
            "properties": {"name": {"type": "string", "enum": list(SOURCES)}, "score": _NUM,
                           "sample_size": {"type": ["integer", "null"], "minimum": 0}}}},
        "divergence": {"type": "object", "required": ["state", "note"],
                       "properties": {"state": {"type": "string", "enum": list(DIVERGENCE)}, "note": _TEXT}},
        "event_cross": {"type": "object", "required": ["event_date", "relation", "note"],
                        "properties": {"event_date": {"type": ["string", "null"]}, "relation": {"type": "string", "enum": list(RELATIONS)},
                                       "note": _TEXT}},
        "data_gaps": {"type": "array", "items": _TEXT},
    },
}


def divergence_state(scores: Mapping[str, float | None]) -> str:
    """How the sources lean, by sign only (a classification, not a calculation): `diverge` when one source is positive and
    another negative, `agree` when two or more share a sign and none opposes it, `mixed` when only one source has a sign,
    `unknown` when none has."""
    signs = [(1 if s > 0 else -1 if s < 0 else 0) for s in scores.values() if s is not None]
    if not signs:
        return "unknown"
    if 1 in signs and -1 in signs:
        return "diverge"
    return "agree" if len([s for s in signs if s != 0]) >= 2 else "mixed"


def event_relation(event_date: str | None, as_of: datetime) -> str:
    """`before_event` while the cut-off is before the event day, `after_event` on or after it, `no_event_known` without a date."""
    if not event_date:
        return "no_event_known"
    day = date.fromisoformat(str(event_date)[:10])
    return "before_event" if as_of.astimezone(timezone.utc).date() < day else "after_event"


def _sample_with_score(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    score = value["score"]
    if score["value"] is not None and not score["sample_size"]:
        return ["score: a score has to come with the size of its sample"]
    return [f"sources.{s['name']}: a score has to come with the size of its sample" for s in value["sources"]
            if s["score"] is not None and not s["sample_size"]]


def _divergence_matches(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    expected = divergence_state({s["name"]: s["score"] for s in value["sources"]})
    stated = value["divergence"]["state"]
    return [] if stated == expected else [f"divergence: the scores of the sources give {expected!r}, the report says {stated!r}"]


def _event_matches(value: dict, ctx: RunContext, evidence: EvidenceLog) -> list[str]:
    cross = value["event_cross"]
    try:
        expected = event_relation(cross["event_date"], ctx.as_of)
    except ValueError:
        return [f"event_cross: {cross['event_date']!r} is not a date"]
    return [] if cross["relation"] == expected else [f"event_cross: the cut-off is {expected!r} the event, the report says {cross['relation']!r}"]


def data_sources() -> dict[str, Any]:
    """The text sources of the snapshot, all of them through the data module."""
    from hokma.data import cot as cot_data
    from hokma.data import news as news_data
    from hokma.data import social as social_data

    def social(symbol: str, cut: datetime) -> Any:
        return merge_results({"stocktwits": social_data.stocktwits(symbol, as_of=cut),
                              "reddit": social_data.reddit_posts(symbol, as_of=cut)}, as_of=cut)

    return {"news": lambda symbol, cut: news_data.news(symbol, as_of=cut), "social": social,
            "cot": lambda symbol, cut: cot_data.cot(symbol, as_of=cut)}


def render(value: dict) -> AnalystReport:
    score = value["score"]
    parts = ["no score" if score["value"] is None else f"Score {score['value']} from {score['sample_size']} items over {score['window_days']} days."]
    findings = [parts[0]]
    findings += [f"Source {s['name']}: " + ("no score" if s["score"] is None else f"{s['score']} from {s['sample_size']} items") + "." for s in value["sources"]]
    findings.append(f"Sources {value['divergence']['state']}: {value['divergence']['note']}")
    cross = value["event_cross"]
    findings.append(f"Event {cross['event_date'] or 'unknown'}: {cross['relation'].replace('_', ' ')}. {cross['note']}")
    return AnalystReport(value["summary"], tuple(findings), tuple(value["data_gaps"]))


SPEC = OutputSpec(SCHEMA, exempt_keys=frozenset({"window_days"}), checks=(_sample_with_score, _divergence_matches, _event_matches))
AGENT = AnalystAgent(CODE, KEY, SPEC, render, snapshot_categories=SNAPSHOT_CATEGORIES, sources=data_sources)

system_prompt = AGENT.system_prompt
toolbox = AGENT.toolbox
run = AGENT.run
