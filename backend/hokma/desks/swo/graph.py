"""The cycle of a SWO desk, nine steps on the desk graph of the core (SPEC-019: AH-22, section 4.3).

Steps: plan (Grant: TA-21 and TA-28), analysts in parallel, company filter (the investors), debate with a moderator, judge, trader, risk
committee, sub-manager, and, after the fill, the lessons. The graph is `desks.core.graph.StateGraph`: the state holds only plain data (so
a checkpoint of it can be saved before every node) and the objects of the run (results of each agent) live in the run object that the nodes
share. One data path goes from the cut-off to the valuation: what the cycle measures after the fill starts from the price the broker
really filled at (H2), never from a close.

What the cycle never does:
- It does not send an order. Isaac's proposal goes to `hokma.exec.submit` through the tool `exec_propose` and the level of the desk decides
  the rest (1 analyzes, 2 asks the owner, 3 sends under the rules the owner approved). Nothing here calls a broker.
- It does not trust what an agent declares. The line, the effect, the origin, the prices and the risk of the proposal are derived by the
  door from real state; the premium switch is read from its table; the regime comes from the statistics module.
- It fails closed. A step that fails, a judge that says REVIEW or REJECT, a gate that does not approve, a committee with no valid verdict or
  a severe one, a final rating that does not validate or two reviewers that are not separate models: the cycle ends with a report and no
  proposal. Analysts that did not arrive stay `absent`; the cycle goes on while one report is there.

Models, data and the broker's own quotes come in through `CycleRuntime` (nothing here reaches the network on its own). A runtime factory
is registered once by the app (`register_runtime_factory`).
"""
from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from hokma.backoffice import checkpoint, runs
from hokma.broker.types import Fill
from hokma.db import Database, get_database
from hokma.desks.agents import fundamentals, investors, market, news, risk, sentiment
from hokma.desks.agents import options as options_agent
from hokma.desks.agents.debate import DebateResult, run_debate
from hokma.desks.agents.investors import CompanyData, FilterResult
from hokma.desks.agents.judge import APPROVALS, JudgeCase, JudgeResult, RubricReviewer, double_review, judge as run_judge
from hokma.desks.agents.manager import (
    DecisionLog, Learning, ManagerResult, Mandate, analyst_plan, final_facts, final_rating, learn_from_order, load_desk_mandate, select_analysts,
)
from hokma.desks.agents.trader import Delivery, TradeContext, TradeResult, deliver, mcp_handlers, run_trader, trader_toolbox
from hokma.desks.core.graph import END, START, ChatModel, CompiledGraph, ModelReply, StateGraph, complete_path_map
from hokma.desks.core.signal import Signal
from hokma.desks.core.state import DeskState, Message, initial_state, make_report
from hokma.desks.plantel import SEATS
from hokma.desks.swo import premium_switch, regime
from hokma.desks.swo.premium_switch import PremiumSwitchClosed
from hokma.exec import levels

log = logging.getLogger("hokma.desks.swo")

LINE = "SWO"
KIND = "swo_cycle"
ANALYST_AGENTS = {"market": market.AGENT, "sentiment": sentiment.AGENT, "news": news.AGENT, "fundamentals": fundamentals.AGENT,
                  "options": options_agent.AGENT}
INVESTOR_CODES = tuple(code for code, seat in SEATS.items() if seat.desk_role == "signals" and code != "A23")
COMMITTEE_CODES = ("A12", "A13", "A14")
SENT = ("sent", "filled", "partially_filled")
# What each structure gains from: +1 when the price rises, -1 when it falls, 0 when it is a bet on a quiet price.
BIAS = {"long_call": 1, "debit_call_spread": 1, "short_put": 1, "credit_put_spread": 1, "cash_secured_put": 1, "covered_call": 1,
        "long_put": -1, "debit_put_spread": -1, "short_call": -1, "credit_call_spread": -1,
        "iron_condor": 0, "short_straddle": 0, "short_strangle": 0}
NODES = ("plan", "analysts", "company_filter", "debate", "judge", "trader", "committee", "manager", "learn", "close")


