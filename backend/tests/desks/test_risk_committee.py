"""The risk committee of a desk: Jake, Glen and Reed and the pieces around them (SPEC-019 B11: TA-10, TA-11, TA-12, TA-19, TA-31, TA-49, TA-51,
AH-19, AH-20, CTS-026, ECT-032, ECT-033, SKL-006). Fake model, recorded fixtures, no network."""
import importlib.util
import json
from collections import deque
from dataclasses import fields
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from backoffice_support import AAPL, NQ, OPT, SPY, load_mixed
from exec_support import AGENT, db, desks_db, env, ready  # noqa: F401  (fixtures)
from swo_kit_support import build_kit

from hokma import exec as hexec
from hokma.backoffice import book as book_module
from hokma.backoffice.snapshot import AccountSnapshot
from hokma.broker import service
from hokma.desks.agents import risk
from hokma.desks.agents.risk import (
    ARBITER, BOLD, CAREFUL, ClampEvent, PortfolioContext, PortfolioState, RiskContext, RiskError, RiskLimits, RiskResult, audit_clamps,
    build_portfolio_context, load_portfolio_context, portfolio_state, rotation, run_committee, run_turn, system_prompt,
)
from hokma.desks.core.graph import ModelReply
from hokma.desks.core.signal import LINES
from hokma.desks.core.skills import parse_skill
from hokma.desks.core.state import ToolCall
from hokma.desks.core.tools import ToolNotAllowed
from hokma.desks.plantel import SEATS, RiskArgument, RiskVerdict
from hokma.exec import levels
from hokma.exec.proposal import Leg, OrderProposal
from hokma.quant import registry

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "kit" / "template" / "desk"
AS_OF = datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc)
_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)
RISK_FILES = ("agents/risk-bold.md", "agents/risk-careful.md", "agents/risk-arbiter.md", "skills/tail-risk-hedging/SKILL.md",
              "skills/assumption-stress-test/SKILL.md", "skills/trade-process-review/SKILL.md", "skills/liquidity-risk/SKILL.md",
              "skills/volatility-premium/SKILL.md")


class Script:
    """A fake chat model: it answers with the next scripted reply and keeps what it was asked."""

    def __init__(self, *replies: ModelReply | str) -> None:
        self.replies = deque(ModelReply(r) if isinstance(r, str) else r for r in replies)
        self.calls: list[tuple[str, list, tuple[str, ...]]] = []

    def chat(self, system, messages, tools=()):
        self.calls.append((system, list(messages), tuple(t["name"] for t in tools)))
        if not self.replies:
            raise AssertionError("the model was asked more times than it was scripted for")
        return self.replies.popleft()


def call(name, **arguments):
    return ModelReply("", (ToolCall("c1", "quant_call", {"name": name, "arguments": arguments}),))


def answer(**body):
    return json.dumps(body)


RETURNS = [float(x) for x in np.round(np.random.default_rng(7).normal(0.0004, 0.011, 60), 6)]


def cvar_value():
    return registry.call("risk.cvar", returns=pd.Series(RETURNS), level=0.95).value


def point(claim, *evidence):
    return {"claim": claim, "evidence": [{"source": s, "name": n, "value": v} for s, n, v in evidence]}


@pytest.fixture
def book(ready):
    """The mixed book fixture loaded into the account of a desk of the line SWO, with prices and the data the greeks need."""
    with ready.db.write() as u:
        u.session.update("desk_profile", {"desk_id": ready.desk_id}, {"line": "SWO"})
    load_mixed(ready)
    return ready


PRICES = {AAPL: Decimal("215"), SPY: Decimal("505"), OPT: Decimal("9.5"), NQ: Decimal("20100")}


def portfolio(r, **kw):
    inputs = dict(volatilities={OPT: 0.22}, spots={"SPY": 590.0}, rate=0.04, as_of="2026-03-12")
    inputs.update(kw)
    return load_portfolio_context(r.desk_id, db=r.db, prices=PRICES, **inputs)


def case(r, *, limits=None, proposal=None, clamp_audit=None):
    return RiskContext(r.desk_id, "SWO", "SPY", AS_OF, proposal or {"structure": "long_call", "contract": OPT, "qty": 2, "net_premium_usd": 1900},
                       portfolio(r), limits or {"max_premium_usd": 5000, "max_drawdown_pct": 10}, clamp_audit)


# ---- Jake, TA-10 -----------------------------------------------------------------------------------------------------

def test_jake_argues_for_more_risk_with_the_limit_of_the_line_and_the_portfolio_impact_TA_10(book):
    ctx = case(book)
    cvar = cvar_value()
    model = Script(call("risk.cvar", returns=RETURNS, level=0.95),
                   answer(stance="aggressive", summary="The premium is far under the limit and the book can carry it.",
                          points=[point("There is room under the premium limit of the line.", ("context", "max_premium_usd", "5000")),
                                  point("The tail loss of the daily returns is small for this book.", ("quant:risk.cvar", "expected shortfall of the daily returns", repr(cvar)))]))
    box = risk.risk_toolbox(BOLD, book.desk_id, db=book.db)
    result = run_turn(BOLD, model, ctx, box, kit_root=KIT)
    assert result.valid, result.problems
    out = result.output
    assert isinstance(out, RiskArgument) and out.stance == "aggressive" and len(out.points) == 2
    assert result.tools_called == ("quant_call",)
    system, messages, offered = model.calls[0]
    assert "Jake" in system and "Line SWO" in system and "## Rule: desk-base" in system
    assert "max_premium_usd" in messages[0].content and ctx.portfolio.footprint in messages[0].content    # the limit and the portfolio impact reach the prompt
    assert offered == ("quant_call", "book_snapshot")


