"""The eight investor profiles as the company filter of a SWO desk (SPEC-019: B8): A16 to A22, their criteria rows and AH-16.

One test name per piece id. A scripted chat model stands in for the language model; the criteria come from the real fundamentals module
through the registry, on the recorded statements of three companies. Nothing here touches the network."""
from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from b8_support import AS_OF, CUT, KIT, ROOT, Chat, LeanChat, candles, load_registry, reply, statements
from dbsupport import db, env  # noqa: F401  (fixtures)
from swo_kit_support import build_kit

from hokma.desks.agents import analyst_kit as kit
from hokma.desks.agents import investors as inv
from hokma.desks.agents.manager import parse_mandate
from hokma.desks.core.signal import Criterion, Signal, SignalContext, contains_execution_terms
from hokma.desks.core.skills import split_frontmatter
from hokma.desks.plantel import SEATS, seats_for
from hokma.desks.swo.create import create_swo_desk
from hokma.quant import registry
from hokma.quant.fundamentals import criteria as crit

_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)

CHEAP = 18          # market cap 1,764 against a buy-below value of 1,806 (owner earnings of ALFA)
VERY_CHEAP = 12     # a value gap of more than 30 percent
FUTURE_CATALYST = {"date": "2024-09-01", "description": "product launch"}
PAST_CATALYST = {"date": "2024-05-01", "description": "product launch"}


@pytest.fixture(autouse=True)
def registry_ready():
    load_registry()


def data(company="ALFA", *, regime="neutral", catalyst=FUTURE_CATALYST, with_candles=True, **last) -> inv.CompanyData:
    return inv.CompanyData(statements(company, **last), candles=candles() if with_candles else None, regime=regime, catalyst=catalyst)


def context(code, *, symbol="NVDA", params=None, **kw) -> SignalContext:
    return inv.build_context(inv.PROFILES[code], desk_id=1, line="SWO", symbol=symbol, as_of=AS_OF, data=data(**kw), params=params)


def manual(code, lean, *, cap=100, **facts) -> SignalContext:
    """A context written by hand, to check what the code refuses whatever the criteria were."""
    names = {"A16": "roe_average", "A17": "margin_change", "A18": "price_to_earnings", "A19": "peg", "A20": "revenue_growth_acceleration",
             "A21": "value_gap", "A22": "rd_intensity"}
    return SignalContext(source=code, desk_id=1, line="SWO", symbol="NVDA", as_of=AS_OF, horizon="weeks", snapshot_hash="hash",
                         facts={"lean": lean, "confidence_cap": cap, **facts}, criteria=(Criterion(names[code], 0.2, 0.1, True, "fundamentals.test"),),
                         required=(), confidence_cap=cap)


def agent(code, chat) -> inv.InvestorAgent:
    return inv.InvestorAgent(code, chat)


def by_name(signal: Signal) -> dict:
    return {c.name: c for c in signal.criteria}


def file_text(slug: str) -> str:
    return (KIT.parent / "agents" / f"{slug}.md").read_text(encoding="utf-8")


def prompt_of(code: str, line: str = "SWO") -> str:
    ctx = SignalContext(source=code, desk_id=1, line=line, symbol="NVDA", as_of=AS_OF, horizon="weeks", snapshot_hash="h", facts={"lean": 0})
    return agent(code, Chat(reply("neutral"))).system_prompt(ctx)


def check_agent_file(code: str, *, must_say: tuple[str, ...]) -> None:
    """The file of a profile: English, no origin, in the format of the seat files, declares its lines and skills, injected in its prompt
    and only on desks of its line, and it names its method as inspired and never as a decision of a person."""
    seat = SEATS[code]
    path = KIT.parent / "agents" / f"{seat.slug}.md"
    text = path.read_text(encoding="utf-8")
    meta, body = split_frontmatter(text, str(path))
    assert meta["name"] == meta["slug"] == seat.slug and meta["display_name"] == seat.name and meta["role"] == "specialist"
    assert set(meta["lines"]) == set(seat.lines) and "SWO" in meta["lines"] and meta["skills"] == []
    assert repo_rules.check(ROOT, [path.relative_to(ROOT).as_posix()]) == []
    assert chr(0x2014) not in text and not re.search(r"[^\x00-\x7f]", text)
    assert "in the spirit of the public method of an investor" in body or "in the spirit of the public method of a" in body
    assert not re.search(r"\bdecides?\s+(as|like)\b", text, re.I) and contains_execution_terms(body) is None
    low = body.lower()
    for phrase in must_say:
        assert phrase.lower() in low, phrase
    prompt = prompt_of(code)
    assert body.strip()[:80] in prompt and "Line SWO" in prompt and "## Skill:" not in prompt
    assert f"never goes above {min(inv.PROFILES[code].confidence_cap, 100)}" in prompt
    assert "## Confidence scale" in prompt and "Answer with one JSON object" in prompt
    with pytest.raises(kit.AnalystKitError):
        prompt_of(code, "DT")
    for other in ("DT", "RF"):
        assert code not in {s.code for s in seats_for(other, None)}