class CycleError(RuntimeError):
    """The cycle cannot start. `code` is stable: not_swo, no_runtime, no_mandate."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---- what the app hands to the cycle -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AnalystInput:
    """What one analyst reads in a run: its verified snapshot, the facts of the desk code, and the handlers or sources of recorded data."""

    snapshot: Any = None
    facts: Mapping[str, Any] | None = None
    handlers: Mapping[str, Callable[..., Any]] | None = None
    sources: Mapping[str, Callable[..., Any]] | None = None


@dataclass(frozen=True)
class CycleData:
    """The data of one symbol at one cut-off. `company.regime` is left empty: the cycle derives it from `bars` (the benchmark) with SVC-stats."""

    analysts: Mapping[str, AnalystInput]
    company: CompanyData
    bars: Any                                   # OHLC bars of the benchmark, the source of the regime
    snapshot: Mapping[str, Any]                 # the evidence the tribunal and the judge may cite, by dotted path
    pead_signal: Signal | None = None           # Drew's view on a PEAD desk, computed by code from the earnings event


@dataclass(frozen=True)
class Mark:
    """What measures an open position after the fill: the mark price and its time, the horizon the line fixes and the contract multiplier."""

    price: float | None
    at: str | None
    horizon_days: float | None
    multiplier: float = 1.0
    benchmark_return: float | None = None


@dataclass
class CycleRuntime:
    chat: Callable[[str], ChatModel]                                     # key: a seat code, or A10:review1 and A10:review2 for the gate
    trader_model: Any                                                    # `complete(messages, schema=...)`, see `desks.agents.trader`
    load: Callable[[int, str, datetime], CycleData]
    trade_context: Callable[[int, str, datetime], TradeContext]          # sheets, quotes, book, budget, costs and rules from the broker and the back office
    caller: Any = None                                                   # `hokma.kit.hokma_tools.Caller` of the agent that proposes
    trader_handlers: Mapping[str, Callable[..., Any]] | None = None      # replaces the door of the tools (tests)
    fills_of: Callable[[Mapping[str, Any]], Sequence[Fill]] | None = None  # the broker's fills of an executed order row
    mark_of: Callable[[Mapping[str, Any], datetime], Mark | None] | None = None
    kit_root: Path | None = None
    language: str | None = None


_FACTORY: Callable[[int], CycleRuntime] | None = None


def register_runtime_factory(factory: Callable[[int], CycleRuntime] | None) -> None:
    """The app registers how to build the runtime of a desk (its models, its data, its broker quotes). `None` removes it."""
    global _FACTORY
    _FACTORY = factory


def current_runtime(desk_id: int) -> CycleRuntime:
    if _FACTORY is None:
        raise CycleError("no_runtime", "no runtime factory is registered: the cycle needs models, data and quotes")
    return _FACTORY(desk_id)


class ClientChat:
    """A `ChatModel` over an `LLMClient` of `hokma.llm` (one question per call, no tools offered by the engine). A model that cannot call
    tools answers from the facts the prompt holds."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def chat(self, system: str, messages: Sequence[Message], tools: Sequence[Mapping[str, Any]] = ()) -> ModelReply:
        body: list[dict[str, str]] = [{"role": "system", "content": system}]
        for m in messages:
            role = "assistant" if m.role == "assistant" else "user"
            body.append({"role": role, "content": m.content if m.role != "tool" else f"Tool result ({m.name}): {m.content}"})
        if body[-1]["role"] != "user":
            body.append({"role": "user", "content": "Continue."})
        return ModelReply(self._client.complete(body).text)


# ---- result ---------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CycleResult:
    run_id: int                       # run_records.id
    rating: str | None                # final rating of Grant, or None when the cycle ended before him
    proposal_id: int | None           # exec_proposals.id when Isaac produced one and it was delivered
    exec_status: str | None           # ExecResult.status from H2
    absent: tuple[str, ...]           # reports that did not arrive
    stopped_at: str | None = None     # the step that ended the cycle without a proposal
    reason: str | None = None         # why, as a stable code
    learning: str | None = None       # step 9 outcome when the order had already executed


