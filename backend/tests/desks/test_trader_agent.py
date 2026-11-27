"""Isaac, the trader (SPEC-019 A11): build_orders (AH-21), the live thesis (AH-55), the prompt by pieces and the plan (TA-09)."""
import json
import shutil
from decimal import Decimal as D
from pathlib import Path

import pytest

from hokma.desks.agents import trader
from hokma.desks.agents.trader import (BrokerCosts, BuildError, ExitRules, Quote, ThesisDecoder, TraderAnswerError, TraderError, build_orders,
                                       parse_answer, plan_trade, run_trader, thesis_stream)
from hokma.desks.plantel import SEATS, TraderPlan
from hokma.exec.proposal import OrderProposal
from trader_support import (AS_OF, C200, C210, C220, P170, P180, P190, STOCK, Reply, ScriptedModel, StreamingModel, answer_json, book,
                            context, inputs, quotes, sheets)

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "kit" / "template" / "desk"
COSTS = BrokerCosts("per_share", D("0.65"), D(0), D(2))


# ---- AH-21 build_orders --------------------------------------------------------------------------------------------

def test_AH_21_the_difference_between_target_and_current_book_is_the_legs_at_the_real_entry_price():
    built = build_orders({C200: D(5), C210: D(-5)}, {C200: D(2), C210: D(-2)}, quotes=quotes(), sheets=sheets(), costs=COSTS)
    assert [(l.contract_id, l.side, l.effect, l.qty) for l in built.legs] == [(C200, "buy", "open", D(3)), (C210, "sell", "open", D(3))]
    buy, sell = built.detail
    assert (buy.reference, sell.reference) == (D("5.20"), D("1.80"))                           # the ask to buy and the bid to sell
    assert (buy.fill, sell.fill) == (D("5.2010"), D("1.7996"))                                 # 2 bps against the trader
    assert (buy.commission, sell.commission) == (D("1.95"), D("1.95"))                         # 0.65 per contract
    assert (buy.slippage_usd, sell.slippage_usd) == (D("0.3000"), D("0.1200"))
    assert built.cost_usd == D("1.95") * 2 + D("0.42")
    assert built.option_net_reference_usd == (D("5.20") - D("1.80")) * 3 * 100
    assert built.option_net_fill_usd == (D("5.2010") - D("1.7996")) * 300
    assert built.outlay_usd == built.option_net_fill_usd + D("3.90") and not built.deferred


def test_AH_21_a_leg_that_reduces_a_position_closes_it_and_a_flip_closes_first():
    built = build_orders({C200: D(0)}, {C200: D(4)}, quotes=quotes(), sheets=sheets(), costs=COSTS)
    assert [(l.side, l.effect, l.qty) for l in built.legs] == [("sell", "close", D(4))]
    assert built.detail[0].reference == D("5.10")                                                # a sell enters at the bid
    flip = build_orders({C200: D(-1)}, {C200: D(3)}, quotes=quotes(), sheets=sheets(), costs=COSTS)
    assert [(l.side, l.effect, l.qty) for l in flip.legs] == [("sell", "close", D(3))] and flip.deferred == {C200: D(-1)}
    assert build_orders({C200: D(2)}, {C200: D(2)}, quotes=quotes(), sheets=sheets(), costs=COSTS).legs == ()


def test_AH_21_shares_are_part_of_the_outlay_but_not_of_the_option_premium():
    built = build_orders({STOCK: D(100), C210: D(-1)}, {}, quotes=quotes(), sheets=sheets(), costs=COSTS)
    assert built.option_net_reference_usd == D("-180.0")
    assert built.outlay_usd == D("200.0400") * 100 - D("1.7996") * 100 + built.detail[0].commission + built.detail[1].commission


