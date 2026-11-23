"""Support of the cycle tests of the SWO desk (SPEC-019: B12): a scripted engine per seat, recorded data shifted to the cut-off, and a runtime
built on the simulated broker of the execution fixtures. Nothing here reaches the network."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

import pandas as pd
from b7_support import ScriptedChat
from b8_support import LeanChat, candles, statements
from trader_support import AS_OF, C200, ScriptedModel, answer_json, context
from tribunal_support import SNAPSHOT, bear_json, bull_json, judge_json, moderator_json

from hokma.broker.types import Fill
from hokma.desks.agents.investors import CompanyData
from hokma.desks.core.graph import ModelReply
from hokma.desks.core.state import Message
from hokma.desks.swo.graph import AnalystInput, CycleData, CycleRuntime, Mark

CUT_OLD = datetime(2024, 7, 1, tzinfo=timezone.utc)
SHIFT = AS_OF - CUT_OLD

SAM_SNAPSHOT = {"symbol": "AAPL", "as_of": AS_OF.isoformat(),
                "sections": {"news": {"status": "ok", "value": {"score": 0.35, "items": 12}},
                             "social": {"status": "ok", "value": {"score": 0.45, "items": 40}},
                             "cot": {"status": "no_data", "value": None}}}
THESIS = "The verdict is a buy on a dated catalyst; a long call keeps the cost of the idea known and the contract trades tight."
SAM_REPORT = {
    "summary": "Mood is positive in the news and in the posts, so the sources agree.",
    "score": {"value": 0.45, "sample_size": 40, "window_days": 7},
    "sources": [{"name": "news", "score": 0.35, "sample_size": 12}, {"name": "social", "score": 0.45, "sample_size": 40}],
    "divergence": {"state": "agree", "note": "News and social posts lean the same way."},
    "event_cross": {"event_date": "2026-12-03", "relation": "before_event", "note": "The reading was taken before the event."},
    "data_gaps": ["cot"],
}
REVIEW = json.dumps({"scores": {k: 0.9 for k in ("plausibility", "overfitting", "sample", "regime", "exit", "concentration", "execution_realism", "invalidation")}, "issues": []})


def shifted_statements(company: str = "ALFA", **last: Any) -> pd.DataFrame:
    frame = statements(company, **{"price": 18, **last})
    frame.index = [(pd.Timestamp(i) + SHIFT).strftime("%Y-%m-%d") for i in frame.index]
    frame["filed"] = [(pd.Timestamp(d) + SHIFT).strftime("%Y-%m-%d") for d in frame["filed"]]
    return frame


def shifted_candles() -> pd.DataFrame:
    frame = candles().copy()
    frame.index = frame.index + SHIFT
    return frame


def _point(claim: str, source: str, name: str, value: str) -> dict[str, Any]:
    return {"claim": claim, "evidence": [{"source": source, "name": name, "value": value}]}


def risk_voices(risk_level: str = "moderate", rounds: int = 2) -> dict[str, ScriptedChat]:
    """Jake, Glen and Reed for the two rounds of a SWO desk. Their points cite the limits of the desk context, which the tests set."""
    jake = json.dumps({"stance": "aggressive", "summary": "There is room.", "points": [_point("Room under the premium limit.", "context", "max_premium_usd", "5000")]})
    glen = json.dumps({"stance": "conservative", "summary": "Mind the drawdown.", "points": [_point("The drawdown limit is close to the exposure.", "context", "max_drawdown_pct", "10")]})
    reed = json.dumps({"risk_level": risk_level, "summary": "The proposal is inside the frame and the book can carry it.",
                       "points": [_point("The premium is under the limit.", "context", "max_premium_usd", "5000")], "limits": {}, "open_questions": []})
    return {"A12": ScriptedChat(*[ModelReply(jake)] * rounds), "A13": ScriptedChat(*[ModelReply(glen)] * rounds), "A14": ScriptedChat(*[ModelReply(reed)] * rounds)}


class Garbage:
    """A model that answers with text that is no object: the report of that analyst stays absent."""

    def chat(self, system, messages, tools=()):
        return ModelReply("I could not read the data.")


class GrantChat:
    """Grant's engine: it answers each task of the cycle by the task named in its prompt."""

    def __init__(self, *, final: Mapping[str, Any] | None = None, reflection: str = "The entry came from a dated catalyst; the first measure shows the read was right.") -> None:
        self.final, self.reflection, self.calls = final, reflection, []

    def chat(self, system: str, messages: Sequence[Message], tools=()) -> ModelReply:
        self.calls.append(system)
        facts = next(m.content for m in messages if m.role == "user")
        if "## Task\nGive the final rating" in system:
            body = {"rating": "APPROVE", "grade": "B", "thesis": "A dated catalyst and a liquid contract carry a buy of calls.",
                    "would_change": ["The catalyst date moves out of the life of the option."], "horizon": "weeks", "desk_level": self._level(facts),
                    "summary": "The tribunal approved and the committee found the risk acceptable.", "data_gaps": []}
            body.update(self.final or {})
            return ModelReply(json.dumps(body))
        if "## Task\nWrite one short lesson" in system:
            order_id = json.loads(facts.split("Facts computed by the desk code:\n", 1)[1])["orders"][0]["order_id"]
            return ModelReply(json.dumps({"reflections": [{"order_id": order_id, "lesson": self.reflection}], "data_gaps": []}))
        return ModelReply("{}")

    @staticmethod
    def _level(user: str) -> int:
        return json.loads(user.split("Facts computed by the desk code:\n", 1)[1])["desk_level"]