# ---- A16 Owen -----------------------------------------------------------------------------------------------------------------

def test_AH_05_owen_file_is_english_inspired_and_filters_the_company_on_a_swo_desk():
    check_agent_file("A16", must_say=("company filter", "never the option", "cannot judge a competitive moat", "management"))


def test_INV_001_owen_reads_the_criteria_the_module_computed_and_forms_a_view():
    ctx = context("A16", price=CHEAP)
    assert ctx.facts["lean"] == 1 and ctx.facts["reason_code"] == "quality_with_margin_of_safety" and ctx.confidence_cap == 89
    direct = registry.call("fundamentals.quality_owner", statements=statements(price=CHEAP), as_of=CUT)
    assert [c.name for c in ctx.criteria] == [c["name"] for c in direct.value["criteria"]]
    assert all(c.value == d["value"] for c, d in zip(ctx.criteria, direct.value["criteria"]))
    chat = LeanChat(85)
    signal = agent("A16", chat).signal(ctx)
    assert (signal.source, signal.value, signal.confidence, signal.abstained) == ("A16", 0.85, 85, False)
    assert signal.criteria == ctx.criteria and signal.snapshot_hash == ctx.snapshot_hash
    assert {f.name for f in fields(Signal)} >= {"source", "value", "confidence", "criteria", "data_missing", "reason"}
    assert not [f.name for f in fields(Signal) if re.search(r"size|stop|target|entry|quantity", f.name)]
    capped = agent("A16", LeanChat(97)).signal(ctx)                    # the cap of the series depth (89) wins over the model
    assert (capped.confidence, capped.value) == (89, 0.89)
    assert "INV-001" in registry.describe("fundamentals.quality_owner").piece_ids
    assert "fundamentals.quality_owner" in [s.name for s in registry.list("fundamentals")]


def test_INV_001_owen_abstains_without_the_required_inputs_and_never_calls_the_model():
    for company, gap in (("CHARLIE", "periods_at_least_4_have_2"), ("BRAVO", "net_income_negative_in_most_periods")):
        chat = Chat(reply("bullish"))
        signal = agent("A16", chat).signal(context("A16", company=company))
        assert signal.abstained and signal.value is None and gap in signal.data_missing and chat.calls == []
    stale = inv.CompanyData(statements(), regime="neutral")
    old = datetime(2025, 3, 1, tzinfo=timezone.utc)
    signal = agent("A16", Chat(reply("bullish"))).signal(inv.build_context(inv.PROFILES["A16"], desk_id=1, line="SWO", symbol="X", as_of=old, data=stale))
    assert signal.abstained and any("filing_newer_than" in m for m in signal.data_missing)


def test_INV_001_a_failed_call_or_an_answer_nobody_can_read_is_an_abstention_not_a_neutral_view():
    ctx = context("A16", price=CHEAP)

    class Broken:
        def chat(self, *a, **k):
            raise RuntimeError("down")

    assert agent("A16", Broken()).signal(ctx).data_missing == ("model_call",)
    junk = agent("A16", Chat(ModelReplyText("not json at all")))
    signal = junk.signal(ctx)
    assert signal.abstained and signal.data_missing == ("model_answer",) and signal.value is None


def ModelReplyText(text):
    from hokma.desks.core.graph import ModelReply

    return ModelReply(text)


def test_INV_001_a_model_that_differs_from_the_criteria_must_say_so_and_the_signal_records_it():
    ctx = context("A16")                                           # lean -1: the price demands perfection
    assert ctx.facts["lean"] == -1
    silent = agent("A16", Chat(reply("neutral", 50)))
    signal = silent.signal(ctx)
    assert signal.abstained and len(silent._model.calls) == 2 and "disagrees_with_criteria" in silent._model.calls[1]["messages"][-1].content
    flagged = agent("A16", Chat(reply("neutral", 50, disagrees=True))).signal(ctx)
    assert not flagged.abstained and flagged.value == 0.0 and by_name(flagged)[inv.DISAGREES].value == 1.0