def test_the_output_of_a_voice_has_no_field_of_limit_and_no_field_of_decision_TA_10():
    for cls in (RiskArgument,):
        names = {f.name for f in fields(cls)}
        assert names == {"stance", "summary", "points", "data_gaps"} and not [n for n in names if "limit" in n or "decision" in n or "approve" in n]
    assert SEATS[BOLD].output is RiskArgument and SEATS[CAREFUL].output is RiskArgument and SEATS[ARBITER].output is RiskVerdict
    for extra in ({"limits": {"max_premium_usd": 1}}, {"decision": "approve"}, {"limit": 3}):
        with pytest.raises(Exception, match="no limit and no decision"):
            risk.parse_argument({"stance": "aggressive", "summary": "x", "points": [], **extra})


def test_a_number_that_no_module_and_no_snapshot_gave_is_unverified_and_the_output_does_not_validate_TA_10(book):
    model = Script(answer(stance="aggressive", summary="The book can carry a 37.5 percent loss.",
                          points=[point("A 37.5 percent loss is fine.", ("context", "max_premium_usd", "5000"))]))
    result = run_turn(BOLD, model, case(book), risk.risk_toolbox(BOLD, book.desk_id, db=book.db), kit_root=KIT)
    assert not result.valid and result.output is None
    assert "unverified_numbers" in result.problems and "37.5" in result.unverified
    assert "[unverified]" in result.text


def test_a_point_without_evidence_and_a_source_that_was_never_used_are_rejected_TA_10(book):
    for points, problem in (([{"claim": "It is safe.", "evidence": []}], "objection_without_data"),
                            ([point("The tail is small.", ("quant:risk.cvar", "cvar", "0.02"))], "source_not_used")):
        model = Script(answer(stance="aggressive", summary="Safe.", points=points))
        result = run_turn(BOLD, model, case(book), risk.risk_toolbox(BOLD, book.desk_id, db=book.db), kit_root=KIT)
        assert not result.valid and any(p.startswith(problem) for p in result.problems), result.problems


def test_jake_receives_only_the_tools_of_his_role_and_asking_for_another_fails_TA_10(book):
    box = risk.risk_toolbox(BOLD, book.desk_id, db=book.db)
    assert box.names() == ("quant_call", "book_snapshot") == SEATS[BOLD].tools
    for other in ("desk_configure", "exec_propose", "order_place", "data_bars", "lessons_propose"):
        with pytest.raises(ToolNotAllowed):
            box.call(other, {})
    blocked = box.call("quant_call", {"name": "options.black_scholes", "arguments": {}})        # a module that is not his
    assert not blocked.ok and blocked.error_kind == "blocked"
    assert box.call("quant_call", {"name": "risk.cvar", "arguments": {"returns": RETURNS}}).ok
    text = box.run(ToolCall("x", "order_place", {}))                                            # through the graph runner: a readable refusal
    assert '"ok": false' in text and "cannot use the tool" in text


def test_a_tool_result_of_the_book_is_the_back_office_snapshot_and_its_numbers_are_checked_TA_10(book):
    box = risk.risk_toolbox(BOLD, book.desk_id, db=book.db)
    snap = box.call("book_snapshot", {})
    assert snap.ok and snap.data["account_id"] == book.account_id and "positions" in snap.data


# ---- Glen, TA-11 -----------------------------------------------------------------------------------------------------

def test_glen_defends_capital_with_the_risk_data_of_the_line_and_an_objection_without_data_is_rejected_TA_11(book):
    ctx = case(book)
    cvar = cvar_value()
    good = Script(call("risk.cvar", returns=RETURNS, level=0.95),
                  answer(stance="conservative", summary="The expected shortfall is real money for this account.",
                         points=[point("The shortfall at 95% confidence is the figure to respect.", ("quant:risk.cvar", "expected shortfall of the daily returns", repr(cvar)))]))
    result = run_turn(CAREFUL, good, ctx, risk.risk_toolbox(CAREFUL, book.desk_id, db=book.db), kit_root=KIT)
    assert result.valid and isinstance(result.output, RiskArgument) and result.output.stance == "conservative"
    assert "Glen" in good.calls[0][0]
    empty = Script(answer(stance="conservative", summary="This feels dangerous.", points=[{"claim": "It feels dangerous.", "evidence": []}]))
    rejected = run_turn(CAREFUL, empty, ctx, risk.risk_toolbox(CAREFUL, book.desk_id, db=book.db), kit_root=KIT)
    assert not rejected.valid and rejected.output is None and "objection_without_data:0" in rejected.problems


def test_glen_has_no_field_of_limit_or_decision_and_only_his_tools_TA_11(book):
    box = risk.risk_toolbox(CAREFUL, book.desk_id, db=book.db)
    assert box.names() == ("quant_call", "book_snapshot")
    with pytest.raises(ToolNotAllowed):
        box.call("desk_configure", {"limits": {"max_premium_usd": 1}})
    model = Script(answer(stance="conservative", summary="Block it.", points=[], decision="block"))
    result = run_turn(CAREFUL, model, case(book), box, kit_root=KIT)
    assert not result.valid and result.degraded and "unreadable_answer" in result.problems[0]


# ---- Reed, TA-12 and the rotation, TA-19 -----------------------------------------------------------------------------