# ---- the run object -------------------------------------------------------------------------------------------------------------------

@dataclass
class _Run:
    rt: CycleRuntime
    db: Database
    desk_id: int
    symbol: str
    as_of: datetime
    mandate: Mandate
    strategy: str | None
    owner_order: bool
    run_id: int
    signature: str
    data: CycleData | None = None
    reports: dict[str, str] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)
    filter: FilterResult | None = None
    debate: DebateResult | None = None
    judge: JudgeResult | None = None
    ctx: TradeContext | None = None
    trade: TradeResult | None = None
    committee: Any = None
    final: ManagerResult | None = None
    delivery: Delivery | None = None
    exec_status: str | None = None
    proposal_id: int | None = None
    learning: Learning | None = None
    orders: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    closed: bool = False
    log: DecisionLog | None = None


def _chat(run: _Run, key: str) -> ChatModel:
    return run.rt.chat(key)


def _stop(reason: str, **extra: Any) -> dict[str, Any]:
    return {"stop": reason, **extra}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ---- the nodes ------------------------------------------------------------------------------------------------------------------------

def _plan(run: _Run, state: DeskState) -> dict[str, Any]:
    """Step 1, Grant: which analysts run and in which order (TA-21), and which are left out (TA-28). A left out analyst's report is absent."""
    plan = analyst_plan(run.mandate)
    selection = select_analysts(plan)
    reports = {step.key: make_report(step.key, None) for step in plan}
    reports.update(selection.reports)
    return {"plan": [s.key for s in plan], "selected": list(selection.run), "off": list(selection.off), "reports": reports}


def _run_analyst(run: _Run, key: str) -> tuple[str, str, str]:
    agent = ANALYST_AGENTS[key]
    given = (run.data.analysts.get(key) if run.data else None) or AnalystInput()
    try:
        result = agent.run(_chat(run, agent.code), desk_id=run.desk_id, symbol=run.symbol, as_of=run.as_of, line=LINE, snapshot=given.snapshot,
                           facts=given.facts, kit_root=run.rt.kit_root, handlers=given.handlers, sources=given.sources, language=run.rt.language)
    except Exception as exc:                                                   # noqa: BLE001  (an analyst that fails is an absent report)
        return key, "", f"error:{type(exc).__name__}"
    if not result.valid:
        return key, "", "invalid:" + "; ".join(result.check.problems[:2])[:200]
    return key, result.text, "ok"


def _analysts(run: _Run, state: DeskState) -> dict[str, Any]:
    """Step 2: the analysts that are on, in parallel. A report that does not arrive, or does not validate, stays absent and does not break the run."""
    run.data = run.rt.load(run.desk_id, run.symbol, run.as_of)
    keys = list(state["selected"])
    with ThreadPoolExecutor(max_workers=max(1, len(keys))) as pool:
        done = list(pool.map(lambda key: _run_analyst(run, key), keys))
    reports = dict(state["reports"])
    status: dict[str, str] = {}
    for key, text, how in done:
        status[key] = how
        reports[key] = make_report(key, text or None)
        if text:
            run.reports[key] = text
    update: dict[str, Any] = {"reports": reports, "analyst_status": status}
    if not run.reports:
        update.update(_stop("no_analyst_report"))
    return update


def _company_filter(run: _Run, state: DeskState) -> dict[str, Any]:
    """Step 3: the regime from SVC-stats, the profiles of the investors, the mix and the verdict. A company the filter rejects does not reach the debate."""
    assert run.data is not None
    label, regime_facts = regime.derive_regime(run.data.bars, run.as_of)
    company = replace(run.data.company, regime=label)
    codes = tuple(c for c in investors.desk_investor_seats(run.desk_id, db=run.db) if c != investors.PEAD_CODE)
    models = {code: _chat(run, code) for code in codes}
    result = investors.evaluate_company(run.desk_id, symbol=run.symbol, as_of=run.as_of, line=LINE, models=models, data=company, mandate=run.mandate,
                                        strategy=run.strategy, run_id=run.run_id, kit_root=run.rt.kit_root, language=run.rt.language, db=run.db,
                                        pead_signal=run.data.pead_signal)
    run.filter = result
    summary = {"passes": result.passes, "reason_code": result.reason_code, "blended": result.blended, "confidence": result.confidence,
               "n_used": result.n_used, "abstained": list(result.abstained), "merged": [list(g) for g in result.merged], "vetoed_by": result.vetoed_by}
    update: dict[str, Any] = {"regime": label, "regime_facts": regime_facts, "filter": summary}
    if not result.passes:
        update.update(_stop(f"filter:{result.reason_code}"))
    return update