def test_INV_001_every_number_of_the_reason_comes_from_the_criteria_a_foreign_number_is_asked_again_once():
    ctx = context("A16", price=CHEAP)
    roe = by_name(Signal(source="A16", desk_id=1, line="SWO", symbol="X", as_of=AS_OF, horizon="weeks", value=0.5, confidence=50, abstained=False,
                         criteria=ctx.criteria, data_missing=(), reason="One reason. Two reasons.", snapshot_hash="h"))["roe_average"].value
    foreign = reply("bullish", 70, "Return on equity averaged 99.9 percent. The margin of safety is wide.")
    good = reply("bullish", 70, f"Return on equity averaged {roe:.4f} over the series. The margin of safety is wide.")
    chat = Chat(foreign, good)
    signal = agent("A16", chat).signal(ctx)
    assert not signal.abstained and len(chat.calls) == 2 and "99.9" in chat.calls[1]["messages"][-1].content
    stubborn = Chat(foreign)
    assert agent("A16", stubborn).signal(ctx).data_missing == ("model_answer",) and len(stubborn.calls) == 2


def test_INV_001_the_reason_cannot_speak_of_sizing_exits_or_timing():
    ctx = context("A16", price=CHEAP)
    loud = Chat(reply("bullish", 70, "The business is sound. Put the stop below the last low."))
    assert agent("A16", loud).signal(ctx).abstained and len(loud.calls) == 2


def test_SHF_004_owner_earnings_reach_owen_by_the_quant_tool_with_the_data_of_the_run_not_the_data_of_the_model():
    ctx = context("A16", price=CHEAP)
    asks = Chat(
        reply_call("fundamentals.owner_earnings", {"discount_rate": 0.12, "growth_rate": 0.02, "statements": {"net_income": [1e9]}, "as_of": "2031-01-01"}),
        reply("bullish", 70, "The business is sound. The margin of safety is wide."))
    agent("A16", asks).signal(ctx, data(price=CHEAP))
    tool_text = next(m.content for m in asks.calls[1]["messages"] if m.role == "tool")
    got = json.loads(tool_text)["data"]
    want = registry.call("fundamentals.owner_earnings", statements=statements(price=CHEAP), as_of=CUT, discount_rate=0.12, growth_rate=0.02)
    assert got["value"]["owner_earnings"] == want.value["owner_earnings"] and got["value"]["intrinsic_value"] == want.value["intrinsic_value"]
    assert got["params"]["safety_margin"] == 0.25 and got["params"]["discount_rate"] == 0.12 and "SHF-004" in got["piece_ids"]
    assert got["value"]["period"] == "2023-12-31"             # the statements and the date are the run's: nothing the model sent counted


def reply_call(name, params):
    from b7_support import call

    return call("quant_call", name=name, params=params)


def test_SHF_004_owen_gets_only_the_fundamentals_module_and_every_call_is_limited_by_the_registry():
    ctx = context("A16", price=CHEAP)
    blocked = Chat(reply_call("options.vol_richness", {}), reply("neutral", 50, "The call was refused. No view is added.", disagrees=True))
    agent("A16", blocked).signal(ctx, data(price=CHEAP))
    result = json.loads(next(m.content for m in blocked.calls[1]["messages"] if m.role == "tool"))
    assert result["ok"] is False and result["error"]["kind"] == "blocked"
    assert blocked.calls[0]["tools"] == ["quant_call"]
    huge = Chat(reply_call("fundamentals.owner_earnings", {"discount_rate": 10}), reply("neutral", 50, "The call was refused. No view is added.", disagrees=True))
    agent("A16", huge).signal(ctx, data(price=CHEAP))
    refused = json.loads(next(m.content for m in huge.calls[1]["messages"] if m.role == "tool"))
    assert refused["ok"] is False and refused["error"]["kind"] == "bad_input"


def test_SHF_006_roe_of_fifteen_percent_and_debt_of_half_apply_and_a_price_that_demands_perfection_lowers_the_view():
    ctx = context("A16", price=CHEAP)
    named = {c.name: c for c in ctx.criteria}
    assert named["roe_average"].threshold == 0.15 and named["roe_minimum"].threshold == 0.10 and named["debt_to_equity"].threshold == 0.5
    assert named["margin_of_safety"].threshold == 0.25
    assert [context("A16", price=p).facts["lean"] for p in (CHEAP, 30)] == [1, 0]
    assert context("A16").facts["lean"] == -1 and context("A16").facts["reason_code"] == "deterioration_or_price_demands_perfection"
    low = agent("A16", LeanChat(60)).signal(context("A16"))
    assert low.value == -0.6


# ---- A17 Nate -----------------------------------------------------------------------------------------------------------------

def test_AH_06_nate_file_asks_about_incentives_and_about_what_is_too_hard_and_defaults_to_neutral():
    check_agent_file("A17", must_say=("incentives", "too hard", "neutral by default", "veto"))