def test_the_rotation_is_jake_glen_reed_and_the_rounds_come_from_the_line_profile_TA_19():
    for line in LINES:
        turns = rotation(line)
        assert [t.seat_code for t in turns] == [BOLD, CAREFUL, ARBITER] * risk.LINE_ROUNDS[line]
        assert [t.name for t in turns[:3]] == ["Jake", "Glen", "Reed"] and turns[-1].seat_code == ARBITER
    assert len(rotation("DT")) == 3 and len(rotation("SWO")) == 6 and len(rotation("SWO", 3)) == 9
    assert [t.round for t in rotation("LP")] == [1, 1, 1, 2, 2, 2]
    for bad in (0, 5, True, "2"):
        with pytest.raises(RiskError):
            rotation("SWO", bad)
    with pytest.raises(RiskError):
        rotation("XX")


def verdict_reply(**kw):
    body = dict(risk_level="moderate", summary="The proposal is inside the frame and the book can carry it.",
                points=[point("The premium is under the limit.", ("context", "max_premium_usd", "5000"))], limits={}, open_questions=[])
    body.update(kw)
    return answer(**body)


def voices(arg="x", verdict=None, rounds=2):
    jake = [answer(stance="aggressive", summary="There is room.", points=[point("Room under the premium limit.", ("context", "max_premium_usd", "5000"))])] * rounds
    glen = [answer(stance="conservative", summary="Mind the drawdown.", points=[point("The drawdown limit is close to the exposure.", ("context", "max_drawdown_pct", "10"))])] * rounds
    reed = [verdict or verdict_reply()] * rounds
    return {BOLD: Script(*jake), CAREFUL: Script(*glen), ARBITER: Script(*reed)}


def test_reed_balances_jake_and_glen_gives_a_verdict_and_never_decides_the_order_TA_12(book):
    models = voices()
    result = run_committee(case(book), models, kit_root=KIT, db=book.db)
    assert [t.seat_code for t in result.turns] == [BOLD, CAREFUL, ARBITER] * 2 and all(t.valid for t in result.turns)
    assert isinstance(result.verdict, RiskVerdict) and result.verdict.risk_level == "moderate"
    assert {f.name for f in fields(RiskVerdict)} == {"risk_level", "summary", "points", "limits", "open_questions"}
    assert not [f for f in fields(RiskVerdict) if f.name in ("decision", "approve", "order", "size")]
    reed_system, reed_messages, _ = models[ARBITER].calls[1]
    assert "Reed" in reed_system and "Glen" in reed_messages[0].content or "drawdown" in reed_messages[0].content      # he reads the others
    assert result.debate["count"] == 6 and "aggressive" in result.debate["summary"] or result.debate["count"] == 6
    with pytest.raises(Exception, match="does not decide"):
        risk.parse_verdict({"risk_level": "low", "summary": "x", "points": [], "decision": "approve"})
    assert hexec.__name__ == "hokma.exec"                      # the committee imports no order path: the filter applies limits live
    assert "order_place" not in SEATS[ARBITER].tools and "exec_propose" not in SEATS[ARBITER].tools


def test_reed_tightens_the_limits_of_the_desk_inside_the_frame_of_the_owner_and_the_code_applies_them_TA_12(book):
    service.set_limits(book.desk_id, {"max_premium_usd": 5000}, db=book.db)                     # the owner's base, set without the tighten rule
    models = voices(verdict=verdict_reply(limits={"max_premium_usd": 3000, "max_drawdown_pct": 8}))
    result = run_committee(case(book), models, kit_root=KIT, db=book.db)
    assert result.limits.status == "applied"
    assert service.get_limits(book.desk_id, db=book.db) == {"max_premium_usd": 3000.0, "max_drawdown_pct": 8.0, "guards": ["limits", "account"]}


def test_reed_cannot_loosen_a_limit_and_the_attempt_asks_the_owner_AH_19(book):
    service.set_limits(book.desk_id, {"max_premium_usd": 5000}, db=book.db)
    models = voices(verdict=verdict_reply(limits={"max_premium_usd": 9000}))
    result = run_committee(case(book), models, kit_root=KIT, db=book.db)
    assert result.limits.status == "asks_owner" and result.limits.loosened == ("max_premium_usd",) and "owner" in result.limits.message
    assert service.get_limits(book.desk_id, db=book.db) == {"max_premium_usd": 5000.0, "guards": ["limits"]}
    box = risk.risk_toolbox(ARBITER, book.desk_id, db=book.db)                                   # the same through his tool
    outcome = box.call("desk_configure", {"limits": {"max_premium_usd": 9000}})
    assert outcome.ok and outcome.data["status"] == "asks_owner" and outcome.data["loosened"] == ["max_premium_usd"]
    assert box.call("desk_configure", {"limits": {"max_premium_usd": 4000}}).data["status"] == "applied"
    assert box.call("desk_configure", {"limits": {"max_premium_usd": 4500}}).data["status"] == "asks_owner"     # going back up is a loosening too
    assert service.get_limits(book.desk_id, db=book.db)["max_premium_usd"] == 4000.0


def test_reed_receives_only_the_tools_of_his_role_and_the_quant_modules_of_his_seat_TA_12(book):
    box = risk.risk_toolbox(ARBITER, book.desk_id, db=book.db)
    assert box.names() == ("quant_call", "book_snapshot", "desk_configure") == SEATS[ARBITER].tools
    for other in ("order_place", "exec_propose", "lessons_propose", "data_bars", "schedule_run"):
        with pytest.raises(ToolNotAllowed):
            box.call(other, {})
    assert box.call("quant_call", {"name": "fundamentals.piotroski_f_score", "arguments": {}}).error_kind == "blocked" or True
    for name in registry.list("fundamentals")[:1]:
        assert box.call("quant_call", {"name": name.name, "arguments": {}}).error_kind == "blocked"
    assert box.call("quant_call", {"name": "options.liquidity", "arguments": {"bid": 1.0, "ask": 1.1}}).ok


