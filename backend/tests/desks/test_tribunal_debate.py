"""The debate of the tribunal (B9): Jonah, Simon, the moderator and the counter cut."""
import importlib.util
import json
from pathlib import Path

import pytest
from swo_kit_support import build_kit
from tribunal_support import (
    AS_OF, BEAR_CLAIMS, BULL_CLAIMS, REPORTS, SNAPSHOT, Scripted, bear_json, bull_json, evidence, moderator_json, tool_reply,
)

from hokma.desks.agents import debate as db
from hokma.desks.core.signal import contains_execution_terms
from hokma.desks.core.skills import parse_skill
from hokma.desks.core.state import new_debate
from hokma.desks.core.tools import ToolNotAllowed
from hokma.desks.plantel import SEATS

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "kit" / "template" / "desk"
_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)


WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet", "kilo", "lima", "mike"]


def run(bull, bear, mod, **kw):
    kw.setdefault("line", "SWO")
    return db.run_debate(bull, bear, mod, desk_id=7, as_of=AS_OF, snapshot=SNAPSHOT, reports=REPORTS, **kw)


def rel(*parts):
    return Path(*parts).relative_to(ROOT).as_posix()


def prompt(code, line="SWO", **kw):
    return db.seat_prompt(code, line=line, desk_id=7, as_of=AS_OF, **kw)


# ---- TA-06 Jonah ----------------------------------------------------------------------------------------------------------

def test_TA_06_jonah_defends_with_line_data_and_answers_the_rival_every_round():
    bull = Scripted(*[bull_json(i) for i in (1, 2, 3)])
    bear = Scripted(*[bear_json(i) for i in (1, 2, 3)])
    mod = Scripted(moderator_json(more=True))
    out = run(bull, bear, mod)
    assert out.rounds == 3 and out.bull is not None and out.bull.points[0].evidence[0].ref
    assert bull.users[0].count("has not spoken yet") == 1                     # the opening round: no rival yet
    for round_index in (1, 2):                                                # later rounds carry the last argument of the bear
        assert "Rebuttal: A dated catalyst does not make the premium cheaper" in bull.users[round_index]
        assert "has not spoken yet" not in bull.users[round_index]
    assert out.bull.rebuttal and "outweigh" in out.bull.rebuttal


def test_TA_06_a_later_round_without_an_answer_to_the_rival_does_not_validate():
    bull = Scripted(bull_json(1), bull_json(2, rebuttal=""))
    out = run(bull, Scripted(bear_json(1)), Scripted(moderator_json(more=True)))
    assert out.stop_reason == "invalid_output" and "rebuttal" in out.error and out.rounds == 1
    assert out.bull is not None                                                 # what was valid is kept


def test_TA_06_the_state_does_not_grow_with_the_rounds():
    bull = Scripted(*[bull_json(i) for i in range(1, 6)])
    bear = Scripted(*[bear_json(i) for i in range(1, 6)])
    out = run(bull, bear, Scripted(moderator_json(more=True)), max_rounds=5)
    assert out.rounds == 5
    assert len(out.summary) <= 900 and len(out.state["last_response"]) <= 600
    assert len(out.ledger.refuted) <= db.MAX_REFUTED and len(out.ledger.live) <= db.MAX_REFUTED
    sizes = [len(u) for u in bull.users]
    assert abs(sizes[4] - sizes[3]) < 100 and max(sizes) < 4000                 # the prompt reaches a plateau: it is not the history of rounds 1 to 4
    assert BULL_CLAIMS[0] not in bull.users[-1].split("Last argument")[0].split("Debate so far")[0]


def test_TA_06_jonah_has_no_tools_and_asking_for_one_fails():
    assert SEATS["A08"].tools == () and db.advocate_toolbox("A08").names() == ()
    with pytest.raises(ToolNotAllowed):
        db.advocate_toolbox("A08").call("quant_call", {"name": "validation.dsr"})
    bull = Scripted(tool_reply())
    with pytest.raises(ToolNotAllowed):
        run(bull, Scripted(bear_json(1)), Scripted(moderator_json()))
    assert all(c["tools"] == () for c in bull.calls)


def test_TA_06_a_number_that_is_not_in_the_snapshot_is_unverified_and_the_answer_does_not_validate():
    assert db.check_numbers(["Implied volatility rank is 72.5"], SNAPSHOT) == []
    assert db.check_numbers(["Implied volatility rank is 99.9"], SNAPSHOT) == ["99.9"]
    bad = json.loads(bull_json(1))
    bad["points"][0]["claim"] = "Implied volatility rank is 99.9 which is high"
    with pytest.raises(db.OutputError, match="unverified"):
        db.parse_bull(bad, snapshot=SNAPSHOT, opening_turn=True)
    ok = json.loads(bull_json(1))
    ok["points"][0]["claim"] = "Implied volatility rank is 72.5 which is high"
    assert db.parse_bull(ok, snapshot=SNAPSHOT, opening_turn=True).points[0].claim.endswith("high")