@pytest.mark.parametrize("target, current, code", [
    ({C200: D("1.5")}, {}, "fractional"),
    ({"OPT:AAPL:2026-12-18:C:230": D(1)}, {}, "no_quote"),
])
def test_AH_21_what_cannot_be_priced_or_is_not_whole_is_refused(target, current, code):
    with pytest.raises(BuildError) as exc:
        build_orders(target, current, quotes=quotes(), sheets=sheets(), costs=COSTS)
    assert exc.value.code == code
    no_ask = {**quotes(), C200: Quote(D("5.1"), D("5.2"))}
    no_bid = {**quotes(), C210: Quote(D(0), D("0.05"))}
    with pytest.raises(BuildError):
        build_orders({C210: D(-1)}, {}, quotes=no_bid, sheets=sheets(), costs=COSTS)
    assert build_orders({C200: D(1)}, {}, quotes=no_ask, sheets=sheets(), costs=COSTS).legs


def test_AH_21_the_costs_come_from_the_broker_of_the_desk_and_default_to_the_simulated_ones():
    class Broker:
        commission = {"type": "per_share", "value": 0.65, "min": 1.0}
        slippage_bps = 5

    costs = BrokerCosts.from_broker(Broker())
    assert (costs.commission_type, costs.commission_value, costs.commission_min, costs.slippage_bps) == ("per_share", D("0.65"), D(1), D(5))
    assert costs.commission(D(520), D(1)) == D("1.00") and costs.commission(D(520), D(10)) == D("6.50") and costs.commission(D(0), D(0)) == 0
    assert costs.fill("buy", D("10")) == D("10.0050") and costs.fill("sell", D("10")) == D("9.9950")
    default = BrokerCosts.from_broker(object())
    assert (default.commission_type, default.commission_value, default.slippage_bps) == ("bps", D(1), D(2))
    assert default.commission(D(10_000), D(5)) == D("1.00")


# ---- AH-55 the thesis while it is written ----------------------------------------------------------------------------

THESIS = 'The verdict is a buy; the "200" calls are liquid.\nUnicode: caf\u00e9 \U0001F600 and <b>bold</b> & more.'


def _chunks(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)]


@pytest.mark.parametrize("size", [1, 2, 3, 5, 11, 1000])
def test_AH_55_the_thesis_arrives_in_parts_whatever_the_cut_of_the_chunks(size):
    raw = json.dumps({"action": "propose", "reason": 'a "thesis" word inside', "thesis": THESIS, "structure": "long_call"})
    decoder = ThesisDecoder()
    got = "".join(decoder.feed(c) for c in _chunks(raw, size))
    assert got == THESIS and decoder.done
    ascii_raw = json.dumps({"thesis": THESIS}, ensure_ascii=True)
    second = ThesisDecoder()
    assert "\\u" in ascii_raw and "".join(second.feed(c) for c in _chunks(ascii_raw, size)) == THESIS


def test_AH_55_a_nested_key_named_thesis_is_not_the_thesis():
    raw = json.dumps({"legs": [{"thesis": "no"}], "reason": {"thesis": "no"}, "thesis": "yes"})
    decoder = ThesisDecoder()
    assert "".join(decoder.feed(c) for c in _chunks(raw, 3)) == "yes"


def test_AH_55_the_panel_receives_the_parts_while_the_model_still_writes_and_the_answer_is_whole():
    raw = answer_json(thesis=THESIS.replace("\n", " "))
    seen = []
    model_chunks = _chunks(raw, 9)
    out = thesis_stream(iter(model_chunks), line="SWO", sink=seen.append)
    assert out.copilot and out.text == raw and len(out.parts) > 1
    assert "".join(p.text for p in seen) == THESIS.replace("\n", " ") and [p.index for p in seen] == list(range(len(seen)))
    assert "<b>" not in "".join(p.html for p in seen) and "&lt;b&gt;" in "".join(p.html for p in seen)