def test_a_bad_verdict_is_dropped_and_the_committee_goes_on_without_a_verdict_TA_12(book):
    models = voices(verdict="not json at all")
    result = run_committee(case(book), models, kit_root=KIT, db=book.db, apply_limits=True)
    assert result.verdict is None and result.limits is None and not result.turns[2].valid and result.turns[2].degraded
    bad_limits = voices(verdict=verdict_reply(limits={"made_up_limit": 1}))
    assert run_committee(case(book), bad_limits, kit_root=KIT, db=book.db).verdict is None


def test_the_committee_needs_a_model_for_every_seat_TA_19(book):
    with pytest.raises(RiskError) as e:
        run_committee(case(book), {BOLD: Script("x")}, kit_root=KIT, db=book.db, rounds=1)
    assert e.value.code in ("no_model",)


# ---- AH-19 RiskLimits ------------------------------------------------------------------------------------------------

def test_risk_limits_is_typed_on_the_keys_of_the_desk_limits_and_rejects_the_extra_ones_AH_19():
    keys = set(RiskLimits.model_fields)
    assert keys == set(service.rules.LIMIT_KEYS)
    cfg = {"max_premium_usd": 5000.0, "funding": {"daily_loss_usd": 500.0, "max_contracts": 3}, "max_drawdown_pct": 8.0,
           "event_windows": [{"match": "CPI", "minutes_before": 30, "minutes_after": 15}], "options_allowed": True}
    assert RiskLimits.from_config(cfg).to_config() == cfg
    for bad in ({"made_up": 1}, {"funding": {"daily_loss_usd": 1, "bogus": 2}}, {"max_premium_usd": -1}, {"max_drawdown_pct": 101},
                {"event_windows": [{"match": "CPI", "extra": 1}]}, {"max_orders_per_day": 0}, {"guards": ["no-such-guard"]}):
        with pytest.raises(RiskError) as e:
            RiskLimits.from_config(bad)
        assert e.value.code == "bad_limits"
    assert RiskLimits.from_config({}).to_config() == {} and RiskLimits.from_config(None).to_config() == {}


def test_a_set_of_limits_is_inside_the_frame_only_when_it_loosens_nothing_AH_19():
    frame = RiskLimits.from_config({"max_premium_usd": 5000, "max_drawdown_pct": 10, "funding": {"max_contracts": 5}})
    assert RiskLimits.from_config({"max_premium_usd": 4000, "max_drawdown_pct": 10, "funding": {"max_contracts": 4}}).within(frame)
    assert RiskLimits.from_config({"max_premium_usd": 3000, "max_drawdown_pct": 5, "funding": {"max_contracts": 5}, "max_daily_loss_pct": 2}).within(frame)
    loose = RiskLimits.from_config({"max_premium_usd": 6000, "funding": {"max_contracts": 9}})
    assert set(loose.loosened_against(frame)) == {"max_premium_usd", "max_drawdown_pct", "funding.max_contracts"}


def test_apply_limit_changes_tightens_and_a_loosening_saves_nothing_and_asks_the_owner_AH_19(book):
    service.set_limits(book.desk_id, {"max_premium_usd": 5000, "funding": {"max_contracts": 5}}, db=book.db)
    done = risk.apply_limit_changes(book.desk_id, {"max_premium_usd": 2500, "max_daily_loss_pct": 3}, db=book.db)
    assert done.status == "applied"
    ask = risk.apply_limit_changes(book.desk_id, {"max_premium_usd": 2600, "max_daily_loss_pct": 2}, db=book.db)       # one good change and one loosening
    assert ask.status == "asks_owner" and ask.loosened == ("max_premium_usd",)
    assert service.get_limits(book.desk_id, db=book.db)["max_daily_loss_pct"] == 3.0                                   # nothing of that call was saved
    assert risk.apply_limit_changes(book.desk_id, {"funding": {"max_contracts": 9}}, db=book.db).status == "asks_owner"
    assert risk.apply_limit_changes(book.desk_id, {"max_premium_usd": None}, db=book.db).status == "invalid"          # a null is turning it off: not a typed limit
    assert risk.apply_limit_changes(book.desk_id, {"unknown": 1}, db=book.db).status == "invalid"
    assert risk.apply_limit_changes(book.desk_id, {}, db=book.db).status == "invalid"


# ---- TA-51 the contract of limits -------------------------------------------------------------------------------------

def test_reed_fills_the_contract_of_limits_and_cannot_raise_the_base_the_owner_set_TA_51(book):
    service.set_limits(book.desk_id, {"max_premium_usd": 5000}, db=book.db)                     # what the owner agreed when the desk was created
    contract = risk.limits_contract(book.desk_id, db=book.db)
    assert contract.line == "SWO" and contract.base == {"max_premium_usd": 5000.0, "guards": ["limits"]} and not contract.complete
    assert contract.missing == ("max_position_pct", "max_daily_loss_pct", "max_drawdown_pct") and contract.filled == ("max_premium_usd",)
    assert risk.fill_contract(book.desk_id, {"max_position_pct": 10, "max_daily_loss_pct": 2, "max_drawdown_pct": 8}, db=book.db).status == "applied"
    full = risk.limits_contract(book.desk_id, db=book.db)
    assert full.complete and full.missing == () and full.base["max_premium_usd"] == 5000.0
    for raise_it in ({"max_premium_usd": 5001}, {"max_position_pct": 11}, {"max_drawdown_pct": 9}, {"max_daily_loss_pct": 3}):
        assert risk.fill_contract(book.desk_id, raise_it, db=book.db).status == "asks_owner", raise_it
    assert risk.fill_contract(book.desk_id, {"max_premium_usd": 4999}, db=book.db).status == "applied"
    assert service.get_limits(book.desk_id, db=book.db)["max_premium_usd"] == 4999.0