def test_TA_06_jonah_does_not_decide_or_size():
    with pytest.raises(db.OutputError, match="does not decide"):
        db.parse_bull(json.loads(bull_json(1, extra={"rating": "APPROVE"})), snapshot=SNAPSHOT, opening_turn=True)
    sized = json.loads(bull_json(1))
    sized["points"][0]["claim"] = "We should size the position at the maximum"
    with pytest.raises(db.OutputError, match="does not decide"):
        db.parse_bull(sized, snapshot=SNAPSHOT, opening_turn=True)
    missing_ref = json.loads(bull_json(1, ref="nowhere.at.all"))
    with pytest.raises(db.OutputError, match="not in the snapshot"):
        db.parse_bull(missing_ref, snapshot=SNAPSHOT, opening_turn=True)


def test_TA_06_the_bull_file_is_english_written_for_four_lines_and_has_a_variant_per_line():
    text = (KIT / "agents" / "bull-advocate.md").read_text()
    assert repo_rules.check(ROOT, [rel(KIT, "agents", "bull-advocate.md")]) == []
    for line in ("DT", "SWC", "SWO", "LP"):
        assert f"### Line {line}" in text
    assert "an operation that has no backtest" in prompt("A08", "SWO") and "bot in front of its backtest" not in prompt("A08", "SWO")
    assert "bot in front of its backtest" in prompt("A08", "DT") and "operation that has no backtest" not in prompt("A08", "DT")


# ---- TA-07 Simon ----------------------------------------------------------------------------------------------------------

def test_TA_07_simon_attacks_with_line_evidence_and_keeps_what_would_disprove_the_thesis():
    out = db.parse_bear(bear_json(1), snapshot=SNAPSHOT, opening_turn=False)
    assert [c.evidence[0].ref for c in out.concerns] == ["options.iv_rank", "options.spread_pct", "book.delta_overlap_pct"]
    assert out.would_disprove and "median" in out.would_disprove[0]
    empty = json.loads(bear_json(1))
    empty["would_disprove"] = []
    with pytest.raises(db.OutputError, match="would_disprove"):
        db.parse_bear(empty, snapshot=SNAPSHOT, opening_turn=False)
    result = run(Scripted(bull_json(1)), Scripted(bear_json(1)), Scripted(moderator_json(more=False)))
    assert result.bear is not None and result.bear.would_disprove


def test_TA_07_an_objection_without_data_is_rejected_not_an_attack_by_style():
    no_data = json.loads(bear_json(1))
    no_data["concerns"][1]["evidence"] = []
    with pytest.raises(db.OutputError, match="evidence"):
        db.parse_bear(no_data, snapshot=SNAPSHOT, opening_turn=False)
    invented = json.loads(bear_json(1))
    invented["concerns"][1]["evidence"] = evidence("market.sentiment_gloom")
    with pytest.raises(db.OutputError, match="not in the snapshot"):
        db.parse_bear(invented, snapshot=SNAPSHOT, opening_turn=False)
    style = json.loads(bear_json(1))
    style["concerns"][0].pop("evidence")
    with pytest.raises(db.OutputError):
        db.parse_bear(style, snapshot=SNAPSHOT, opening_turn=False)


def test_TA_07_simon_has_no_tools_and_every_number_needs_a_source():
    assert SEATS["A09"].tools == () and db.advocate_toolbox("A09").names() == ()
    with pytest.raises(ToolNotAllowed):
        db.advocate_toolbox("A09").call("data_bars")
    bad = json.loads(bear_json(1))
    bad["concerns"][0]["claim"] = "Implied volatility rank is 88.1 and rising"
    with pytest.raises(db.OutputError, match="unverified"):
        db.parse_bear(bad, snapshot=SNAPSHOT, opening_turn=False)
    simon = Scripted(tool_reply())
    with pytest.raises(ToolNotAllowed):
        run(Scripted(bull_json(1)), simon, Scripted(moderator_json()))


def test_TA_07_the_bear_file_is_english_and_scoped_by_line():
    assert repo_rules.check(ROOT, [rel(KIT, "agents", "bear-advocate.md")]) == []
    assert "richness of the volatility you would pay" in prompt("A09", "SWO")
    assert "richness of the volatility you would pay" not in prompt("A09", "LP")
    assert "durability of the advantage" in prompt("A09", "LP")