def _tribunal_snapshot(run: _Run) -> dict[str, Any]:
    assert run.data is not None and run.filter is not None and run.log is not None
    snap = dict(run.data.snapshot)
    snap["filter"] = {"blended": run.filter.blended, "confidence": run.filter.confidence, "n_used": run.filter.n_used, "reason_code": run.filter.reason_code,
                      "passes": run.filter.passes}
    snap["prior_approved_decisions"] = [{"decision_id": d["decision_id"], "topic": d["topic"], "decided_at": d["decided_at"]}
                                        for d in run.log.approved(run.symbol, limit=5)]
    return _plain(snap)


def _debate(run: _Run, state: DeskState) -> dict[str, Any]:
    """Step 4: the bull against the bear with a moderator, cut by the counter of the line. Layer 1 of the decision log keeps the raw debate."""
    run.snapshot = _tribunal_snapshot(run)
    result = run_debate(_chat(run, "A08"), _chat(run, "A09"), _chat(run, "A10"), line=LINE, desk_id=run.desk_id, as_of=run.as_of, snapshot=run.snapshot,
                        reports=dict(run.reports), kit_root=run.rt.kit_root, language=run.rt.language)
    run.debate = result
    assert run.log is not None
    raw = {"rounds": result.rounds, "stop_reason": result.stop_reason, "summary": result.summary, "error": result.error,
           "bull": None if result.bull is None else _plain(result.bull.to_dict()), "bear": None if result.bear is None else _plain(result.bear.to_dict())}
    run.log.record_raw(symbol=run.symbol, as_of=run.as_of, debate=raw)
    return {"debate": {"count": result.rounds, "summary": result.summary, "last_speaker": "bear", "last_response": "", "verdict": ""},
            "debate_stop": result.stop_reason}


def _case_text(run: _Run) -> str:
    assert run.debate is not None
    bull = run.debate.bull.render() if run.debate.bull else "(no argument in favor)"
    bear = run.debate.bear.render() if run.debate.bear else "(no argument against)"
    return f"Symbol {run.symbol}.\nFor:\n{bull}\nAgainst:\n{bear}\nDebate summary: {run.debate.summary or '(none)'}"


def _judge(run: _Run, state: DeskState) -> dict[str, Any]:
    """Step 5: the double review gate on two separate models, then the judge. REVIEW or REJECT ends the cycle with a report and no proposal."""
    assert run.debate is not None
    first, second = _chat(run, "A10:review1"), _chat(run, "A10:review2")
    if first is second:
        return _stop("reviewers_not_separate")
    kwargs = dict(line=LINE, desk_id=run.desk_id, as_of=run.as_of, kit_root=run.rt.kit_root, language=run.rt.language)
    gate = double_review(_case_text(run), RubricReviewer(first, **kwargs), RubricReviewer(second, **kwargs))
    case = JudgeCase(line=LINE, desk_id=run.desk_id, symbol=run.symbol, as_of=run.as_of, bull=run.debate.bull, bear=run.debate.bear,
                     snapshot=run.snapshot, reports=dict(run.reports), summary=run.debate.summary, ledger=run.debate.ledger)
    result = run_judge(_chat(run, "A10"), case, gate=gate, kit_root=run.rt.kit_root, language=run.rt.language)
    run.judge = result
    update: dict[str, Any] = {"judge": {"rating": result.rating, "flags": list(result.flags), "gate": gate.status, "gate_rounds": gate.rounds}}
    assert run.log is not None
    run.log.record_raw(symbol=run.symbol, as_of=run.as_of, debate={"stage": "judge", "case": _case_text(run)[:4000]},
                       judge=update["judge"], stopped_at=None if result.rating in APPROVALS else "judge")
    if result.rating not in APPROVALS:
        update.update(_stop(f"judge:{result.rating}"))
    return update