def test_INV_002_nate_is_neutral_by_default_and_cannot_be_bullish_unless_the_criteria_lean_bullish():
    ctx = context("A17")
    assert ctx.facts["lean"] == 0 and ctx.confidence_cap == 89
    neutral = agent("A17", LeanChat(60)).signal(ctx)
    assert neutral.value == 0.0 and not neutral.abstained
    eager = Chat(reply("bullish", 80))
    refused = agent("A17", eager).signal(ctx)
    assert refused.abstained and len(eager.calls) == 2 and refused.data_missing == ("model_answer",)
    rare = context("A17", price=CHEAP)
    assert rare.facts["lean"] == 1
    allowed = agent("A17", Chat(reply("bullish", 95))).signal(rare)
    assert (allowed.confidence, allowed.value) == (89, 0.89)               # bullish is rare and capped under the top of the scale
    bravo = context("A17", company="BRAVO")                              # a failure cause is itself a finding: no abstention for a missing change
    assert bravo.facts["lean"] == -1
    assert agent("A17", Chat(reply("bearish", 70))).signal(bravo).value == -0.7
    assert agent("A17", Chat(reply("bullish"))).signal(context("A17", company="CHARLIE")).abstained


def test_INV_002_what_is_too_hard_to_judge_is_neutral_or_abstention_and_failure_causes_are_criteria():
    ctx = context("A17")
    names = {c.name for c in ctx.criteria}
    assert {"margin_change", "debt_to_equity_change", "roe_change", "earnings_quality_fcf_to_net_income", "loss_periods"} <= names
    hard = manual("A17", 0, too_hard=True)
    strong = Chat(reply("bearish", 70))
    assert agent("A17", strong).signal(hard).abstained and len(strong.calls) == 2
    assert agent("A17", Chat(reply("neutral", 40))).signal(hard).value == 0.0
    assert agent("A17", Chat(reply("abstain", 0, "The series is too inconsistent to judge. It is in the pile of what is too hard."))).signal(hard).data_missing == ("model_abstained",)


def test_INV_002_the_veto_counts_only_when_the_mandate_turns_it_on():
    nate = Signal(source="A17", desk_id=1, line="SWO", symbol="X", as_of=AS_OF, horizon="weeks", value=-0.7, confidence=70, abstained=False, criteria=(),
                  data_missing=(), reason="Failure signals are strong. The cash conversion is weak.", snapshot_hash="a")
    owen = Signal(source="A16", desk_id=1, line="SWO", symbol="X", as_of=AS_OF, horizon="weeks", value=0.9, confidence=90, abstained=False, criteria=(),
                  data_missing=(), reason="Quality is sound. The price is fair.", snapshot_hash="b")
    off = inv.company_filter([nate, owen])
    assert off.passes and off.vetoed_by is None
    on = inv.company_filter([nate, owen], veto=True)
    assert not on.passes and on.reason_code == "rejected_by_veto" and on.vetoed_by == "A17"
    base = {"name": "m", "line": "SWO", "variant": "traditional", "strategies": [
        {"name": "s", "horizon": "weeks", "models": [{"name": "A16"}, {"name": "A17", "params": {"veto": True}}]}]}
    assert inv.veto_enabled(parse_mandate(base)) is True
    base["strategies"][0]["models"][1] = {"name": "A17"}
    assert inv.veto_enabled(parse_mandate(base)) is False
    assert inv.module_params(inv.PROFILES["A17"], {"veto": True}) == {"veto_enabled": True}
    assert context("A17", params={"veto": True}).facts["veto_enabled"] is True and context("A17").facts["veto_enabled"] is False
    with pytest.raises(inv.InvestorError):
        inv.module_params(inv.PROFILES["A17"], {"statements": "mine"})


# ---- A18 Gabe -----------------------------------------------------------------------------------------------------------------

def test_AH_07_gabe_file_applies_the_margin_of_safety_as_a_prior_filter_of_the_company():
    check_agent_file("A18", must_say=("prior filter", "margin of safety", "does not invent", "outside the balance sheet"))


def test_INV_003_gabe_has_pe_pb_and_their_product_computed_by_code_and_the_classic_thresholds_are_cited():
    ctx = context("A18")
    named = {c.name: c for c in ctx.criteria}
    assert named["pe_times_pb"].value == pytest.approx(named["price_to_earnings"].value * named["price_to_book"].value, abs=1e-8)
    assert (named["price_to_earnings"].threshold, named["price_to_book"].threshold, named["pe_times_pb"].threshold) == (15.0, 1.5, 22.5)
    assert ctx.facts["classic_thresholds"]["pe_x_pb_max"] == 22.5 and ctx.facts["classic_thresholds"]["current_ratio_strict"] == 2.0
    assert "INV-003" in registry.describe("fundamentals.safety_margin").piece_ids
    sound = context("A18", price=6)
    assert sound.facts["lean"] in (0, 1)