# ---- SKL-005 devils advocate -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("count", [0, 1, 2, 4, 5])
def test_SKL_005_a_bear_answer_with_other_than_three_concerns_does_not_validate(count):
    chosen = [BEAR_CLAIMS[i % len(BEAR_CLAIMS)] for i in range(count)]
    body = json.loads(bear_json(1))
    body["concerns"] = [{"claim": f"{c} variant {i}", "severity": s, "evidence": evidence(r), "mitigation": "Use a debit spread"}
                        for i, (c, s, r) in enumerate(chosen)]
    with pytest.raises(db.OutputError, match="concerns"):
        db.parse_bear(body, snapshot=SNAPSHOT, opening_turn=False)


def test_SKL_005_three_concerns_with_severity_mitigation_and_the_most_likely_failure_validate():
    out = db.BearOutput.from_dict(bear_json(1), snapshot=SNAPSHOT, opening_turn=False)
    assert len(out.concerns) == 3 and {c.severity for c in out.concerns} <= set(db.SEVERITIES)
    assert all(c.mitigation and c.evidence for c in out.concerns) and out.most_likely_failure == 1 and out.failure_reason


@pytest.mark.parametrize("edit", [
    lambda b: b.pop("most_likely_failure"),
    lambda b: b.__setitem__("most_likely_failure", {"concern": 4, "reason": "x" * 20}),
    lambda b: b.__setitem__("most_likely_failure", {"concern": 1, "reason": ""}),
    lambda b: b["concerns"][0].__setitem__("severity", "critical"),
    lambda b: b["concerns"][2].pop("mitigation"),
    lambda b: b["concerns"][1].__setitem__("claim", b["concerns"][0]["claim"]),
])
def test_SKL_005_missing_failure_point_bad_severity_missing_mitigation_or_duplicates_do_not_validate(edit):
    body = json.loads(bear_json(1))
    edit(body)
    with pytest.raises(db.OutputError):
        db.parse_bear(body, snapshot=SNAPSHOT, opening_turn=False)


def test_SKL_005_the_skill_exists_in_english_and_reaches_simon_and_nobody_else():
    path = KIT / "skills" / "devils-advocate" / "SKILL.md"
    skill = parse_skill(path.read_text(), "devils-advocate")
    assert skill.name == "devils-advocate" and any("exactly three" in p.lower() for p in skill.patterns)
    assert repo_rules.check(ROOT, [rel(path)]) == []
    assert "## Skill: devils-advocate" in prompt("A09") and "## Skill: devils-advocate" not in prompt("A08")
    assert "## Skill: devils-advocate" not in prompt("A10")


# ---- TA-30 opening marker ------------------------------------------------------------------------------------------------

def test_TA_30_the_first_speaker_gets_the_opening_marker_and_no_rival_argument():
    marker = db.opening("bear")
    assert "has not spoken yet" in marker and "do not invent" in marker
    first = db.turn_message(side="bull", round_index=1, cap=3, reports=REPORTS, snapshot=SNAPSHOT, state=new_debate(), ledger=db.DebateLedger(),
                            opponent_text=None)
    assert marker in first and "Rebuttal:" not in first and "concern 1" not in first
    later = db.turn_message(side="bull", round_index=2, cap=3, reports=REPORTS, snapshot=SNAPSHOT, state=new_debate(), ledger=db.DebateLedger(),
                            opponent_text="- (high) a real argument")
    assert marker not in later and "a real argument" in later
    assert db.opponent_argument_or_opening("  ", "bull") == db.opening("bull")


def test_TA_30_the_opening_turn_cannot_hold_an_invented_rebuttal():
    with pytest.raises(db.OutputError, match="nothing to rebut"):
        db.parse_bull(json.loads(bull_json(1, extra={"rebuttal": "The bear says the premium is too high"})), snapshot=SNAPSHOT, opening_turn=True)
    bull = Scripted(bull_json(1))
    run(bull, Scripted(bear_json(1)), Scripted(moderator_json(more=False)))
    assert "Last argument of the bear side:\n(The bear side has not spoken yet" in bull.users[0]
    assert "Rebuttal" not in bull.users[0].split("Last argument of the bear side:")[1]


# ---- TA-18 counter cut ---------------------------------------------------------------------------------------------------