def _underlying_ref(ctx: TradeContext, symbol: str) -> float | None:
    quote = ctx.quotes.get(f"EQ:{symbol}")
    return None if quote is None else float((quote.bid + quote.ask) / 2)


def _trader(run: _Run, state: DeskState) -> dict[str, Any]:
    """Step 6: Isaac. The context comes from code; the premium switch is read from its table; the size, stop and objective are computed by code."""
    assert run.judge is not None and run.log is not None
    base = run.rt.trade_context(run.desk_id, run.symbol, run.as_of)
    from hokma.broker import service

    verdict = f"{run.judge.rating}. {run.judge.to_plantel().rationale}"
    ctx = replace(base, desk_id=run.desk_id, underlying=run.symbol, as_of=run.as_of, verdict=verdict, reports=dict(run.reports),
                  mandate_summary=run.mandate.summary(), limits=service.get_limits(run.desk_id, db=run.db),
                  premium_selling_open=premium_switch.selling_open(run.desk_id, run.symbol, db=run.db))
    run.ctx = ctx
    result = run_trader(run.rt.trader_model, ctx, language=run.rt.language)
    run.trade = result
    if result.proposal is None:
        return _stop(f"trader:{result.status}:{result.code}", trade={"status": result.status, "code": result.code, "why": result.why})
    proposal = result.proposal
    try:
        premium_switch.require_offerable(proposal.structure, run.desk_id, proposal.underlying, db=run.db)
    except PremiumSwitchClosed:
        return _stop("premium_selling_closed")
    if run.log.reopen_blocked(proposal.underlying, proposal.expiration, owner_order=run.owner_order):
        return _stop("rejected_before")
    return {"trade": {"status": result.status, "code": result.code, "structure": proposal.structure, "underlying": proposal.underlying,
                      "expiration": proposal.expiration.isoformat(), "contracts": result.contracts}}


def _committee(run: _Run, state: DeskState) -> dict[str, Any]:
    """Step 7: Jake, Glen and Reed in fixed rotation over the impact on the book that code read from the back office."""
    assert run.ctx is not None and run.trade is not None and run.trade.proposal is not None
    portfolio = risk.load_portfolio_context(run.desk_id, run.ctx.sheets, line=LINE, db=run.db)
    context = risk.RiskContext(run.desk_id, LINE, run.symbol, run.as_of, _plain(run.trade.proposal.to_tool_body()), portfolio, _plain(run.ctx.limits),
                               risk.audit_clamps(run.desk_id, db=run.db))
    result = risk.run_committee(context, {code: _chat(run, code) for code in COMMITTEE_CODES}, kit_root=run.rt.kit_root, language=run.rt.language, db=run.db)
    run.committee = result
    verdict = result.verdict
    summary = {"risk_level": None if verdict is None else verdict.risk_level, "summary": "" if verdict is None else verdict.summary,
               "limits": None if result.limits is None else {"status": result.limits.status, "limits": _plain(result.limits.limits)},
               "turns": [{"seat": t.seat_code, "valid": t.valid} for t in result.turns]}
    update: dict[str, Any] = {"committee": summary}
    if verdict is None:
        update.update(_stop("committee_no_verdict"))
    elif verdict.risk_level == "severe":
        update.update(_stop("committee_severe"))
    return update