def test_INV_003_earnings_that_are_not_positive_disqualify_with_a_medium_confidence_and_it_is_not_an_abstention():
    ctx = context("A18", company="BRAVO")
    assert ctx.facts["lean"] == -1 and ctx.facts["reason_code"] == "eps_not_positive_disqualifies"
    signal = agent("A18", LeanChat(50)).signal(ctx)
    assert not signal.abstained and signal.value == -0.5
    assert agent("A18", Chat(reply("bullish"))).signal(context("A18", company="CHARLIE")).abstained


# ---- A19 Paul -----------------------------------------------------------------------------------------------------------------

def test_AH_08_paul_file_categorizes_the_company_before_applying_the_growth_price_ratio():
    check_agent_file("A19", must_say=("categorize first", "price to earnings over growth", "abstains"))


def test_INV_004_paul_categorizes_by_the_growth_of_eps_and_abstains_without_the_peg_and_without_calling_the_model():
    ctx = context("A19", price=CHEAP)
    assert ctx.facts["category"] and {"eps_growth_cagr", "peg"} <= {c.name for c in ctx.criteria}
    chat = Chat(reply("bullish"))
    none = agent("A19", chat).signal(context("A19", company="BRAVO"))
    assert none.abstained and "peg" in none.data_missing and chat.calls == []


def test_INV_004_insiders_and_institutions_count_only_when_the_data_arrives():
    without = agent("A19", LeanChat(60)).signal(context("A19"))
    assert {"insider_transactions", "institutional_ownership"} <= set(without.data_missing)
    arrived = inv.CompanyData(statements(), insiders=[{"net": 1}], institutional=[{"holders": 3}])
    ctx = inv.build_context(inv.PROFILES["A19"], desk_id=1, line="SWO", symbol="NVDA", as_of=AS_OF, data=arrived)
    assert ctx.facts["declared_missing"] == [] and ctx.facts["optional_data_arrived"] == ["insider_transactions", "institutional_ownership"]


def test_SHF_009_peg_under_one_adds_over_two_subtracts_and_the_edges_add_nothing():
    for peg, points in ((0.99, 1), (1.0, 0), (1.5, 0), (2.0, 0), (2.01, -1)):
        assert crit.peg_points(peg) == points
    ctx = context("A19", price=CHEAP)
    assert ctx.facts["peg_points"] == crit.peg_points(ctx.facts["peg"]) and ctx.facts["peg"] < 1.0 and ctx.facts["lean"] == 1
    peg = next(c for c in ctx.criteria if c.name == "peg_not_excessive")
    assert peg.threshold == 2.0


# ---- A20 Ray ------------------------------------------------------------------------------------------------------------------

def test_AH_09_ray_file_declares_what_he_does_not_have():
    check_agent_file("A20", must_say=("monetary policy", "bonds", "currencies", "wait"))
    assert "monetary policy" in prompt_of("A20").lower() or "monetary policy" in inv.PROFILES["A20"].lacks
    assert "monetary policy, liquidity, bonds, currencies" in prompt_of("A20")          # the scope clause of the prompt says it too
    assert "A20" in {s.code for s in seats_for("SWC", None)} and "A20" in {s.code for s in seats_for("SWO", None)}


def test_INV_005_ray_waits_by_default_and_cannot_be_bullish_unless_the_criteria_lean_bullish():
    ctx = context("A20")
    assert ctx.facts["lean"] == 0 and ctx.facts["default"] == "wait"
    wait = agent("A20", LeanChat(70)).signal(ctx)
    assert wait.value == 0.0 and not wait.abstained                            # waiting is a view of value zero with its reason
    eager = Chat(reply("bullish", 80))
    assert agent("A20", eager).signal(ctx).abstained and len(eager.calls) == 2
    ok = agent("A20", Chat(reply("bullish", 80))).signal(manual("A20", 1))
    assert ok.value == 0.8


def test_INV_005_ray_abstains_without_the_regime_or_the_candles_and_never_reads_the_candle_of_the_cut_day():
    for kw, gap in (({"regime": None}, "regime"), ({"with_candles": False}, "candles")):
        chat = Chat(reply("neutral"))
        signal = agent("A20", chat).signal(context("A20", **kw))
        assert signal.abstained and gap in signal.data_missing[0] and chat.calls == []
    bars = candles()
    day = pd.Timestamp(AS_OF.date())
    assert bars.index.max() > day
    cut = inv._closed_days_only(bars, AS_OF)
    assert cut.index.max() < day and len(cut) == int((bars.index < day).sum())
    before = context("A20")
    extra = bars.copy()
    extra.loc[day] = [100.0, 101.0, 99.0, 100.5, 1_000_000.0]            # a bar of the cut-off day that may still be open
    after = inv.build_context(inv.PROFILES["A20"], desk_id=1, line="SWO", symbol="NVDA", as_of=AS_OF,
                              data=inv.CompanyData(statements(), candles=extra.sort_index(), regime="neutral", catalyst=FUTURE_CATALYST))
    assert before.snapshot_hash == after.snapshot_hash and before.criteria == after.criteria
    hostile = context("A20", regime="hostile")
    assert hostile.facts["regime"] == "hostile"