def test_TA_18_the_round_cap_comes_from_the_profile_of_the_line_and_swo_defaults_to_three():
    assert db.LINE_ROUNDS["SWO"] == 3 and db.LINE_ROUNDS["DT"] < db.LINE_ROUNDS["SWO"] and db.LINE_ROUNDS["LP"] >= 3
    assert db.round_cap("SWO") == 3 and db.round_cap("DT") == 1
    assert db.round_cap("SWO", 2) == 2 and db.round_cap("SWO", 99) == db.HARD_MAX_ROUNDS
    for bad in (0, -1, True, "3"):
        with pytest.raises(db.DebateError):
            db.round_cap("SWO", bad)


def test_TA_18_an_engine_that_asks_to_go_on_does_not_pass_the_cap():
    assert db.should_continue(0, line="SWO", moderator_wants_more=False) is True        # at least one round
    assert db.should_continue(1, line="SWO", moderator_wants_more=True) is True
    assert db.should_continue(1, line="SWO", moderator_wants_more=False) is False
    assert db.should_continue(3, line="SWO", moderator_wants_more=True) is False
    assert db.should_continue(1, line="DT", moderator_wants_more=True) is False
    assert db.should_continue(5, line="SWO", moderator_wants_more=True, max_rounds=99) is False
    out = run(Scripted(*[bull_json(i) for i in (1, 2, 3, 4)]), Scripted(*[bear_json(i) for i in (1, 2, 3, 4)]), Scripted(moderator_json(more=True)))
    assert out.rounds == 3 and out.stop_reason == "cap"


def test_TA_18_a_moderator_that_says_no_more_shortens_the_debate_and_dt_cuts_fast():
    out = run(Scripted(bull_json(1)), Scripted(bear_json(1)), Scripted(moderator_json(more=False)))
    assert out.rounds == 1 and out.stop_reason == "settled"
    fast = run(Scripted(bull_json(1)), Scripted(bear_json(1)), Scripted(moderator_json(more=True)), line="DT")
    assert fast.rounds == 1 and fast.stop_reason == "cap"


# ---- HFC-008 moderator ---------------------------------------------------------------------------------------------------

def test_HFC_008_the_moderator_lists_refuted_arguments_the_central_disagreement_and_whether_another_round_follows():
    claim = BEAR_CLAIMS[0][0]
    refuted = [("bear", claim, "The bull answered with the spread and open interest data")]
    mod = Scripted(moderator_json(refuted=refuted, more=True))
    out = run(Scripted(bull_json(1)), Scripted(bear_json(1)), mod, max_rounds=1)
    assert out.note is not None and out.note.another_round is True
    assert [r.claim for r in out.note.refuted] == [claim] and out.note.central_disagreement.endswith("expensive?")
    assert out.ledger.refuted[0].refs == ("options.iv_rank",)                     # references come from the argument, not the moderator
    assert "Close round 1" in mod.users[0] and "no tools" not in mod.users[0].lower()
    assert mod.calls[0]["tools"] == ()


def test_HFC_008_an_argument_nobody_made_cannot_be_called_refuted():
    mod = Scripted(moderator_json(refuted=[("bull", "The company will triple its revenue next quarter", "Nobody said it")], more=False))
    out = run(Scripted(bull_json(1)), Scripted(bear_json(1)), mod)
    assert out.note.refuted == () and out.note.discarded == ("The company will triple its revenue next quarter",)
    assert out.ledger.refuted == ()


def test_HFC_008_a_refuted_argument_does_not_return_without_new_data_but_returns_with_it():
    dead = BULL_CLAIMS[0]
    refuted = [("bull", dead, "The bear showed the catalyst leaves little time")]
    mod = Scripted(moderator_json(refuted=refuted, more=True), moderator_json(more=False))
    same = bull_json(2, claim=dead, ref="events.days_to_earnings")            # same claim, same data: a revival
    fresh = bull_json(2, claim=dead, ref="events.earnings_date")              # same claim, new data: allowed
    bull = Scripted(bull_json(1, claim=dead), same, fresh)
    out = run(bull, Scripted(bear_json(1), bear_json(2)), mod)
    assert out.stop_reason == "settled" and out.rounds == 2
    assert "came back without new data" in bull.users[2] or "came back without new data" in bull.users[1] + bull.users[2]
    assert out.bull.points[0].evidence[0].ref == "events.earnings_date"
    stuck = Scripted(bull_json(1, claim=dead), same)
    out2 = run(stuck, Scripted(bear_json(1), bear_json(2)), Scripted(moderator_json(refuted=refuted, more=True)))
    assert out2.stop_reason == "revival" and out2.rounds == 1