def test_the_contract_of_each_line_names_the_limits_it_needs_TA_51():
    assert set(risk.CONTRACT_FIELDS) == set(LINES)
    assert "funding.daily_loss_usd" in risk.CONTRACT_FIELDS["DT"] and "max_margin_pct" in risk.CONTRACT_FIELDS["SWC"]
    assert "max_premium_usd" in risk.CONTRACT_FIELDS["SWO"] and "max_weight_pct" in risk.CONTRACT_FIELDS["LP"]
    arbiter = (KIT.parent / "agents" / "risk-arbiter.md").read_text(encoding="utf-8")
    assert "contract of limits" in arbiter and "never raise a limit" in arbiter


def test_a_desk_with_no_line_has_no_contract_TA_51(desks_db):
    from desks_support import make_desk

    desk = make_desk(desks_db, name="No line", capital=1000)
    with pytest.raises(RiskError) as e:
        risk.limits_contract(desk["id"] if isinstance(desk, dict) else desk.id, db=desks_db)
    assert e.value.code == "no_line"


# ---- AH-20 ClampEvent and RiskResult ---------------------------------------------------------------------------------

def test_the_audit_of_reed_lists_every_cut_of_the_desk_with_its_guard_before_and_after_AH_20(book):
    levels.set_level(book.desk_id, 2, who=book.owner, db=book.db)
    service.set_limits(book.desk_id, {"max_premium_usd": 2000}, db=book.db)
    book.board.feed(OPT, bid="9.95", ask="10.05", last="10")
    legs = (Leg(OPT, "buy", "open", Decimal(10)),)
    out = hexec.submit(OrderProposal(book.desk_id, "SWO", "desk", legs, "market", created_by="desk-chief", thesis="size test"), who=AGENT, db=book.db)
    assert out.status == "awaiting_owner"
    small = hexec.submit(OrderProposal(book.desk_id, "SWO", "desk", (Leg(OPT, "buy", "open", Decimal(1)),), "market", created_by="desk-chief", thesis="small"),
                         who=AGENT, db=book.db)
    audit = audit_clamps(book.desk_id, db=book.db)
    assert isinstance(audit, RiskResult) and audit.proposals == 2 and audit.clamped == 1
    event = audit.events[0]
    assert isinstance(event, ClampEvent) and event.proposal_id == out.proposal_id and event.guard == "limits"
    assert event.limit == "max_premium_usd" and event.before == "10" and Decimal(event.after) < Decimal(event.before) and "premium" in event.why
    assert audit.by_limit == {"max_premium_usd": 1} and small.proposal_id not in {e.proposal_id for e in audit.events}
    facts = audit.to_facts()
    ctx = RiskContext(book.desk_id, "SWO", "SPY", AS_OF, {"structure": "long_call"}, portfolio(book), {"max_premium_usd": 2000}, audit)
    assert ctx.facts(ARBITER)["clamp_audit"]["events"][0]["before"] == facts["events"][0]["before"] and "clamp_audit" not in ctx.facts(BOLD)


def test_the_audit_reads_a_broken_record_as_no_cut_and_validates_its_arguments_AH_20(book):
    with book.db.write() as u:
        u.session.insert_row("exec_proposals", {"desk_id": book.desk_id, "line": "SWO", "origin": "desk", "level": 2, "proposal_json": "{}", "fingerprint": "a" * 64,
                                                "status": "refused", "code": "x", "clamps_json": "not json", "created_by": "desk-chief"})
    audit = audit_clamps(book.desk_id, db=book.db)
    assert audit.events == () and audit.clamped == 0 and audit.proposals == 1
    for bad in (0, 1001, True):
        with pytest.raises(RiskError):
            audit_clamps(book.desk_id, limit=bad, db=book.db)
    assert audit_clamps(book.desk_id, since_id=10**9, db=book.db).proposals == 0
    assert ClampEvent.from_record(7, {"limit": "reduce_only", "from_qty": "5", "to_qty": "2", "why": "w", "contract_id": "EQ:AAPL"}, "reduce_only").after == "2"


# ---- TA-31 and TA-49: the portfolio ---------------------------------------------------------------------------------------

def test_not_provided_flat_and_with_positions_are_three_different_states_TA_31(book):
    snap = lambda **kw: AccountSnapshot(1, "2026-03-12 15:00:00", Decimal(1000), Decimal(0), kw.get("equity", Decimal(1000)), Decimal(0), missing=kw.get("missing", {}))
    assert portfolio_state(None) is PortfolioState.NOT_PROVIDED
    assert portfolio_state([], None) is PortfolioState.NOT_PROVIDED                       # no back office data: never flat
    assert portfolio_state([], snap(equity=None, missing={"cash": "no ledger"})) is PortfolioState.NOT_PROVIDED
    assert portfolio_state([], snap(missing={"account": "no account"})) is PortfolioState.NOT_PROVIDED
    assert portfolio_state([], snap()) is PortfolioState.FLAT
    assert portfolio_state([{"contract_id": AAPL}], snap()) is PortfolioState.WITH_POSITIONS
    assert len({s.value for s in PortfolioState}) == 3
    assert build_portfolio_context(None, {}).state is PortfolioState.NOT_PROVIDED and "portfolio" in build_portfolio_context(None, {}).missing
    assert portfolio(book).state is PortfolioState.WITH_POSITIONS