# ---- A21 Noah -----------------------------------------------------------------------------------------------------------------

def test_INV_006_noah_file_requires_a_dated_catalyst_to_be_bullish():
    check_agent_file("A21", must_say=("dated catalyst", "cannot be positive", "thirty percent", "invents"))


def test_INV_006_gap_above_thirty_percent_with_a_dated_catalyst_allows_a_bullish_view_without_it_the_code_refuses():
    good = context("A21", price=VERY_CHEAP)
    assert good.facts["value_gap"] > 0.30 and good.facts["catalyst_valid"] is True and good.facts["lean"] == 1
    assert agent("A21", Chat(reply("bullish", 80))).signal(good).value == 0.8
    stale = context("A21", price=VERY_CHEAP, catalyst=PAST_CATALYST)
    assert stale.facts["catalyst_valid"] is False and stale.facts["lean"] == 0
    asks = Chat(reply("bullish", 80))
    assert agent("A21", asks).signal(stale).abstained and len(asks.calls) == 2
    forced = manual("A21", 1, catalyst_valid=False)                       # whatever the lean says, no valid catalyst means no positive view
    again = Chat(reply("bullish", 80))
    assert agent("A21", again).signal(forced).abstained and any("dated" in m.content or "catalyst" in m.content for m in again.calls[1]["messages"][-1:])
    bubble = context("A21", catalyst=None)
    assert bubble.facts["lean"] == -1
    seen = agent("A21", LeanChat(70)).signal(bubble)
    assert seen.value == -0.7 and "catalyst" in seen.data_missing           # the negative side needs no catalyst and the gap is declared


def test_SHF_008_the_catalyst_is_a_field_with_a_date_and_a_past_or_missing_date_voids_the_positive_side():
    cheap = dict(price=VERY_CHEAP)
    assert context("A21", catalyst=None, **cheap).facts["lean"] == 0
    assert context("A21", catalyst=PAST_CATALYST, **cheap).facts["catalyst_valid"] is False
    undated = agent("A21", Chat(reply("neutral"))).signal(context("A21", catalyst={"description": "launch"}, **cheap))
    assert undated.abstained and undated.data_missing == ("quant_call_failed",)
    ahead = {c.name: c for c in context("A21", **cheap).criteria}["catalyst_days_ahead"]
    assert ahead.value == 62.0 and ahead.passed is True


# ---- A22 Ruth -----------------------------------------------------------------------------------------------------------------

def test_INV_007_ruth_file_takes_the_theme_from_a_table_declares_the_tam_unknown_and_caps_confidence_at_69():
    check_agent_file("A22", must_say=("curated table", "unknown", "sixty nine", "size of the market"))


def test_INV_007_ruth_has_a_theme_from_the_table_unknown_tam_and_a_confidence_that_never_passes_69():
    ctx = context("A22", symbol="NVDA")
    assert ctx.facts["theme"] == "artificial_intelligence" and ctx.confidence_cap == 69
    signal = agent("A22", LeanChat(95)).signal(ctx)
    assert signal.confidence == 69 and signal.value == 0.0
    assert {"tam", "first_mover", "team_vision"} <= set(signal.data_missing)
    bearish = agent("A22", Chat(reply("bearish", 98, "Revenue slows. Cash runway is short.", disagrees=True))).signal(ctx)
    assert (bearish.confidence, bearish.value) == (69, -0.69)
    grown = agent("A22", Chat(reply("bullish", 99))).signal(manual("A22", 1, cap=69))
    assert (grown.confidence, grown.value) == (69, 0.69)
    off_table = agent("A22", Chat(reply("neutral"))).signal(context("A22", symbol="ALFA"))
    assert off_table.abstained and "theme_not_in_the_curated_table" in off_table.data_missing


def test_SHF_007_what_is_not_in_the_data_is_never_invented_rd_missing_abstains_and_the_unknowns_are_declared():
    frame = statements().drop(columns=["rd_expenses"])
    ctx = inv.build_context(inv.PROFILES["A22"], desk_id=1, line="SWO", symbol="NVDA", as_of=AS_OF, data=inv.CompanyData(frame))
    chat = Chat(reply("bullish"))
    signal = agent("A22", chat).signal(ctx)
    assert signal.abstained and "rd_expenses" in signal.data_missing and chat.calls == []
    assert "unknown" in prompt_of("A22")
    full = context("A22")
    assert full.facts["tam"] == "unknown" and full.facts["first_mover"] == "unknown" and full.facts["team_vision"] == "unknown"
    assert context("A22", params={"rd_intensity_min": 0.05, "runway_months_min": 12, "dilution_max": 0.05, "revenue_accel_min": 0.0}).facts["reason_code"] \
        != "thresholds_not_calibrated"


