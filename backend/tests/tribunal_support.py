"""Fixtures of the tribunal tests (B9): a recorded snapshot, scripted engines and the JSON the three seats answer with."""
import json
from datetime import datetime, timezone

from hokma.desks.core.graph import ModelReply
from hokma.desks.core.state import ToolCall

AS_OF = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
SNAPSHOT = {
    "symbol": "ACME",
    "options": {"iv_rank": 72.5, "iv_30d": 0.41, "rv_30d": 0.28, "spread_pct": 0.06, "open_interest": 1850},
    "market": {"close": 152.4, "above_trend": True, "distance_to_resistance_pct": 3.2},
    "events": {"earnings_date": "2026-12-03", "days_to_earnings": 13},
    "filter": {"blend_score": 0.42, "investors_for": 4},
    "book": {"delta_overlap_pct": 18.5},
    "costs": {"round_trip_pct": 0.9},
    "portfolio": {"correlation_to_book": 0.31},
}
REPORTS = {"market": "Trend is up and price sits below the next resistance.", "options": "Implied volatility is rich against the realized read."}

BULL_CLAIMS = [
    "Earnings fall inside the life of the option so the catalyst is dated",
    "The company filter backs the name with four profiles in favor",
    "Price holds above the trend with room before resistance",
    "Open interest is deep enough to trade the contract",
    "Book overlap is modest so the position adds diversification",
]
BEAR_CLAIMS = [
    ("Implied volatility is rich against realized so the purchase is expensive", "high", "options.iv_rank"),
    ("The spread of the contract eats a large share of the premium", "medium", "options.spread_pct"),
    ("Overlap with the book concentrates delta risk", "low", "book.delta_overlap_pct"),
    ("Resistance sits close to the current price", "medium", "market.distance_to_resistance_pct"),
    ("The catalyst date leaves little time for a failed reaction", "medium", "events.days_to_earnings"),
]


def evidence(ref, note="what the value shows"):
    return [{"ref": ref, "note": note}]


def bull_json(round_no=1, *, rebuttal=None, extra=None, ref="events.days_to_earnings", claim=None):
    opening = round_no == 1
    body = {
        "rebuttal": "" if opening else (rebuttal if rebuttal is not None else "The volatility cost is real, yet the dated catalyst and the deep open interest outweigh it"),
        "points": [{"claim": claim or BULL_CLAIMS[(round_no - 1) % len(BULL_CLAIMS)], "evidence": evidence(ref)}],
        "catalysts": ["Earnings on 2026-12-03"],
    }
    body.update(extra or {})
    return json.dumps(body)


def bear_json(round_no=1, *, rebuttal=None, extra=None, concerns=None):
    base = (round_no - 1) % 3
    chosen = concerns or [BEAR_CLAIMS[(base + i) % len(BEAR_CLAIMS)] for i in range(3)]
    body = {
        "rebuttal": rebuttal if rebuttal is not None else "A dated catalyst does not make the premium cheaper, the volatility read stays rich",
        "concerns": [{"claim": c, "severity": s, "evidence": evidence(r), "mitigation": "Use a debit spread to cut the premium paid"} for c, s, r in chosen],
        "most_likely_failure": {"concern": 1, "reason": "Rich implied volatility is the first thing a failed reaction exposes"},
        "would_disprove": ["Implied volatility rank falls below the median before the catalyst"],
    }
    body.update(extra or {})
    return json.dumps(body)


def moderator_json(refuted=(), more=True, central="Is implied volatility rich enough to make the purchase expensive?"):
    return json.dumps({"refuted": [{"side": s, "claim": c, "reason": r} for s, c, r in refuted], "central_disagreement": central, "another_round": more})


def judge_json(rating="APPROVE", confidence=78, band="solid", **over):
    body = {
        "rating": rating, "confidence": confidence, "band": band,
        "band_justification": f"The case sits in the {band} band: the dated catalyst and the volatility read agree and the contract is liquid",
        "deciding_claim": "The catalyst is dated and falls inside the life of the option",
        "weakest_claim": "The purchase is expensive against realized volatility",
        "rationale": "Implied volatility rank is 72.5 against realized 0.28, yet the dated catalyst and the open interest carry the case",
        "catalysts": ["Earnings on 2026-12-03"], "conditions": [], "evidence_requested": [],
        "evidence_refs": ["events.days_to_earnings", "options.iv_rank"],
    }
    body.update(over)
    return json.dumps(body)


class Scripted:
    """A ChatModel that answers from a script (the last answer repeats) and records every call it sees."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, system, messages, tools=()):
        self.calls.append({"system": system, "messages": list(messages), "tools": tuple(tools)})
        item = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if callable(item):
            item = item(system, messages)
        return item if isinstance(item, ModelReply) else ModelReply(item)

    @property
    def users(self):
        return [c["messages"][-1].content for c in self.calls]


def tool_reply(name="quant_call"):
    return ModelReply("let me check", (ToolCall("c1", name, {"name": "validation.dsr"}),))