def test_AH_55_the_flag_of_the_line_turns_the_copilot_off_in_dt_and_a_failing_panel_does_not_stop_the_answer():
    raw = answer_json()
    seen = []
    off = thesis_stream(_chunks(raw, 9), line="DT", sink=seen.append)
    assert not off.copilot and off.parts == () and seen == [] and off.text == raw
    assert not thesis_stream(_chunks(raw, 9), line="SWO", sink=seen.append, copilot=False).copilot and seen == []
    assert all(thesis_stream(_chunks(raw, 9), line=line, sink=seen.append).copilot for line in ("SWC", "SWO", "LP"))

    def boom(part):
        raise RuntimeError("panel closed")

    assert thesis_stream(_chunks(raw, 9), line="SWO", sink=boom).text == raw
    with pytest.raises(TraderError):
        thesis_stream([], line="XX")


def test_AH_55_run_trader_streams_the_thesis_to_the_panel_while_the_proposal_is_built():
    model, seen = StreamingModel(answer_json()), []
    result = run_trader(model, context(), sink=seen.append)
    assert result.status == "proposed" and model.calls[0][0] == "stream"
    assert "".join(p.text for p in seen) == json.loads(answer_json())["thesis"]
    plain = ScriptedModel(answer_json())
    assert run_trader(plain, context(), sink=seen.append).status == "proposed" and plain.calls[0][0] == "complete"
    dt_model, dt_seen = StreamingModel(answer_json()), []
    assert run_trader(dt_model, context(), sink=dt_seen.append, copilot=False).status == "proposed" and dt_seen == []


# ---- TA-09 the prompt by pieces ---------------------------------------------------------------------------------------

def test_TA_09_the_prompt_is_built_by_pieces_of_the_line_in_the_fixed_order():
    prompt = trader.build_system_prompt(desk_id=7, as_of=AS_OF, mandate_summary="Options swing, traditional. Risk: up to 1% per trade.",
                                        limits={"max_premium_usd": 2500, "funding": {"daily_loss_usd": 1000}})
    order = [prompt.index(f"## Piece: {key}\n") for key in ("sizing", "exits", "precheck", "funding-rules", "strategy-rules", "desk-limits", "desk-mandate", "premium-switch")]
    assert order == sorted(order)
    assert "## Role\nYou turn the verdict into an order proposal" in prompt and "## Line\nLine SWO" in prompt and "## Profile\n# Isaac" in prompt
    assert "- max_premium_usd: 2500" in prompt and "- funding.daily_loss_usd: 1000" in prompt and "up to 1% per trade" in prompt
    assert "The premium selling switch of this desk is closed. Offer only purchases and debit spreads." in prompt
    assert "## Answer format\n" in prompt and prompt.rstrip().endswith("the code computes them.")
    opened = trader.build_system_prompt(desk_id=7, as_of=AS_OF, premium_selling_open=True)
    assert "switch of this desk is open." in opened and "Offer only purchases" not in opened


def test_TA_09_the_prompt_follows_the_language_of_the_owner_and_each_desk_has_its_own_pieces():
    es = trader.build_system_prompt(desk_id=1, as_of=AS_OF, language="es", mandate_summary="Mandate of desk one")
    assert "Write your entire final answer in Spanish." in es
    other = trader.build_system_prompt(desk_id=2, as_of=AS_OF, language="es", mandate_summary="Mandate of desk two")
    assert "desk one" in es and "desk one" not in other and "desk two" in other