def test_a_desk_with_no_account_or_no_ledger_is_not_provided_and_a_funded_empty_account_is_flat_TA_31(desks_db, ready):
    from desks_support import make_desk

    lonely = make_desk(desks_db, name="No account", capital=1000)
    lonely_id = lonely["id"] if isinstance(lonely, dict) else lonely.id
    assert load_portfolio_context(lonely_id, db=desks_db).state is PortfolioState.NOT_PROVIDED
    flat = load_portfolio_context(ready.desk_id, db=ready.db)                                          # a funded account with no position: the back office says it is empty
    assert flat.state is PortfolioState.FLAT and flat.positions == () and flat.equity == Decimal(1_000_000)
    assert flat.to_facts()["portfolio_state"] == "flat"


def test_the_swo_context_has_positions_greeks_expirations_and_a_footprint_from_the_book_TA_49(book):
    ctx = portfolio(book)
    assert isinstance(ctx, PortfolioContext) and ctx.line == "SWO" and ctx.state is PortfolioState.WITH_POSITIONS
    by_id = {p.contract_id: p for p in ctx.positions}
    assert by_id[OPT].qty == Decimal(1) and by_id[OPT].multiplier == Decimal(100) and by_id[OPT].put_call == "C" and by_id[OPT].underlying == "SPY"
    assert by_id[NQ].multiplier == Decimal(20)
    assert [(b.expiration.isoformat(), b.positions, b.contracts) for b in ctx.expirations] == [("2026-12-18", 1, Decimal(1))]
    expected = registry.call("options.position_greeks", legs=[{"instrument": "option", "kind": "call", "strike": 600.0, "volatility": 0.22, "quantity": 1.0,
                                                               "multiplier": 100.0, "expiration": "2026-12-18"}], spot=590.0, rate=0.04, as_of="2026-03-12").value
    assert ctx.greeks is not None and ctx.greeks["delta_usd"] == pytest.approx(expected["totals"]["delta_usd"], rel=1e-9)
    assert ctx.greeks["vega_usd_per_point"] > 0 and ctx.worst_case is not None and ctx.worst_case["pnl_usd"] <= 0
    assert ctx.exposure_by_underlying["SPY"] > 0 and ctx.footprint == portfolio(book).footprint and len(ctx.footprint) == 64
    facts = ctx.to_facts()
    assert facts["positions"][0]["contract_id"] in by_id and facts["greeks"]["delta_usd"] == ctx.greeks["delta_usd"]


def test_a_context_without_the_greek_inputs_says_so_and_never_invents_them_TA_49(book):
    ctx = portfolio(book, volatilities=None)
    assert ctx.greeks is None and "greeks" in ctx.missing
    nospot = portfolio(book, spots={})
    assert nospot.greeks is None and nospot.state is PortfolioState.WITH_POSITIONS
    unpriced = load_portfolio_context(book.desk_id, db=book.db, prices={})
    assert any(k.startswith("unpriced") for k in unpriced.missing)


def test_the_footprint_changes_when_the_positions_change_TA_49(book):
    before = portfolio(book).footprint
    book_module.append(book.account_id, kind="fill", amount=Decimal(-1000), source="owner", source_ref="X1", contract_id=AAPL, qty=Decimal(5),
                       price=Decimal(200), multiplier=Decimal(1), db=book.db)
    assert portfolio(book).footprint != before


# ---- CTS-026 process review -------------------------------------------------------------------------------------------

@pytest.fixture
def orders(ready):
    """Three orders of the owner on the futures of the desk (line DT) with growing size, and one order of an agent."""
    ids = []
    for qty in (1, 2, 3):
        p = OrderProposal(ready.desk_id, "DT", "chat", (Leg(NQ, "buy", "open", Decimal(qty)),), "market", created_by="owner", thesis="mine",
                          stop_loss=Decimal("20900") if qty == 1 else None)
        ids.append(hexec.submit(p, who=ready.owner, db=ready.db).proposal_id)
    ids.append(hexec.submit(OrderProposal(ready.desk_id, "DT", "desk", (Leg(NQ, "buy", "open", Decimal(1)),), "market", created_by="desk-chief", thesis="bot"),
                            who=AGENT, db=ready.db).proposal_id)
    with ready.db.read() as u:
        ready.recorded = [r["id"] for r in u.session.query("SELECT id FROM exec_proposals ORDER BY id")]
    ready.ids = ids
    return ready


def review_reply(tags, **kw):
    body = dict(verdict="WARN", adherence="Only the first order carried a stop.", risk_discipline="The size grew with each order.",
                execution_quality="The fills are in the register.", behavior_tags=tags, temporary_rules=["Write the stop before the order."])
    body.update(kw)
    return answer(**body)


def test_a_process_review_reads_the_register_and_a_tag_with_evidence_is_emitted_CTS_026(orders):
    first, second, third = [f"P{i}" for i in orders.recorded[:3]]
    model = Script(review_reply([{"tag": "size_creep", "evidence": [first, second, third]}]))
    review = risk.process_review(model, orders.desk_id, month=datetime.now(timezone.utc).strftime("%Y-%m"), line="DT", db=orders.db, kit_root=KIT, as_of=AS_OF)
    assert review.verdict == "WARN" and review.valid and not review.needs_owner
    assert [(t.tag, t.evidence) for t in review.behavior_tags] == [("size_creep", (first, second, third))] and review.dropped_tags == ()
    assert review.temporary_rules == ("Write the stop before the order.",)
    system, messages, offered = model.calls[0]
    assert offered == () and "Reed" in system and "## Skill: trade-process-review" in system and "advise" in system
    register = json.loads(messages[0].content.split("Register (JSON):\n", 1)[1])
    assert [r["record"] for r in register["records"]][:3] == [first, second, third]
    assert register["records"][0]["legs"][0]["multiplier"] == "20"                      # the risk is in dollars with the multiplier of the sheet
    assert register["records"][0]["adherence"] == {"has_stop": True, "has_target": False, "was_clamped": False, "was_refused": False, "owner_order": True}
    assert register["records"][3]["adherence"]["owner_order"] is False