# ---- the persistence of the views and the filter (AH-16) ------------------------------------------------------------------------

@pytest.fixture
def world(db, tmp_path):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Test Fund")
    return db, build_kit(tmp_path / "kit")


def test_AH_16_signals_are_stored_in_desk_signals_once_and_only_by_an_active_seat_of_the_desk(world):
    db, kit_root = world
    desk = create_swo_desk("Options SWO", 50_000, kit_root=kit_root, db=db)["id"]
    sig = agent("A16", LeanChat(80)).signal(inv.build_context(inv.PROFILES["A16"], desk_id=desk, line="SWO", symbol="NVDA", as_of=AS_OF,
                                                              data=data(price=CHEAP)))
    first = inv.persist_signal(sig, db=db)
    assert inv.persist_signal(sig, db=db) == first
    with db.read() as u:
        rows = u.session.query("SELECT seat_code, symbol, value, confidence, abstained, snapshot_hash, signal_json FROM desk_signals WHERE desk_id = :d", {"d": desk})
    assert len(rows) == 1 and rows[0]["seat_code"] == "A16" and rows[0]["value"] == 0.8 and rows[0]["abstained"] == 0
    assert Signal.from_json(rows[0]["signal_json"]) == sig
    nobody = agent("A16", Chat(reply("neutral"))).signal(context("A16", company="CHARLIE"))
    stored = inv.persist_signal(Signal(**{**nobody.__dict__, "desk_id": desk}), db=db)
    with db.read() as u:
        gone = u.session.one("SELECT value, abstained FROM desk_signals WHERE id = :i", {"i": stored})
    assert gone["value"] is None and gone["abstained"] == 1                        # an abstention is stored as such
    with pytest.raises(inv.InvestorError):                                         # A23 is not a seat of a traditional desk
        inv.persist_signal(Signal.abstain("A23", desk, "SWO", "NVDA", AS_OF, "days", ("x",), "No view is formed. Nothing arrived.", "h"), db=db)
    with db.write() as u:
        u.session.execute("UPDATE desk_seats SET active = 0 WHERE desk_id = :d AND seat_code = 'A16'", {"d": desk})
    other = Signal(**{**sig.__dict__, "snapshot_hash": "another"})
    with pytest.raises(inv.InvestorError):
        inv.persist_signal(other, db=db)
    with pytest.raises(inv.InvestorError):
        inv.persist_signal(Signal.abstain("A01", desk, "SWO", "NVDA", AS_OF, "weeks", ("x",), "No view is formed. Nothing arrived.", "h"), db=db)


def _history(db, desk, rows):
    """rows: (seat, day offset, value). One stored view per row, as if earlier cycles had run."""
    for seat, offset, value in rows:
        when = AS_OF - timedelta(days=offset)
        sig = Signal(source=seat, desk_id=desk, line="SWO", symbol="NVDA", as_of=when, horizon="weeks", value=value, confidence=70, abstained=False,
                     criteria=(), data_missing=(), reason="Past view. Stored for the history.", snapshot_hash=f"{seat}{offset}")
        inv.persist_signal(sig, db=db)


def today(seat, value, conf=70):
    return Signal(source=seat, desk_id=1, line="SWO", symbol="NVDA", as_of=AS_OF, horizon="weeks", value=value, confidence=conf, abstained=False,
                  criteria=(), data_missing=(), reason="Today view. Read from the criteria.", snapshot_hash=f"t{seat}")


def test_AH_16_profiles_that_correlate_above_point_eight_in_desk_signals_count_as_one_and_the_history_is_only_the_past(world):
    db, kit_root = world
    desk = create_swo_desk("Options SWO", 50_000, kit_root=kit_root, db=db)["id"]
    pattern = [0.7, -0.5, 0.3, 0.9, -0.2, 0.6, -0.8, 0.4, 0.1, -0.6, 0.5, 0.2]
    rows = [("A16", i + 1, v) for i, v in enumerate(pattern)] + [("A17", i + 1, v * 0.9) for i, v in enumerate(pattern)] \
        + [("A18", i + 1, v) for i, v in enumerate([0.3, 0.8, -0.4, -0.1, 0.6, -0.7, 0.2, 0.5, -0.9, 0.4, -0.3, 0.1])]
    _history(db, desk, rows)
    _history(db, desk, [("A16", -3, 0.99)])                                       # a view of the future: it can never enter
    history = inv.load_signal_history(desk, AS_OF, db=db)
    assert len(history) == 36 and set(history["source"]) == {"A16", "A17", "A18"}
    assert pd.to_datetime(history["as_of"]).max() < pd.Timestamp(AS_OF.replace(tzinfo=None))
    result = inv.company_filter([today("A16", 0.8), today("A17", 0.6), today("A18", -0.2)], history=history)
    assert result.merged == (("A16", "A17"),) and any("merged profiles" in w for w in result.warnings)
    assert result.n_used == 3 and result.passes and result.reason_code == "passed"
    plain = inv.company_filter([today("A16", 0.8), today("A17", 0.6), today("A18", -0.2)])
    assert plain.merged == () and plain.blended != result.blended                    # without the history the same view counts twice