def _manager(run: _Run, state: DeskState) -> dict[str, Any]:
    """Step 8, Grant: the final rating, then the delivery of the proposal to `exec_propose`. The level of the desk decides what follows."""
    assert run.judge is not None and run.trade is not None and run.trade.proposal is not None and run.filter is not None
    level, _rules = levels.get_level(run.desk_id, db=run.db)
    committee = state.get("committee", {})
    facts = final_facts(desk_level=level, judge_rating=run.judge.rating, symbol=run.symbol, filter_summary=run.filter.summary(),
                        judge_rationale=run.judge.to_plantel().rationale, committee={"risk_level": committee.get("risk_level"), "summary": committee.get("summary")},
                        proposal={"structure": run.trade.proposal.structure, "underlying": run.trade.proposal.underlying,
                                  "expiration": run.trade.proposal.expiration.isoformat(), "net_premium": str(run.trade.proposal.net_premium),
                                  "contracts": run.trade.contracts})
    result = final_rating(_chat(run, "A15"), desk_id=run.desk_id, as_of=run.as_of, symbol=run.symbol, facts=_plain(facts), line=LINE,
                          kit_root=run.rt.kit_root, language=run.rt.language, db=run.db)
    run.final = result
    if not result.valid or result.final is None:
        return _stop("manager_invalid", final={"valid": False, "problems": list(result.check.problems[:4])})
    report = {"rating": result.final.rating, "grade": result.final.grade, "summary": result.final.summary, "thesis": result.thesis,
              "would_change": list(result.would_change), "horizon": result.horizon, "level": result.level, "valid": True}
    if result.final.rating == "REJECT":
        return _stop("manager_rejected", final=report)
    caller_handlers = run.rt.trader_handlers
    if caller_handlers is None:
        if run.rt.caller is None:
            return _stop("no_caller", final=report)
        caller_handlers = mcp_handlers(run.rt.caller)
    delivery = deliver(trader_toolbox(caller_handlers), run.desk_id, run.trade)
    run.delivery = delivery
    update: dict[str, Any] = {"final": report, "delivery": {"status": delivery.status, "code": delivery.code, "why": delivery.why[:300], "proposal_id": delivery.proposal_id}}
    if delivery.proposal_id is not None:
        from hokma import exec as hexec

        run.proposal_id = int(delivery.proposal_id)
        run.exec_status = hexec.status(run.proposal_id, db=run.db).status
        ref = _underlying_ref(run.ctx, run.symbol) if run.ctx else None
        run.orders.append({"proposal_id": run.proposal_id, "symbol": run.symbol, "bias": BIAS.get(run.trade.proposal.structure, 0), "underlying_ref": ref,
                           "ref_source": "quote"})
        update["exec_status"] = run.exec_status
    else:
        update.update(_stop(f"delivery:{delivery.status}:{delivery.code}"))
    return update


def learn(run: _Run, *, proposal_id: int | None = None, as_of: datetime | None = None) -> Learning | None:
    """Step 9, after the fill: `lessons.settle` measures from the real entry fill, Grant reflects and lessons are proposed. Only an order that
    executed teaches. Called by the cycle when the order already left (level 3) and later by whoever sees the fill (level 2, after the owner)."""
    pid = proposal_id if proposal_id is not None else run.proposal_id
    if pid is None:
        return None
    with run.db.read() as u:
        row = u.session.one("SELECT * FROM exec_proposals WHERE id = :i AND desk_id = :d", {"i": pid, "d": run.desk_id})
        order = None if row is None or row["broker_order_record_id"] is None else u.session.one(
            "SELECT * FROM broker_orders WHERE id = :i", {"i": row["broker_order_record_id"]})
    if row is None or row["status"] not in SENT or order is None:
        return Learning("not_executed", "the proposal has not left")
    log = run.log or DecisionLog(run.desk_id, db=run.db)
    thesis = (run.trade.thesis if run.trade else "") or str(json.loads(row["proposal_json"]).get("thesis") or "")
    decision_id = row["decision_id"] or order.get("decision_id") or log.record_approved(int(pid))
    if decision_id is None:
        return Learning("not_executed", "no decision is recorded for this order")
    if run.rt.fills_of is None:
        return Learning("pending", "no fills source")
    fills = tuple(run.rt.fills_of(order))
    mark = run.rt.mark_of(order, as_of or run.as_of) if run.rt.mark_of else None
    return learn_from_order(_chat(run, "A15"), desk_id=run.desk_id, decision_id=int(decision_id), order_id=int(order["id"]), as_of=as_of or run.as_of,
                            thesis=thesis, fills=fills, mark_price=None if mark is None else mark.price, mark_at=None if mark is None else mark.at,
                            horizon_days=None if mark is None else mark.horizon_days, multiplier=1.0 if mark is None else mark.multiplier,
                            benchmark_return=None if mark is None else mark.benchmark_return, line=LINE, kit_root=run.rt.kit_root,
                            language=run.rt.language, db=run.db)