class Engines:
    """One scripted engine per key, created once so that a seat that speaks several times plays its script in order."""

    def __init__(self, *, sam: bool = True, judge_rating: str = "APPROVE", judge_over: Mapping[str, Any] | None = None, filter_bias: int = 70, moderator_more: bool = False, grant: GrantChat | None = None,
                 risk_level: str = "moderate", reviewers: tuple[Any, Any] | None = None) -> None:
        self._made: dict[str, Any] = {}
        risk = risk_voices(risk_level)
        self.grant = grant or GrantChat()
        self.models: dict[str, Any] = {"A01": Garbage(), "A04": Garbage(), "A06": Garbage(), "A07": Garbage(),
                                       "A02": ScriptedChat(ModelReply(json.dumps(SAM_REPORT))) if sam else Garbage(),
                                       "A08": ScriptedChat(ModelReply(bull_json(1))), "A09": ScriptedChat(ModelReply(bear_json(1))),
                                       "A10": ScriptedChat(ModelReply(moderator_json(more=moderator_more)), ModelReply(judge_json(rating=judge_rating, **(judge_over or {})))),
                                       "A10:review1": ScriptedChat(ModelReply(REVIEW)), "A10:review2": ScriptedChat(ModelReply(REVIEW)),
                                       "A12": risk["A12"], "A13": risk["A13"], "A14": risk["A14"], "A15": self.grant}
        if reviewers:
            self.models["A10:review1"], self.models["A10:review2"] = reviewers
        for code in ("A16", "A17", "A18", "A19", "A20", "A21", "A22"):
            self.models[code] = LeanChat(filter_bias)

    def __call__(self, key: str) -> Any:
        return self.models[key]


def fills_from_sim(db):
    """The fills of the simulated broker for one order row of the log, as the broker reports them."""
    def fills_of(order_row: Mapping[str, Any]) -> list[Fill]:
        with db.read() as u:
            rows = u.session.query("SELECT f.* FROM sim_fills f JOIN sim_orders o ON o.id = f.sim_order_id WHERE o.order_ref = :r ORDER BY f.id",
                                   {"r": order_row["broker_order_id"]})
        return [Fill(account_id=0, symbol=r["contract_id"], side=r["side"], qty=float(r["qty"]), price=float(r["price"]), commission=float(r["commission"]),
                     at=r["filled_at"], order_id=str(order_row["broker_order_id"]), multiplier=Decimal(r["multiplier"]), contract_id=r["contract_id"])
                for r in rows]
    return fills_of


def runtime(r, engines: Engines, *, company_name: str = "ALFA", price: float = 18, trader_answer: str | None = None, mark: Mark | None = None, agent: str = "hugo", fills_of=None) -> CycleRuntime:
    """The runtime of a cycle on the broker of the shared execution fixture: recorded data, the quotes of the chain, the scripted engines."""
    company = CompanyData(statements=shifted_statements(company_name, price=price), candles=shifted_candles(), regime=None,
                          catalyst={"date": "2026-12-03", "description": "Quarterly results"})

    def load(desk_id: int, symbol: str, as_of: datetime) -> CycleData:
        return CycleData(analysts={"sentiment": AnalystInput(snapshot=SAM_SNAPSHOT)}, company=company, bars=shifted_candles(), snapshot=dict(SNAPSHOT))

    def trade_context(desk_id: int, symbol: str, as_of: datetime):
        return context(desk_id=desk_id)

    from hokma.kit.hokma_tools import Caller

    return CycleRuntime(chat=engines, trader_model=ScriptedModel(trader_answer or answer_json(structure="long_call", legs=[{"contract_id": C200, "side": "buy"}], thesis=THESIS)),
                        load=load, trade_context=trade_context, caller=Caller({"HOKMA_AGENT": agent}), fills_of=fills_of or fills_from_sim(r.db),
                        mark_of=(lambda order, at: mark) if mark else None)