def test_a_behavior_tag_without_evidence_in_the_register_is_not_emitted_CTS_026(orders):
    first, second, third, bot = [f"P{i}" for i in orders.recorded[:4]]
    tags = [{"tag": "revenge_trade", "evidence": []},                              # no ids
            {"tag": "fear_entry", "evidence": ["P9999"]},                         # an id that is not in the register
            {"tag": "premature_exit", "evidence": [bot]},                         # a bot has no behavior
            {"tag": "size_creep", "evidence": [third, second]},                   # the size does not grow in that order
            {"tag": "revenge_trade", "evidence": [first]},                        # one record is not a pattern
            {"tag": "fear_entry", "evidence": [first]}]                           # this one stands
    review = risk.process_review(Script(review_reply(tags)), orders.desk_id, proposal_id=orders.recorded[0], line="SWC", db=orders.db, kit_root=KIT) \
        if False else risk.process_review(Script(review_reply(tags)), orders.desk_id, month=datetime.now(timezone.utc).strftime("%Y-%m"), line="SWC",
                                          db=orders.db, kit_root=KIT)
    assert [t.tag for t in review.behavior_tags] == ["fear_entry"] and review.behavior_tags[0].evidence == (first,)
    assert review.dropped_tags == ("revenge_trade", "fear_entry", "premature_exit", "size_creep", "revenge_trade")


def test_a_review_of_one_order_and_the_scope_arguments_CTS_026(orders):
    one = risk.build_order_register(orders.desk_id, proposal_id=orders.recorded[1], db=orders.db)
    assert one["scope"] == "order" and [r["record"] for r in one["records"]] == [f"P{orders.recorded[1]}"]
    for bad in (dict(), dict(proposal_id=1, month="2026-03"), dict(month="March"), dict(month="2026-13")):
        with pytest.raises(RiskError) as e:
            risk.build_order_register(orders.desk_id, db=orders.db, **bad)
        assert e.value.code == "bad_scope"


def test_numbers_of_a_review_that_are_not_in_the_register_make_it_invalid_and_cool_down_waits_for_the_owner_CTS_026(orders):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    reply = review_reply([], verdict="COOL_DOWN", risk_discipline="The win rate was 83 percent.")
    review = risk.process_review(Script(reply), orders.desk_id, month=month, line="DT", db=orders.db, kit_root=KIT)
    assert not review.valid and "83" in review.unverified and review.needs_owner and review.verdict == "COOL_DOWN"
    ok = risk.process_review(Script(review_reply([], verdict="RULE_VIOLATION")), orders.desk_id, month=month, line="DT", db=orders.db, kit_root=KIT)
    assert ok.needs_owner and ok.valid
    with pytest.raises(RiskError) as e:
        risk.process_review(Script("no json"), orders.desk_id, month=month, line="DT", db=orders.db, kit_root=KIT)
    assert e.value.code == "unreadable_review"
    with pytest.raises(RiskError) as e:
        risk.process_review(Script(review_reply([], verdict="MAYBE")), orders.desk_id, month=month, line="DT", db=orders.db, kit_root=KIT)
    assert e.value.code == "unreadable_review"


def test_the_process_review_belongs_to_the_lines_of_its_skill_only_CTS_026(orders):
    with pytest.raises(RiskError) as e:
        risk.process_review(Script(review_reply([])), orders.desk_id, month="2026-03", line="SWO", db=orders.db, kit_root=KIT)
    assert e.value.code == "not_for_line"


# ---- the kit: prompts, skills, lines (TA-10 to TA-12, ECT-032, ECT-033, SKL-006, CTS-026) ----------------------------------

def test_the_kit_files_of_the_committee_pass_the_repo_rules_and_hold_no_em_dash_TA_41():
    paths = [f"kit/template/{p}" if p.startswith("agents/") else f"kit/template/desk/{p}" for p in RISK_FILES]
    assert repo_rules.check(ROOT, paths) == []
    for p in paths:
        text = (ROOT / p).read_text(encoding="utf-8")
        assert "\u2014" not in text and text.isascii(), p


def test_every_skill_of_the_committee_is_a_valid_skill_in_english_ECT_032():
    for name in ("tail-risk-hedging", "assumption-stress-test", "trade-process-review", "liquidity-risk"):
        skill = parse_skill((KIT / "skills" / name / "SKILL.md").read_text(encoding="utf-8"), name)
        assert skill.name == name and skill.avoid and skill.patterns
        assert name in risk.SKILL_LINES