def learn_after_fill(desk_id: int, run_id: int, *, runtime: CycleRuntime, as_of: datetime | None = None, db: Database | None = None) -> Learning | None:
    """Step 9 for a cycle whose order left later (level 2: after the owner approved it). Reads what the cycle recorded, then measures from the
    real entry fill and lets Grant reflect. Returns None when the cycle produced no proposal."""
    database = db or get_database()
    record = runs.get(run_id, db=database)
    if record is None or record["desk_id"] != desk_id or record["kind"] != KIND:
        raise CycleError("no_run", f"run {run_id} is not a cycle of desk {desk_id}")
    orders = (record["record"].get("outputs") or {}).get("orders") or []
    if not orders:
        return None
    entries = record["record"].get("entries") or {}
    cut = as_of or datetime.fromisoformat(entries["as_of"])
    run = _Run(runtime, database, desk_id, str(entries["symbol"]), cut, load_desk_mandate(desk_id, db=database), None, False, run_id, record["signature"],
               log=DecisionLog(desk_id, db=database))
    return learn(run, proposal_id=int(orders[0]["proposal_id"]), as_of=cut)


def _learn(run: _Run, state: DeskState) -> dict[str, Any]:
    if run.exec_status not in SENT:                         # nothing left yet (level 1, or waiting for the owner): nothing to learn from
        return {}
    result = learn(run)
    run.learning = result
    return {} if result is None else {"learning": {"status": result.status, "reason": result.reason}}


def _close(run: _Run, state: DeskState) -> dict[str, Any]:
    """Close the record of the run: what came in, what each step said and the proposal that left. A run that ended in an error is `failed`."""
    stop = state.get("stop")
    status = "failed" if isinstance(stop, str) and stop.startswith("error:") else "done"
    absent = [k for k, r in state.get("reports", {}).items() if r.get("status") == "absent"]
    outputs = _plain({"stopped_at": state.get("stop_at") if stop else None, "reason": stop, "regime": state.get("regime"),
                      "regime_facts": state.get("regime_facts"), "filter": state.get("filter"), "judge": state.get("judge"), "trade": state.get("trade"),
                      "committee": state.get("committee"), "final": state.get("final"), "delivery": state.get("delivery"),
                      "learning": state.get("learning"), "orders": run.orders, "absent": absent, "analyst_status": state.get("analyst_status"),
                      "debate": state.get("debate"), "trail": [*state.get("trail", []), "close"]})
    runs.finish(run.run_id, status, entries={"symbol": run.symbol, "as_of": run.as_of.isoformat(), "desk_id": run.desk_id, "mandate": run.mandate.name},
                outputs=outputs, order_ids=[run.proposal_id] if run.proposal_id is not None else [], db=run.db)
    run.closed = True
    return {"absent": absent}


# ---- the graph ------------------------------------------------------------------------------------------------------------------------