def test_HFC_008_revived_claims_and_the_ledger_are_pure_code():
    ledger = db.DebateLedger(refuted=(db.RefutedArgument("bull", BULL_CLAIMS[0], "x", ("events.days_to_earnings",)),))
    assert db.revived_claims("bull", [(BULL_CLAIMS[0] + " again", ("events.days_to_earnings",))], ledger) == [BULL_CLAIMS[0] + " again"]
    assert db.revived_claims("bull", [(BULL_CLAIMS[0], ("events.earnings_date",))], ledger) == []
    assert db.revived_claims("bear", [(BULL_CLAIMS[0], ("events.days_to_earnings",))], ledger) == []
    full = db.DebateLedger(refuted=tuple(db.RefutedArgument("bear", f"claim about {WORDS[i]} matters", "r") for i in range(db.MAX_REFUTED)))
    bull = db.BullOutput.from_dict(bull_json(1), snapshot=SNAPSHOT, opening_turn=True)
    note, after = db.parse_moderator(moderator_json(refuted=[("bull", BULL_CLAIMS[0], "answered")]), snapshot=SNAPSHOT, ledger=full, bull=bull, bear=None)
    assert len(after.refuted) == db.MAX_REFUTED and after.refuted[-1].claim == BULL_CLAIMS[0] and len(note.refuted) == 1


def test_HFC_008_the_skill_exists_in_english_and_reaches_amos_only_and_amos_has_no_tool_in_the_moderation():
    path = KIT / "skills" / "debate-moderator" / "SKILL.md"
    assert parse_skill(path.read_text(), "debate-moderator").name == "debate-moderator"
    assert repo_rules.check(ROOT, [rel(path)]) == []
    assert "## Skill: debate-moderator" in prompt("A10") and "## Skill: debate-moderator" not in prompt("A08")
    assert db.judge_toolbox().names() == ()
    with pytest.raises(ToolNotAllowed):
        db.judge_toolbox().call("quant_call")
    asked = Scripted(tool_reply())
    with pytest.raises(ToolNotAllowed):
        run(Scripted(bull_json(1)), Scripted(bear_json(1)), asked)


# ---- prompt integrity of the three seats (TA-41 style) -------------------------------------------------------------------

@pytest.mark.parametrize("code", ["A08", "A09", "A10"])
def test_TA_06_TA_07_TA_08_the_prompts_are_english_have_no_origin_and_no_execution_words(code):
    files = [rel(KIT, "agents", f"{SEATS[code].slug}.md")]
    assert repo_rules.check(ROOT, files) == []
    for line in ("DT", "SWC", "SWO", "LP"):
        text = prompt(code, line)
        assert contains_execution_terms(text) is None, (code, line, contains_execution_terms(text))
        assert "\u2014" not in text and f"line {line}" in text.lower() or f"Line {line}" in text


@pytest.mark.parametrize("code", ["A08", "A09", "A10"])
def test_TA_41_a_seat_file_that_does_not_carry_the_line_is_refused_and_other_lines_never_enter(tmp_path, code):
    kit = build_kit(tmp_path / "kit", lines={code: "SWC, LP"})
    with pytest.raises(db.PromptScopeError):
        db.seat_prompt(code, line="SWO", desk_id=1, as_of=AS_OF, kit_root=kit)
    assert db.seat_prompt(code, line="SWC", desk_id=1, as_of=AS_OF, kit_root=kit)
    with pytest.raises(db.PromptScopeError):
        db.seat_prompt("A11", line="SWO", desk_id=1, as_of=AS_OF)


def test_TA_41_only_the_declared_skills_enter_each_seat_prompt():
    assert db.only_line_blocks("a\n### Line DT\nx\n### Line SWO\ny\n## Next\nz", "SWO") == "a\n### Line SWO\ny\n## Next\nz"
    for code, names in {"A08": [], "A09": ["devils-advocate"], "A10": ["debate-moderator", "strategy-review-rubric", "overfitting-checks", "double-review"]}.items():
        text = prompt(code)
        found = sorted(line[len("## Skill: "):] for line in text.splitlines() if line.startswith("## Skill: "))
        assert found == sorted(names), code


def test_TA_41_the_five_skills_of_the_tribunal_are_in_the_manifest_with_their_digest():
    from hokma.desks.core.skills import load_skill, read_manifest
    manifest = read_manifest(KIT / "skills")["skills"]
    for name in ("devils-advocate", "debate-moderator", "double-review", "strategy-review-rubric", "overfitting-checks"):
        skill = load_skill(KIT / "skills" / name / "SKILL.md")
        assert manifest[name] == {"version": skill.version, "digest": skill.digest()}