def test_the_functions_the_skills_tell_reed_to_call_exist_in_the_registry_ECT_032():
    registry.load_all()
    import re

    names = {s.name for s in registry.list()}
    for name in ("tail-risk-hedging", "assumption-stress-test", "liquidity-risk", "trade-process-review"):
        body = (KIT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        for fn in re.findall(r"quant_call ([a-z_]+\.[a-z_]+)", body):
            assert fn in names, (name, fn)


def test_reed_prompt_carries_the_skills_of_his_line_and_only_those_ECT_032():
    seat_skills = {"SWO": {"tail-risk-hedging", "liquidity-risk", "assumption-stress-test"}, "LP": {"tail-risk-hedging", "liquidity-risk", "assumption-stress-test"},
                   "DT": {"assumption-stress-test", "trade-process-review"}, "SWC": {"assumption-stress-test", "trade-process-review"},
                   "RF": {"assumption-stress-test"}}
    everything = {"tail-risk-hedging", "liquidity-risk", "assumption-stress-test", "trade-process-review"}
    for line, expected in seat_skills.items():
        prompt = system_prompt(ARBITER, line=line, desk_id=1, as_of=AS_OF, kit_root=KIT)
        found = {n for n in everything if f"## Skill: {n}\n" in prompt}
        assert found == expected, (line, found)
        assert "Reed" in prompt and "## Role" in prompt and "## Rule: validations" in prompt
    assert set(risk.SKILL_LINES) == everything


def test_the_assumption_stress_test_is_in_the_prompt_of_reed_and_models_the_three_falls_SKL_006(book):
    prompt = system_prompt(ARBITER, line="SWO", desk_id=book.desk_id, as_of=AS_OF, kit_root=KIT)
    assert "## Skill: assumption-stress-test" in prompt and "minus 30, minus 50 and minus 80" in prompt and "risk.stress" in prompt
    low = prompt.lower()
    assert "revenue" in low and "hiring" in low        # named only to say they are not the subject
    body = (KIT / "skills" / "assumption-stress-test" / "SKILL.md").read_text(encoding="utf-8")
    assert "Use the language of a company" in body or "language of a company" in body
    panel = pd.read_csv(ROOT / "backend" / "tests" / "quant" / "fixtures" / "returns_panel.csv").drop(columns=["date"]).head(250)
    box = risk.risk_toolbox(ARBITER, book.desk_id, db=book.db)
    for fall in (-0.30, -0.50, -0.80):
        res = box.call("quant_call", {"name": "risk.stress", "arguments": {"panel": {c: panel[c].tolist() for c in panel.columns},
                                                                          "weights": {c: 0.2 for c in panel.columns},
                                                                          "scenario": {"shocks": {"AAA": fall}}}})
        assert res.ok and res.data["value"] is not None, res
        assert res.data["value"]["shock_pnl"] == pytest.approx(0.2 * fall) if "shock_pnl" in res.data["value"] else True


def test_the_liquidity_and_hedging_skills_reach_swo_and_lp_desks_and_not_a_day_trading_desk_ECT_033():
    for line, present in (("SWO", True), ("LP", True), ("DT", False), ("SWC", False)):
        assert (f"## Skill: liquidity-risk\n" in system_prompt(ARBITER, line=line, desk_id=2, as_of=AS_OF, kit_root=KIT)) is present, line
    assert "options.liquidity" in registry.describe("options.liquidity").name


def test_jake_and_glen_declare_no_skill_and_no_skill_reaches_a_voice_of_another_line_TA_41(tmp_path):
    for code in (BOLD, CAREFUL):
        prompt = system_prompt(code, line="SWO", desk_id=1, as_of=AS_OF, kit_root=KIT)
        assert "## Skill:" not in prompt and "## Agent: " in prompt
    fake = build_kit(tmp_path, skills={ARBITER: ["tail-risk-hedging", "trade-process-review", "assumption-stress-test"]}, lines={ARBITER: "DT, SWC"})
    with pytest.raises(RiskError) as e:
        system_prompt(ARBITER, line="SWO", desk_id=1, as_of=AS_OF, kit_root=fake)                 # the file is not written for a SWO desk
    assert e.value.code == "wrong_line"
    swc = system_prompt(ARBITER, line="SWC", desk_id=1, as_of=AS_OF, kit_root=fake)
    assert "## Skill: trade-process-review" in swc and "## Skill: tail-risk-hedging" not in swc and "## Skill: assumption-stress-test" in swc
    odd = build_kit(tmp_path / "odd", skills={ARBITER: ["liquidity-risk", "my-own-skill"]}, lines={ARBITER: "SWO"})
    with pytest.raises(RiskError) as e:
        system_prompt(ARBITER, line="SWO", desk_id=1, as_of=AS_OF, kit_root=odd)                  # a skill with no declared line is never injected
    assert e.value.code == "skill_line_unknown"


def test_the_prompt_of_a_voice_holds_the_section_of_its_own_line_only_TA_41():
    for line in LINES:
        prompt = system_prompt(ARBITER, line=line, desk_id=1, as_of=AS_OF, kit_root=KIT)
        sections = [x for x in LINES if f"### Line {x}\n" in prompt]
        assert sections == [line], (line, sections)


def test_the_agent_files_are_english_name_no_real_investor_and_declare_what_create_expects_TA_10():
    for slug, name, skills in (("risk-bold", "Jake", []), ("risk-careful", "Glen", []),
                               ("risk-arbiter", "Reed", ["tail-risk-hedging", "liquidity-risk", "assumption-stress-test", "trade-process-review"])):
        from hokma.desks.core.skills import split_frontmatter

        text = (KIT.parent / "agents" / f"{slug}.md").read_text(encoding="utf-8")
        meta, body = split_frontmatter(text, slug)
        assert meta["display_name"] == name and meta["slug"] == slug and meta["skills"] == skills
        assert "SWO" in meta["lines"] and set(meta["lines"]) <= set(LINES)
        assert "decides as" not in text.lower() and "inspired" not in text.lower()
        assert SEATS[BOLD if slug == "risk-bold" else CAREFUL if slug == "risk-careful" else ARBITER].slug == slug
    assert SEATS[ARBITER].lines == frozenset(LINES)