def test_TA_09_a_piece_of_another_line_never_enters_the_prompt_of_a_swo_desk(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(KIT, root / "kit" / "template" / "desk")
    shutil.copytree(KIT.parent / "agents", root / "kit" / "template" / "agents")
    path = root / "kit" / "template" / "desk" / "trader-pieces" / "sizing.md"
    path.write_text("---\nname: sizing\nlines: [DT]\nsummary: dt only\n---\nDT ONLY SIZING TEXT\n", encoding="utf-8")
    with pytest.raises(TraderError) as exc:
        trader.load_pieces("SWO", root=root)
    assert exc.value.code == "piece_missing"
    path.write_text("---\nname: sizing\nlines: [DT, SWO]\nsummary: both\n---\nSHARED SIZING TEXT\n", encoding="utf-8")
    for line in ("SWO",):
        assert "SHARED SIZING TEXT" in trader.build_system_prompt(desk_id=1, as_of=AS_OF, line=line, root=root)
    for other in ("DT", "SWC", "LP"):
        with pytest.raises(TraderError):                                                          # H5 writes the pieces of the other lines
            trader.load_pieces(other, root=root)
    with pytest.raises(TraderError):
        trader.load_pieces("XX")
    path.write_text("---\nname: other\nlines: [SWO]\n---\nbody\n", encoding="utf-8")
    with pytest.raises(TraderError) as bad:
        trader.load_pieces("SWO", root=root)
    assert bad.value.code == "bad_piece"


def test_TA_09_the_pieces_and_the_agent_file_are_english_without_origin_or_em_dash():
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    files = ["kit/template/agents/trader.md", *(p.relative_to(ROOT).as_posix() for p in sorted((KIT / "trader-pieces").glob("*.md"))),
             "backend/hokma/desks/agents/trader.py", "backend/hokma/desks/swo/schema.py", "backend/hokma/desks/trading/sizing.py",
             "backend/hokma/desks/trading/exits.py", "backend/hokma/desks/trading/budget.py"]
    assert len(files) == 11 and rules.check(ROOT, files) == []
    for rel in files:
        assert "\u2014" not in (ROOT / rel).read_text(encoding="utf-8"), rel
    investors = ("buffett", "munger", "graham", "lynch", "burry", "wood", "dalio", "druckenmiller", "soros")
    text = " ".join((ROOT / f).read_text(encoding="utf-8").lower() for f in files[:6])
    assert not any(name in text for name in investors)


def test_TA_09_the_agent_file_is_the_one_the_desk_creation_expects():
    from hokma.desks.core.skills import split_frontmatter
    from hokma.desks.swo import create

    meta, body = split_frontmatter((KIT.parent / "agents" / "trader.md").read_text(encoding="utf-8"))
    seat = SEATS["A11"]
    assert (meta["name"], meta["slug"], meta["display_name"]) == (seat.slug, seat.slug, seat.name) and "SWO" in meta["lines"]
    assert set(meta["lines"]) == set(seat.lines) and meta["skills"] == [] and "You never write a size" in body or "never writes a size" in body
    member = create._load_member(seat, KIT)
    assert member.skills == () and member.seat.code == "A11"
    assert trader.load_persona().startswith("# Isaac: the trader")


def test_TA_09_the_trader_prompt_may_hold_execution_words_and_no_other_role_prompt_does():
    from hokma.desks.core import prompts
    from hokma.desks.core.signal import contains_execution_terms

    assert "trader" in prompts.EXECUTION_ROLES
    assert contains_execution_terms(trader.build_system_prompt(desk_id=1, as_of=AS_OF))
    assert prompts.prompt_integrity_problems() == []


# ---- TA-09 the answer and the plan -----------------------------------------------------------------------------------

@pytest.mark.parametrize("extra, code", [
    ({"qty": 5}, "free_size"), ({"contracts": "about five"}, "free_size"), ({"stop_loss": "1.70"}, "free_size"), ({"size": "5 contracts"}, "free_size"),
    ({"take_profit": 6.8}, "free_size"), ({"limit_price": 3.4}, "free_size"), ({"risk_usd": 500}, "free_size"), ({"origin": "chat"}, "free_size"),
    ({"color": "red"}, "unknown_field"),
    ({"thesis": "Take about 5 contracts and ride it."}, "size_in_text"), ({"thesis": "Size: 10 on the spread."}, "size_in_text"),
    ({"thesis": ""}, "bad_answer"), ({"structure": "butterfly"}, "bad_answer"), ({"expiration": "soon"}, "bad_answer"), ({"legs": []}, "bad_answer"),
    ({"stop_basis": "feel"}, "bad_answer"),
])
def test_TA_09_a_size_written_as_free_text_or_a_field_outside_the_format_refuses_the_answer(extra, code):
    with pytest.raises(TraderAnswerError) as exc:
        parse_answer(answer_json(**extra))
    assert exc.value.code == code


def test_TA_09_a_size_inside_a_leg_and_a_text_that_is_not_json_are_refused():
    body = json.loads(answer_json())
    body["legs"][0]["qty"] = 3
    with pytest.raises(TraderAnswerError) as exc:
        parse_answer(json.dumps(body))
    assert exc.value.code == "free_size"
    for text in ("buy five contracts", "[1, 2]", ""):
        with pytest.raises(TraderAnswerError) as bad:
            parse_answer(text)
        assert bad.value.code == "bad_json"
    with pytest.raises(TraderAnswerError):
        parse_answer(json.dumps({"action": "stand_aside"}))
    assert parse_answer(json.dumps({"action": "stand_aside", "reason": "no evidence"})).reason == "no evidence"


def test_TA_09_the_size_the_stop_and_the_objective_come_from_the_modules_by_hand():
    result = plan_trade(parse_answer(answer_json()), context())
    p = result.proposal
    assert result.status == "proposed" and result.code == "ok" and result.contracts == 5
    # entry: ask 5.20 and bid 1.80 move 2 bps against the trader: 5.2010 - 1.7996 = 3.4014, rounded up to the tick: 3.41
    assert p.limit_price == D("3.41") and p.order_type == "limit" and p.net_premium == D("3.40")
    # stop: half of the premium, 1.705, rounded away from the entry: 1.70; risk 1.71. Objective: 2 R, entry + 3.41 = 6.82
    assert (p.stop_loss, p.take_profit) == (D("1.70"), D("6.82"))
    # risk of one contract: 1.71 * 100 + commission in and out (0.65 * 2 legs * 2) = 173.60; 1% of 100000 buys 5 (5.76)
    assert result.detail["risk_per_contract_usd"] == "173.60" and p.risk_usd == D("868.00")
    assert result.detail["budget"]["binding"] == "risk_per_trade" and result.detail["budget"]["limits"]["risk_per_trade"] == 5
    assert [(l.contract_id, l.side, l.effect, l.qty) for l in p.legs] == [(C200, "buy", "open", D(5)), (C210, "sell", "open", D(5))]
    assert (p.structure, p.expiration.isoformat(), p.underlying, p.max_loss_usd) == ("debit_call_spread", "2026-12-18", "AAPL", D(1700))
    assert (p.desk_id, p.line, p.origin, p.created_by, p.tif) == (7, "SWO", "desk", "trader", "day")
    assert result.warnings == ("uncalibrated defaults in the exits: stop_fraction",)
    plan = result.to_plan()
    assert isinstance(plan, TraderPlan) and plan.proposal["structure"] == "debit_call_spread" and plan.rationale.startswith("The verdict")


def test_TA_09_the_binding_limit_changes_the_size_and_the_rules_of_the_desk_enter_the_exits():
    capped = plan_trade(parse_answer(answer_json()), context(budget=inputs(max_premium_usd=D(1500))))      # 1500 over 520 of premium: 2
    assert capped.contracts == 2 and capped.detail["budget"]["binding"] == "max_premium"
    committee = plan_trade(parse_answer(answer_json()), context(budget=inputs(committee_max_contracts=3)))
    assert committee.contracts == 3 and committee.detail["budget"]["binding"] == "committee"
    tuned = plan_trade(parse_answer(answer_json()), context(rules=ExitRules(r_multiple=D(3), debit_stop_fraction=D("0.4"))))
    assert (tuned.proposal.stop_loss, tuned.proposal.take_profit) == (D("2.04"), D("7.50"))
    assert tuned.warnings == () and tuned.detail["plan"]["uncalibrated"] == []


def test_TA_09_no_contract_that_fits_is_no_trade_and_the_stop_is_not_narrowed():
    out = plan_trade(parse_answer(answer_json()), context(budget=inputs(equity=D(10_000), cash=D(10_000), capital=D(10_000))))
    assert out.status == "no_trade" and out.code == "no_trade_budget" and out.proposal is None
    assert out.detail["plan"]["stop"] == "1.70" and "stop stays where it is" in out.why
    cash = plan_trade(parse_answer(answer_json()), context(budget=inputs(cash=D(100))))
    assert cash.status == "no_trade" and cash.code == "no_room_cash" and cash.detail["plan"]["stop"] == "1.70"


def test_TA_09_the_precheck_stops_a_proposal_before_any_sizing():
    out = plan_trade(parse_answer(answer_json()), context(budget=inputs(equity=D(85_000), max_drawdown_pct=D(10))))
    assert out.status == "blocked_precheck" and out.code == "max_drawdown_pct" and out.proposal is None
    stand = plan_trade(parse_answer(json.dumps({"action": "stand_aside", "reason": "evidence is thin"})), context())
    assert (stand.status, stand.code, stand.why) == ("stand_aside", "stand_aside", "evidence is thin")


def test_TA_09_every_number_of_the_thesis_comes_from_the_snapshot_or_the_output_does_not_validate():
    ok = plan_trade(parse_answer(answer_json(thesis="Implied volatility is rich at 31 percent and the 200 and 210 calls trade tight.")), context())
    assert ok.status == "proposed"
    bad = plan_trade(parse_answer(answer_json(thesis="The stock will reach 250 and volatility is 47 percent.")), context())
    assert (bad.status, bad.code, bad.proposal) == ("invalid_output", "unverified_numbers", None) and set(bad.unverified) == {"250", "47"}


@pytest.mark.parametrize("change, code", [
    (dict(structure="credit_call_spread"), "structure_mismatch"),
    (dict(underlying="MSFT"), "bad_legs"),
    (dict(legs=[{"contract_id": "OPT:AAPL:2026-12-18:C:230", "side": "buy"}], structure="long_call"), "bad_legs"),
    (dict(legs=[{"contract_id": "OPT:MSFT:2026-12-18:C:200", "side": "buy"}], structure="long_call"), "bad_legs"),
    (dict(legs=[{"contract_id": "AAPL 200C", "side": "buy"}], structure="long_call"), "bad_legs"),
    (dict(legs=[{"contract_id": C200, "side": "buy"}, {"contract_id": C200, "side": "sell"}]), "bad_legs"),
    (dict(expiration="2026-12-19"), "expiration_mismatch"),
])
def test_TA_09_legs_that_are_not_in_the_chain_or_do_not_make_the_structure_are_refused(change, code):
    out = plan_trade(parse_answer(answer_json(**change)), context())
    assert (out.status, out.code, out.proposal) == ("invalid_output", code, None)


def test_TA_09_an_entry_never_touches_a_position_the_desk_holds():
    out = plan_trade(parse_answer(answer_json()), context(current_book={C210: D(2)}))
    assert (out.status, out.code) == ("no_trade", "touches_position")
    out2 = plan_trade(parse_answer(answer_json()), context(current_book={C200: D(3)}))
    assert out2.status == "proposed" and out2.proposal.legs[0].qty == D(5)                    # more of the same, from a held 3


def test_TA_09_a_premium_selling_structure_is_built_complete_and_marked_for_the_door():
    condor = answer_json(structure="iron_condor", expiration="2026-12-18",
                         legs=[{"contract_id": P170, "side": "buy"}, {"contract_id": P180, "side": "sell"}, {"contract_id": C210, "side": "sell"},
                               {"contract_id": C220, "side": "buy"}])
    out = plan_trade(parse_answer(condor), context())
    p = out.proposal
    assert out.status == "proposed" and p.structure == "iron_condor" and p.net_premium < 0 and p.max_loss_usd > 0
    assert any(w.startswith("premium_selling_closed") for w in out.warnings)
    assert p.take_profit < p.limit_price < p.stop_loss                                           # a short premium: the stop is above, the objective below
    opened = plan_trade(parse_answer(condor), context(premium_selling_open=True))
    assert not any(w.startswith("premium_selling_closed") for w in opened.warnings)
    naked = plan_trade(parse_answer(answer_json(structure="short_call", legs=[{"contract_id": C210, "side": "sell"}])), context())
    assert naked.status == "proposed" and naked.proposal.max_loss_usd is None and naked.proposal.structure == "short_call"
    covered = plan_trade(parse_answer(answer_json(structure="covered_call", legs=[{"contract_id": STOCK, "side": "buy"}, {"contract_id": C210, "side": "sell"}])), context())
    assert covered.status == "proposed" and covered.proposal.legs[0].qty == covered.proposal.legs[1].qty * 100
    # decision 1264: a covered call carries no stop or target on the order; they are kept apart for orders of their own
    assert covered.proposal.stop_loss is None and covered.proposal.take_profit is None and set(covered.detail["exits_apart"]) == {"stop", "target", "why"}
    assert any(w.startswith("covered_call") for w in covered.warnings)
    assert out.proposal.stop_loss is not None and out.proposal.take_profit is not None                  # the other structures keep their exits


def test_TA_09_a_stop_on_the_underlying_needs_the_level_and_the_premium_priced_there():
    underlying = answer_json(stop_basis="underlying")
    missing = plan_trade(parse_answer(underlying), context())
    assert (missing.status, missing.code) == ("no_trade", "bad_stop")
    ok = plan_trade(parse_answer(underlying), context(underlying_stop=D("188.5"), premium_at_stop=D("1.40")))
    assert ok.status == "proposed" and ok.proposal.stop_loss == D("1.40") and ok.detail["plan"]["basis"] == "underlying"


def test_TA_09_run_trader_asks_again_once_when_the_answer_holds_a_size_and_gives_up_after():
    model = ScriptedModel(answer_json(qty=5), answer_json())
    out = run_trader(model, context())
    assert out.status == "proposed" and len(model.calls) == 2
    assert "refused" in model.calls[1][1][-1]["content"] and "qty was written by the model" in model.calls[1][1][-1]["content"]
    stubborn = ScriptedModel(answer_json(qty=5))
    result = run_trader(stubborn, context())
    assert (result.status, result.code, result.proposal) == ("invalid_output", "free_size", None) and len(stubborn.calls) == 2
    assert run_trader(ScriptedModel("not json"), context(), max_attempts=1).code == "bad_json"
    first = ScriptedModel(answer_json(thesis="It reaches 250 soon."), answer_json())
    assert run_trader(first, context()).status == "proposed"


def test_TA_09_a_failed_model_call_is_a_result_and_a_decision_that_is_not_retryable_is_returned_at_once():
    class Broken:
        def complete(self, *a, **k):
            raise RuntimeError("down")

    out = run_trader(Broken(), context())
    assert (out.status, out.code) == ("unavailable", "model_failed")
    model = ScriptedModel(answer_json(structure="credit_call_spread"), answer_json())
    assert run_trader(model, context()).code == "structure_mismatch" and len(model.calls) == 1
    first = model.calls[0][1]
    assert first[0]["role"] == "system" and "## Piece: sizing" in first[0]["content"] and "Chain (the only contracts you may name)" in first[1]["content"]
    assert f"- {C200}: bid 5.10 ask 5.20" in first[1]["content"] and "Pre-trade check: clear" in first[1]["content"]


def test_TA_09_the_context_refuses_what_makes_no_sense():
    with pytest.raises(TraderError):
        context(underlying="")
    with pytest.raises(TraderError):
        context(as_of=AS_OF.replace(tzinfo=None))
    with pytest.raises(TraderError):
        Quote(D("2"), D("1"))
    with pytest.raises(TraderError):
        Quote(D(-1), D("1"))
    snap = trader.snapshot_of(context())
    assert snap["quotes"][C200] == {"bid": "5.10", "ask": "5.20"} and snap["symbol"] == "AAPL"