def test_AH_16_abstentions_are_left_out_no_view_rejects_and_a_net_bearish_company_does_not_reach_the_debate():
    abstained = Signal.abstain("A19", 1, "SWO", "NVDA", AS_OF, "weeks", ("peg",), "No view is formed. The ratio does not exist.", "h")
    mixed = inv.company_filter([today("A16", 0.8), abstained])
    assert mixed.abstained == ("A19",) and mixed.n_used == 1 and mixed.blended == pytest.approx(0.8)
    none = inv.company_filter([abstained])
    assert (none.passes, none.reason_code) == (False, "no_view") and "No profile formed a view" in none.summary()
    assert inv.company_filter([]).reason_code == "no_view"
    bearish = inv.company_filter([today("A16", -0.7), today("A18", -0.4)])
    assert (bearish.passes, bearish.reason_code) == (False, "rejected_net_bearish") and bearish.blended < 0
    assert "does not pass" in bearish.summary()
    weighted = inv.company_filter([today("A16", 0.9), today("A18", -0.9)], weights={"A16": 3.0, "A18": 1.0})
    assert weighted.blended > 0 and weighted.passes
    base = {"name": "m", "line": "SWO", "variant": "traditional", "strategies": [
        {"name": "s", "horizon": "weeks", "models": [{"name": "A16", "weight": 3}, {"name": "A18"}]}]}
    assert inv.mandate_weights(parse_mandate(base)) == {"A16": 3.0, "A18": 1.0}


def test_AH_16_one_filter_pass_of_the_desk_runs_every_investor_seat_stores_each_view_and_mixes_them(world):
    db, kit_root = world
    desk = create_swo_desk("Options SWO", 50_000, kit_root=kit_root, db=db)["id"]
    assert inv.desk_investor_seats(desk, db=db) == inv.INVESTOR_CODES
    models = {code: LeanChat(70) for code in inv.INVESTOR_CODES}
    models.pop("A19")                                                                # a seat with no model abstains
    result = inv.evaluate_company(desk, symbol="NVDA", as_of=AS_OF, line="SWO", models=models, data=data(price=CHEAP), db=db, kit_root=kit_root)
    stored = {s.source: s for s in inv.load_signals(desk, symbol="NVDA", db=db)}
    assert set(stored) == set(inv.INVESTOR_CODES)
    assert stored["A19"].abstained and stored["A19"].data_missing == ("model",)
    assert stored["A16"].value == 0.7 and not stored["A22"].abstained and stored["A22"].value == 0.0
    assert result.signals and result.n_used + len(result.abstained) == 7 and result.passes
    again = inv.evaluate_company(desk, symbol="NVDA", as_of=AS_OF, line="SWO", models=models, data=data(price=CHEAP), db=db, kit_root=kit_root)
    assert len(inv.load_signals(desk, symbol="NVDA", db=db)) == 7 and again.blended == result.blended        # the same views are stored once


def test_AH_16_a_signal_can_be_tied_to_a_run_of_its_own_desk_and_to_no_other(world):
    db, kit_root = world
    first = create_swo_desk("Options SWO", 50_000, kit_root=kit_root, db=db)["id"]
    second = create_swo_desk("Second SWO", 50_000, kit_root=kit_root, db=db)["id"]
    with db.write() as u:
        mine = u.session.insert("run_records", {"desk_id": first, "kind": "cycle", "signature": "s", "status": "running", "record_json": "{}"})
        theirs = u.session.insert("run_records", {"desk_id": second, "kind": "cycle", "signature": "s", "status": "running", "record_json": "{}"})
    sig = Signal(source="A16", desk_id=first, line="SWO", symbol="NVDA", as_of=AS_OF, horizon="weeks", value=0.4, confidence=40, abstained=False,
                 criteria=(), data_missing=(), reason="A view of the desk. Read from the criteria.", snapshot_hash="run")
    stored = inv.persist_signal(sig, run_id=mine, db=db)
    with db.read() as u:
        assert u.session.one("SELECT run_id FROM desk_signals WHERE id = :i", {"i": stored})["run_id"] == mine
    with pytest.raises(inv.InvestorError):
        inv.persist_signal(Signal(**{**sig.__dict__, "snapshot_hash": "other"}), run_id=theirs, db=db)