def build_graph(run: _Run) -> CompiledGraph:
    """The nine steps as a compiled graph. Every node is guarded: an error becomes a stop with its class and the cycle goes to the record."""
    g = StateGraph()
    bodies: dict[str, Callable[[_Run, DeskState], dict[str, Any]]] = {
        "plan": _plan, "analysts": _analysts, "company_filter": _company_filter, "debate": _debate, "judge": _judge, "trader": _trader,
        "committee": _committee, "manager": _manager, "learn": _learn, "close": _close}

    def guarded(name: str) -> Callable[[DeskState], dict[str, Any]]:
        def node(state: DeskState) -> dict[str, Any]:
            run.step += 1
            trail = [*state.get("trail", []), name]
            if name != "close":
                try:
                    checkpoint.save(run.run_id, run.signature, run.step, name, dict(_plain(_checkpoint_state(state))), trail, db=run.db)
                except Exception:                                                   # noqa: BLE001  (a checkpoint that cannot be written is not a reason to trade)
                    log.warning("checkpoint of %s was not written", name)
            try:
                update = bodies[name](run, state) or {}
            except Exception as exc:                                                # noqa: BLE001  (fail closed: no proposal after an error)
                log.exception("step %s failed", name)
                update = {"stop": f"error:{name}:{type(exc).__name__}", "error": str(exc)[:300]}
            if update.get("stop"):
                update["stop_at"] = name
            return {**update, "trail": trail}
        return node

    for name in NODES:
        g.add_node(name, guarded(name))
    g.add_edge(START, "plan")

    def after(next_node: str) -> Callable[[DeskState], str]:
        return lambda state: "close" if state.get("stop") else next_node

    chain = (("plan", "analysts"), ("analysts", "company_filter"), ("company_filter", "debate"), ("judge", "trader"), ("trader", "committee"),
             ("committee", "manager"), ("manager", "learn"))
    for source, target in chain:
        g.add_conditional_edges(source, after(target), complete_path_map(target, "close"))
    g.add_edge("debate", "judge")                  # the judge decides what an empty debate means: REVIEW
    g.add_edge("learn", "close")
    g.add_edge("close", END)
    return g.compile()


def _checkpoint_state(state: DeskState) -> dict[str, Any]:
    """The state a checkpoint keeps: plain data only, with the debate as its bounded summary and not its text."""
    keep = {k: v for k, v in state.items() if k not in ("messages",)}
    return keep


# ---- the entry --------------------------------------------------------------------------------------------------------------------------

def _profile(db: Database, desk_id: int) -> tuple[str, str | None]:
    with db.read() as u:
        row = u.session.one("SELECT line, variant FROM desk_profile WHERE desk_id = :d", {"d": desk_id})
    if row is None or row["line"] != LINE:
        raise CycleError("not_swo", f"desk {desk_id} is not a {LINE} desk")
    return row["line"], row["variant"]


def run_cycle(desk_id: int, *, symbol: str, as_of: datetime, runtime: CycleRuntime | None = None, strategy: str | None = None,
              owner_order: bool = False, db: Database | None = None) -> CycleResult:
    """One cycle of a SWO desk for one symbol at one cut-off (AH-22). Returns the run id, the final rating of Grant (None when the cycle ended
    before him), the proposal and what the door answered, and the reports that did not arrive. `owner_order` is true only when the owner asked
    for this operation again after rejecting it."""
    database = db or get_database()
    _profile(database, desk_id)
    rt = runtime or current_runtime(desk_id)
    try:
        mandate = load_desk_mandate(desk_id, db=database)
    except Exception as exc:                                                       # noqa: BLE001
        raise CycleError("no_mandate", f"desk {desk_id} has no mandate it can run with") from exc
    sym = symbol.strip().upper()
    signature = hashlib.sha256(f"{desk_id}|{sym}|{as_of.isoformat()}".encode()).hexdigest()
    run_id = runs.begin(desk_id, KIND, signature=signature, started_at=as_of, db=database)
    run = _Run(rt, database, desk_id, sym, as_of, mandate, strategy, owner_order, run_id, signature, log=DecisionLog(desk_id, db=database))
    state = initial_state(desk_id=desk_id, symbol=sym, line=LINE, as_of=as_of, horizon=mandate.strategies[0].horizon)
    try:
        final_state = build_graph(run).invoke(state).state
    except BaseException:
        if not run.closed:
            try:
                runs.finish(run_id, "failed", entries={"symbol": sym, "as_of": as_of.isoformat()}, outputs={"reason": "graph_error"}, db=database)
            except runs.RunClosed:
                pass
        raise
    stop = final_state.get("stop")
    return CycleResult(run_id, (final_state.get("final") or {}).get("rating"), run.proposal_id, run.exec_status, tuple(final_state.get("absent", ())),
                       final_state.get("stop_at") if stop else None, stop if isinstance(stop, str) else None,
                       None if run.learning is None else run.learning.status)
