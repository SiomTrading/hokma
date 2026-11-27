"""Amos, the judge of the tribunal (B9): scale, rating parser, rubric, double review, evidence and the rules of code."""
import importlib.util
import json
from pathlib import Path

import pytest
from tribunal_support import AS_OF, REPORTS, SNAPSHOT, Scripted, bear_json, bull_json, judge_json, tool_reply

import hokma.quant.validation  # noqa: F401  (importing registers the validation functions)
from hokma.desks.agents import debate as db
from hokma.desks.agents import judge as jg
from hokma.desks.core.signal import contains_execution_terms
from hokma.desks.core.skills import parse_skill
from hokma.desks.core.tools import ToolNotAllowed
from hokma.desks.plantel import SEATS, JudgeVerdict

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "kit" / "template" / "desk"
_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)


def rel(path):
    return Path(path).relative_to(ROOT).as_posix()


def case(line="SWO", *, symbol="ACME", snapshot=None, backtest=None, signal_source=None, bull=True, bear=True, n_tests=None):
    return jg.JudgeCase(
        line=line, desk_id=7, symbol=symbol, as_of=AS_OF,
        bull=db.parse_bull(bull_json(1), snapshot=SNAPSHOT, opening_turn=True) if bull else None,
        bear=db.parse_bear(bear_json(1), snapshot=SNAPSHOT, opening_turn=False) if bear else None,
        snapshot=snapshot if snapshot is not None else SNAPSHOT, reports=REPORTS, summary="bull: x | bear: y",
        backtest=backtest, signal_source=signal_source, n_tests=n_tests)


def amos_prompt(line="SWO"):
    return db.seat_prompt("A10", line=line, desk_id=7, as_of=AS_OF)


GOOD = {k: 0.8 for k in jg.RUBRIC_WEIGHTS}


# ---- TA-08 the judge ---------------------------------------------------------------------------------------------------------

def test_TA_08_in_swo_amos_judges_without_a_backtest_with_evidence_and_catalysts_and_returns_a_structured_verdict():
    model = Scripted(judge_json("APPROVE", 78, "solid"))
    result = jg.judge(model, case("SWO"))
    out = result.output
    assert result.rating == "APPROVE" and not result.degraded and result.flags == ()
    assert out.catalysts == ("Earnings on 2026-12-03",) and out.deciding_claim and out.weakest_claim and out.rationale
    assert out.band == "solid" and out.confidence == 78 and out.evidence_refs
    plantel = result.to_plantel()
    assert isinstance(plantel, JudgeVerdict) and plantel.rating == "APPROVE"
    assert "Argument in favor" in model.users[0] and "Argument against" in model.users[0] and "Snapshot" in model.users[0]


@pytest.mark.parametrize("line", ["DT", "SWC"])
def test_TA_08_dt_and_swc_need_a_backtest_and_without_one_the_verdict_is_review_with_no_model_call(line):
    model = Scripted(judge_json("APPROVE", 78, "solid"))
    result = jg.judge(model, case(line))
    assert result.rating == jg.REVIEW and "backtest_required" in result.flags and model.calls == []
    with_backtest = jg.judge(model, case(line, backtest={"sample_trades": 240, "out_of_sample_sharpe": 0.9}))
    assert with_backtest.rating == "APPROVE" and len(model.calls) == 1
    assert "240" in model.users[0] and "backtest" in model.users[0]


def test_TA_08_a_missing_side_or_a_verdict_that_does_not_validate_is_review_not_a_guess():
    model = Scripted(judge_json())
    assert jg.judge(model, case(bear=False)).rating == jg.REVIEW and model.calls == []
    broken = Scripted("I think this is fine")
    result = jg.judge(broken, case())
    assert result.rating == jg.REVIEW and result.degraded and "verdict_not_valid" in result.flags and len(broken.calls) == 2


def test_TA_08_a_number_in_the_verdict_that_is_not_in_the_evidence_is_unverified_and_does_not_validate():
    model = Scripted(judge_json(rationale="Implied volatility rank is 99.9 which is far above the median"))
    result = jg.judge(model, case())
    assert result.rating == jg.REVIEW and result.degraded and "unverified" in result.error


def test_TA_08_amos_gets_only_the_tools_of_his_role_asking_for_another_fails_closed():
    assert SEATS["A10"].tools == ("quant_call",) and SEATS["A10"].quant_modules == ("validation",)
    model = Scripted(tool_reply("data_bars"))
    result = jg.judge(model, case())
    assert result.rating == jg.REVIEW and "tool_requested" in result.flags
    with pytest.raises(ToolNotAllowed):
        db.judge_toolbox().call("data_bars")
    assert jg.evidence_toolbox().names() == ("quant_call",)


def test_TA_08_the_judge_file_has_the_line_variants_and_the_variants_of_other_lines_never_enter():
    assert "There is no backtest for an operation of this line" in amos_prompt("SWO")
    assert "Weigh the metrics of the backtest and of its validation" not in amos_prompt("SWO")
    assert "Weigh the metrics of the backtest and of its validation" in amos_prompt("DT")
    assert "There is no backtest for a thesis of this line" in amos_prompt("LP") and "operation of this line" not in amos_prompt("LP")


# ---- TA-34 no external tools ---------------------------------------------------------------------------------------------------

def test_TA_34_the_judge_runs_with_no_tool_and_the_prompt_carries_the_phrase():
    assert db.judge_toolbox().names() == ()
    model = Scripted(judge_json())
    jg.judge(model, case())
    assert model.calls[0]["tools"] == ()
    assert jg.NO_EXTERNAL_TOOLS in model.calls[0]["system"]
    assert jg.NO_EXTERNAL_TOOLS.startswith("Use only the evidence in this prompt")
    assert "Use only the evidence in this prompt" in (KIT.parent / "agents" / "desk-judge.md").read_text()


def test_TA_34_asking_for_a_tool_in_the_judgment_fails():
    with pytest.raises(ToolNotAllowed):
        db.judge_toolbox().call("quant_call", {"name": "validation.dsr"})
    model = Scripted(tool_reply("quant_call"))
    result = jg.judge(model, case())
    assert result.rating == jg.REVIEW and result.flags == ("tool_requested",) and result.output is None


# ---- TA-39 rating parser ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("APPROVE", "APPROVE"), ("Rating: APPROVE", "APPROVE"), ("rating = approve_with_conditions", "APPROVE_WITH_CONDITIONS"),
    ("Approve with conditions", "APPROVE_WITH_CONDITIONS"), ("**REJECT**", "REJECT"), ("Final rating: Reject.", "REJECT"),
    ("I approve this one.", "APPROVE"), ('{"rating": "REJECT"}', "REJECT"), ("review", "REVIEW"), ("REVIEW", "REVIEW"),
])
def test_TA_39_each_rating_of_the_scale_is_extracted_without_a_model(text, expected):
    assert jg.extract_rating(text) == expected


@pytest.mark.parametrize("text", [
    "", "no clear view", "approve or reject, hard to say", "Rating: APPROVE but also rating: REJECT", "I do not approve this",
    "never reject it", "approve, reject, approve with conditions", "Rating: maybe", "APPROVER", "unapproved",
])
def test_TA_39_an_ambiguous_text_is_review(text):
    assert jg.extract_rating(text) == jg.REVIEW


def test_TA_39_every_line_uses_its_scale_and_review_is_the_same_in_all():
    assert set(jg.RATING_SCALES) == {"RF", "DT", "SWC", "SWO", "LP"}
    for line in jg.RATING_SCALES:
        assert "REVIEW" not in jg.rating_scale(line)
        assert jg.extract_rating("REVIEW", jg.rating_scale(line)) == "REVIEW"
    assert jg.extract_rating("BUY", ("BUY", "SELL")) == "BUY"
    out = jg.JudgeOutput.from_dict(judge_json("maybe approve, maybe reject"), line="SWO", snapshot=jg.judge_snapshot(case(), {}))
    assert out.rating == jg.REVIEW


# ---- TA-18 reaches the judge as the cap; TA-40 anti-bias ------------------------------------------------------------------------------

def test_TA_40_the_anti_bias_section_of_the_swo_variant_weighs_evidence_and_catalysts_and_forbids_hold_by_conflict_and_order_bias():
    text = (KIT.parent / "agents" / "desk-judge.md").read_text()
    assert "## Anti-bias" in text and repo_rules.check(ROOT, [rel(KIT.parent / "agents" / "desk-judge.md")]) == []
    prompt = amos_prompt("SWO")
    assert "A conflict between the two sides is not a reason to pick the middle" in prompt
    assert "The order in which the two arguments appear carries no weight" in prompt
    assert "dated catalysts" in prompt and "evidence in the snapshot" in prompt
    assert contains_execution_terms(prompt) is None
    assert "Weigh the metrics of the backtest and of its validation" in amos_prompt("DT")


def test_TA_40_the_order_of_the_arguments_is_fixed_by_the_case_and_neither_side_is_always_first():
    orders = {jg.argument_order(case(symbol=s)) for s in ("ACME", "BOLT", "CRUX", "DYNE", "EDGE", "FLUX", "GRID", "HALO")}
    assert orders == {("bull", "bear"), ("bear", "bull")}
    assert jg.argument_order(case(symbol="ACME")) == jg.argument_order(case(symbol="ACME"))
    for symbol in ("ACME", "BOLT", "CRUX", "DYNE"):
        c = case(symbol=symbol)
        model = Scripted(judge_json())
        jg.judge(model, c)
        user = model.users[0]
        first, second = jg.argument_order(c)
        marks = {"bull": "Argument in favor", "bear": "Argument against"}
        assert user.index(marks[first]) < user.index(marks[second])


def test_TA_40_a_verdict_must_name_the_deciding_and_the_weakest_claim_so_a_conflict_cannot_hide_in_the_middle():
    for missing in ("deciding_claim", "weakest_claim"):
        body = json.loads(judge_json())
        body.pop(missing)
        with pytest.raises(db.OutputError, match=missing):
            jg.JudgeOutput.from_dict(body, line="SWO", snapshot=jg.judge_snapshot(case(), {}))


# ---- AH-13 anchored confidence --------------------------------------------------------------------------------------------------------

def test_AH_13_the_swo_scale_is_anchored_by_bands_that_cover_zero_to_hundred_and_lives_in_code_and_in_the_prompt():
    scale = jg.ConfidenceScale("SWO")
    bands = scale.bands
    assert [b.name for b in bands] == ["decisive", "solid", "mixed", "weak", "none"]
    assert bands[0].high == 100 and bands[-1].low == 0 and all(a.low == b.high + 1 for a, b in zip(bands, bands[1:]))
    assert all("catalyst" in b.anchor or "evidence" in b.anchor for b in bands[:4])
    model = Scripted(judge_json())
    jg.judge(model, case())
    system = model.calls[0]["system"]
    for band in bands:
        assert f"- {band.name} ({band.low}-{band.high}): {band.anchor}" in system
    assert scale.band_for(75).name == "solid"
    assert set(jg.BANDS_BY_LINE) == {"RF", "DT", "SWC", "SWO", "LP"} and jg.BANDS_BY_LINE["DT"][0].anchor != jg.BANDS_BY_LINE["SWO"][0].anchor


def test_AH_13_an_engine_that_always_answers_75_without_the_justification_of_the_band_does_not_validate():
    always_75 = {"rating": "APPROVE", "confidence": 75, "deciding_claim": "The catalyst is dated", "weakest_claim": "The premium is rich",
                 "rationale": "The dated catalyst carries the case", "catalysts": ["Earnings on 2026-12-03"]}
    model = Scripted(json.dumps(always_75))
    result = jg.judge(model, case())
    assert result.rating == jg.REVIEW and result.degraded and "band_justification" in result.error
    for patch, text in (({"band": "solid", "band_justification": "ok"}, "band_justification"),
                        ({"band": "solid", "band_justification": "This is a long justification that never names the level at all", "evidence_refs": ["events.days_to_earnings"]}, "band_justification"),
                        ({"band": "solid", "band_justification": "The solid band fits because the catalyst is dated and the data agrees", "evidence_refs": ["nowhere.here"]}, "evidence_refs"),
                        ({"band": "decisive", "confidence": 75, "band_justification": "The decisive band is chosen because everything agrees with the data", "evidence_refs": ["options.iv_rank"]}, "outside the band")):
        body = {**always_75, **patch}
        with pytest.raises(db.OutputError, match=text):
            jg.JudgeOutput.from_dict(body, line="SWO", snapshot=jg.judge_snapshot(case(), {}))
    ok = jg.JudgeOutput.from_dict(judge_json("APPROVE", 75, "solid"), line="SWO", snapshot=jg.judge_snapshot(case(), {}))
    assert ok.confidence == 75 and ok.band == "solid"


# ---- AS-001 realistic expectations -----------------------------------------------------------------------------------------------------

def test_AS_001_the_realistic_expectation_is_a_rule_of_the_judge_with_its_medium_confidence_and_the_citation_lives_only_in_the_notices():
    path = KIT.parent / "agents" / "desk-judge.md"
    text = path.read_text()
    section = text.split("## Realistic expectations")[1].split("## ")[0]
    assert "0.37" in section and "48 percent" in section and "medium confidence" in section and "prior, not as a threshold" in section
    assert repo_rules.check(ROOT, [rel(path), ".notices/THIRD_PARTY.txt"]) == []
    notices = (ROOT / ".notices" / "THIRD_PARTY.txt").read_text()
    assert "4,843" in notices and "median Sharpe" in notices and "medium confidence" in notices
    for kit_file in KIT.rglob("*"):
        if kit_file.is_file():
            assert "4,843" not in kit_file.read_text(errors="ignore")
    assert "Realistic expectations" in amos_prompt("SWO")


def test_AS_001_the_reference_numbers_are_in_the_snapshot_so_the_judge_may_quote_them():
    snap = jg.judge_snapshot(case(), {})
    assert snap["reference"]["median_sharpe"] == 0.37 and snap["reference"]["confidence"] == "medium"
    assert db.check_numbers(["The median Sharpe of replications is 0.37 and 0.48 of them clear 1.96"], snap) == []
    assert db.check_numbers(["The median Sharpe of replications is 0.51"], snap) == ["0.51"]


# ---- CTS-007 rubric -----------------------------------------------------------------------------------------------------------------------

def test_CTS_007_the_rubric_has_eight_criteria_with_weights_that_add_to_one_hundred():
    assert sum(jg.RUBRIC_WEIGHTS.values()) == 100 and len(jg.RUBRIC_WEIGHTS) == 8
    assert dict(jg.RUBRIC_WEIGHTS) == {"plausibility": 20, "overfitting": 20, "sample": 15, "regime": 10, "exit": 10, "concentration": 10,
                                       "execution_realism": 10, "invalidation": 5}
    assert jg.CRITICAL_CRITERIA == ("plausibility", "overfitting")


def test_CTS_007_hand_scored_cases_pass_review_or_reject_by_the_total():
    passed = jg.rubric(GOOD)
    assert passed.total == 80.0 and passed.verdict == "pass" and passed.failed_critical == ()
    middling = jg.rubric({k: 0.6 for k in jg.RUBRIC_WEIGHTS})
    assert middling.total == 60.0 and middling.verdict == "review"
    low = jg.rubric({**{k: 0.5 for k in jg.RUBRIC_WEIGHTS}, "sample": 0.0, "regime": 0.0, "exit": 0.0, "concentration": 0.0, "execution_realism": 0.0})
    assert low.total == 22.5 and low.verdict == "reject"
    exact = jg.rubric({"plausibility": 1, "overfitting": 1, "sample": 1, "regime": 1, "exit": 0.0, "concentration": 0, "execution_realism": 0, "invalidation": 0})
    assert exact.total == 65.0 and exact.verdict == "review"
    boundary = jg.rubric({**{k: 1 for k in jg.RUBRIC_WEIGHTS}, "invalidation": 0, "execution_realism": 0.5})
    assert boundary.total == 90.0 and boundary.contributions["execution_realism"] == 5.0


@pytest.mark.parametrize("criterion", ["plausibility", "overfitting"])
def test_CTS_007_a_failure_in_plausibility_or_overfitting_rejects_even_when_the_total_passes(criterion):
    scores = {k: 1.0 for k in jg.RUBRIC_WEIGHTS}
    scores[criterion] = 0.2
    result = jg.rubric(scores)
    assert result.total == 84.0 and result.total >= jg.RubricThresholds().pass_total
    assert result.verdict == "reject" and result.failed_critical == (criterion,)
    scores["sample"] = 0.0                                              # a weak non critical criterion does not reject by itself
    assert jg.rubric({**GOOD, "sample": 0.0}).verdict in ("pass", "review")


def test_CTS_007_the_marks_are_uncalibrated_defaults_and_the_result_says_so_and_can_be_replaced():
    default = jg.rubric(GOOD)
    assert default.warnings and "uncalibrated" in default.warnings[0]
    calibrated = jg.rubric(GOOD, jg.RubricThresholds(pass_total=85.0, reject_total=40.0, critical_floor=0.5, calibrated=True))
    assert calibrated.warnings == () and calibrated.verdict == "review"


@pytest.mark.parametrize("scores", [{}, {**GOOD, "plausibility": 1.2}, {**GOOD, "sample": True}, {**GOOD, "extra": 0.5},
                                    {k: v for k, v in GOOD.items() if k != "regime"}, {**GOOD, "exit": "high"}])
def test_CTS_007_scores_that_are_not_eight_numbers_from_zero_to_one_do_not_validate(scores):
    with pytest.raises(db.OutputError):
        jg.rubric(scores)


def test_CTS_007_the_rubric_skill_exists_in_english_and_reaches_amos_only():
    path = KIT / "skills" / "strategy-review-rubric" / "SKILL.md"
    skill = parse_skill(path.read_text(), "strategy-review-rubric")
    assert "uncalibrated" in path.read_text() and skill.name == "strategy-review-rubric"
    assert repo_rules.check(ROOT, [rel(path)]) == []
    assert "## Skill: strategy-review-rubric" in amos_prompt() and "## Skill: strategy-review-rubric" not in db.seat_prompt("A09", line="SWO", desk_id=1, as_of=AS_OF)


# ---- ECT-021 overfitting checks ------------------------------------------------------------------------------------------------------------------

def test_ECT_021_the_false_positive_probability_with_n_tests_is_one_minus_095_to_the_n():
    assert jg.false_positive_probability(1) == pytest.approx(0.05)
    assert jg.false_positive_probability(20) == pytest.approx(0.641514, abs=1e-6)         # written by hand: 1 - 0.95 ** 20
    assert jg.false_positive_probability(14) == pytest.approx(0.512325, abs=1e-6)
    assert jg.false_positive_probability(100) == pytest.approx(0.994079, abs=1e-6)
    for bad in (0, -3, True, 1.5, 2_000_000):
        with pytest.raises(Exception):
            jg.false_positive_probability(bad)
    assert jg.count_disclosure(20) == {"n_tests": 20, "false_positive_probability": 0.641514, "alpha": 0.05}
    snap = jg.judge_snapshot(case(n_tests=20), {})
    assert snap["evidence"]["tests"]["false_positive_probability"] == 0.641514
    assert db.check_numbers(["With 20 tests the chance of a false positive is 0.641514"], snap) == []


def test_ECT_021_the_skill_exists_in_english_and_reaches_amos_only_and_never_teaches_the_invented_formulas():
    path = KIT / "skills" / "overfitting-checks" / "SKILL.md"
    text = path.read_text()
    assert parse_skill(text, "overfitting-checks").name == "overfitting-checks" and repo_rules.check(ROOT, [rel(path)]) == []
    assert "0.95 to the power N" in text and "For one test it is 0.05" in text and "the chance hurdle is zero" in text
    assert "ask the module" in text.lower() or "Ask the module" in text
    assert "## Skill: overfitting-checks" in amos_prompt("SWO")
    for code in ("A08", "A09"):
        assert "## Skill: overfitting-checks" not in db.seat_prompt(code, line="SWO", desk_id=1, as_of=AS_OF)
    for line in ("DT", "SWC", "LP"):
        assert "## Skill: overfitting-checks" in amos_prompt(line)


def test_ECT_021_the_judge_asks_the_validation_module_for_evidence_and_never_computes_it():
    box = jg.evidence_toolbox()
    got = jg.gather_evidence(box, [jg.EvidenceRequest("validation.expected_max_sharpe", {"n_trials": 10, "sr_std": 0.1})])
    value = got["validation.expected_max_sharpe"]
    assert value["unit"] and isinstance(value["value"], float) and value["value"] > 0
    model = Scripted(judge_json(rationale=f"The chance hurdle is {value['value']} for ten trials and the catalyst is dated"))
    result = jg.judge(model, case(), evidence_requests=[jg.EvidenceRequest("validation.expected_max_sharpe", {"n_trials": 10, "sr_std": 0.1})])
    assert result.rating == "APPROVE" and str(value["value"]) in model.users[0] and result.evidence["validation.expected_max_sharpe"]["value"] == value["value"]
    source = Path(jg.__file__).read_text()
    for banned in ("import numpy", "import scipy", "import pandas", "from scipy", "from numpy", "statsmodels"):
        assert banned not in source


def test_ECT_021_only_functions_of_the_validation_module_can_be_asked_and_a_failed_request_is_reported_not_guessed():
    with pytest.raises(db.OutputError):
        jg.EvidenceRequest("metrics.sharpe", {})
    with pytest.raises(db.OutputError):
        jg.EvidenceRequest("validation.dsr; drop", {})
    box = jg.evidence_toolbox()
    refused = box.call("quant_call", {"name": "metrics.sharpe", "arguments": {}})
    assert not refused.ok and refused.error_kind == "bad_input"
    failed = jg.gather_evidence(box, [jg.EvidenceRequest("validation.expected_max_sharpe", {"n_trials": 10})])
    assert failed["validation.expected_max_sharpe"]["error"] == "bad_input"
    too_big = jg.gather_evidence(box, [jg.EvidenceRequest("validation.expected_max_sharpe", {"n_trials": 10_000_000, "sr_std": 0.1})])
    assert too_big["validation.expected_max_sharpe"]["error"] == "bad_input"          # sizes are limited by quant.limits
    with pytest.raises(db.OutputError):
        jg.gather_evidence(box, [jg.EvidenceRequest("validation.dsr", {})] * 9)
    with pytest.raises(ToolNotAllowed):
        box.call("data_bars", {})


# ---- SKL-004 double review ----------------------------------------------------------------------------------------------------------------------------

def reviewer_pair(first_replies, second_replies):
    kw = dict(line="SWO", desk_id=7, as_of=AS_OF)
    a, b = Scripted(*first_replies), Scripted(*second_replies)
    return a, b, jg.RubricReviewer(a, **kw), jg.RubricReviewer(b, **kw)


def review_json(scores=None, issues=()):
    return json.dumps({"scores": scores or GOOD, "issues": list(issues)})


def test_SKL_004_two_independent_reviewers_with_the_same_rubric_and_no_shared_context():
    a, b, ra, rb = reviewer_pair([review_json(issues=["IssueFromFirst the exit rule is vague"])] * 3,
                                 [review_json({**GOOD, "plausibility": 0.1}, issues=["IssueFromSecond the mechanism is not stated"])] * 3)
    result = jg.double_review("subject v0", ra, rb, revise=lambda s, issues: s + " revised")
    assert result.status == jg.REVIEW and result.rounds == 3
    for model, other in ((a, "IssueFromSecond"), (b, "IssueFromFirst")):
        for call in model.calls:
            assert other not in call["system"] and all(other not in m.content for m in call["messages"])
    assert a.calls[0]["system"] == b.calls[0]["system"]                                    # the same rubric and brief
    assert all(len(c["messages"]) == 1 for c in [*a.calls, *b.calls])                       # no history is kept inside a reviewer
    assert "subject v0 revised" in a.users[1] and "subject v0" in b.users[0]


def test_SKL_004_both_must_approve_and_one_failed_review_blocks_the_gate():
    _, _, ra, rb = reviewer_pair([review_json()], [review_json()])
    assert jg.double_review("s", ra, rb).approved
    _, _, ra, rb = reviewer_pair([review_json()], [review_json({**GOOD, "overfitting": 0.1})])
    result = jg.double_review("s", ra, rb)
    assert result.status == jg.REVIEW and result.rounds == 1 and not result.history[0].second.passed and result.history[0].first.passed


def test_SKL_004_the_loop_has_at_most_three_rounds_and_then_review_and_a_fix_in_round_two_is_approved():
    calls = []
    never = lambda subject, n: (calls.append(n), jg.ReviewVerdict(False, ("still wrong",)))[1]
    result = jg.double_review("s", never, never, revise=lambda s, i: s + "!", max_rounds=10)
    assert result.status == jg.REVIEW and result.rounds == 3 and calls == [1, 1, 2, 2, 3, 3]
    fixed = lambda subject, n: jg.ReviewVerdict(subject.endswith("!"), () if subject.endswith("!") else ("fix it",))
    again = jg.double_review("s", fixed, fixed, revise=lambda s, i: s + "!")
    assert again.approved and again.rounds == 2 and again.subject == "s!"
    single = jg.double_review("s", never, never)
    assert single.status == jg.REVIEW and single.rounds == 1


def test_SKL_004_a_reviewer_that_breaks_or_answers_badly_does_not_approve():
    def boom(subject, n):
        raise RuntimeError("engine down")
    ok = lambda subject, n: jg.ReviewVerdict(True)
    assert jg.double_review("s", boom, ok).status == jg.REVIEW
    assert jg.double_review("s", ok, lambda subject, n: "yes").status == jg.REVIEW
    _, _, ra, rb = reviewer_pair(["not json"], [review_json()])
    result = jg.double_review("s", ra, rb)
    assert result.status == jg.REVIEW and "did not validate" in result.history[0].first.issues[0]


def test_SKL_004_a_failed_gate_turns_an_approval_into_review():
    model = Scripted(judge_json("APPROVE", 78, "solid"))
    failed = jg.DoubleReviewResult(jg.REVIEW, 3, "s")
    result = jg.judge(model, case(), gate=failed)
    assert result.rating == jg.REVIEW and result.flags == ("double_review_not_approved",)
    approved = jg.judge(model, case(), gate=jg.DoubleReviewResult("approved", 1, "s"))
    assert approved.rating == "APPROVE"


def test_SKL_004_the_skill_exists_in_english_and_reaches_amos_only():
    path = KIT / "skills" / "double-review" / "SKILL.md"
    skill = parse_skill(path.read_text(), "double-review")
    assert skill.name == "double-review" and "three rounds" in path.read_text() and repo_rules.check(ROOT, [rel(path)]) == []
    assert "## Skill: double-review" in amos_prompt() and "## Skill: double-review" not in db.seat_prompt("A08", line="SWO", desk_id=1, as_of=AS_OF)


# ---- KR-011 cost and portfolio check ----------------------------------------------------------------------------------------------------------------------

def test_KR_011_a_candle_signal_without_costs_and_portfolio_is_rejected_whatever_the_engine_says():
    bare = {k: v for k, v in SNAPSHOT.items() if k not in ("costs", "portfolio")}
    model = Scripted(judge_json("APPROVE", 78, "solid"))
    result = jg.judge(model, case(snapshot=bare, signal_source="candles"))
    assert result.rating == "REJECT" and result.flags == ("candle_signal_without_costs_and_portfolio",)
    no_portfolio = {k: v for k, v in SNAPSHOT.items() if k != "portfolio"}
    assert jg.judge(model, case(snapshot=no_portfolio, signal_source="candles")).rating == "REJECT"


def test_KR_011_with_costs_and_portfolio_the_candle_signal_is_judged_and_other_signals_do_not_need_them():
    model = Scripted(judge_json("APPROVE", 78, "solid"))
    assert jg.judge(model, case(signal_source="candles")).rating == "APPROVE"
    bare = {k: v for k, v in SNAPSHOT.items() if k not in ("costs", "portfolio")}
    assert jg.judge(model, case(snapshot=bare)).rating == "APPROVE"


def test_KR_011_the_rule_is_written_in_the_judge_file_in_english_and_injected_in_amos_prompt():
    path = KIT.parent / "agents" / "desk-judge.md"
    assert "## Cost and portfolio check" in path.read_text() and repo_rules.check(ROOT, [rel(path)]) == []
    prompt = amos_prompt("SWO")
    assert "candles or from price context is not accepted until it has passed through costs and through the portfolio" in prompt


# ---- more rules of code and the prompt of the line (TA-41 style) ------------------------------------------------------------------------------------------

def test_TA_08_an_approval_on_a_line_without_backtest_needs_a_dated_catalyst_or_it_is_review():
    model = Scripted(judge_json("APPROVE", 78, "solid", catalysts=[]))
    result = jg.judge(model, case("SWO"))
    assert result.rating == jg.REVIEW and result.flags == ("approval_without_catalyst",)
    conditional = judge_json("APPROVE_WITH_CONDITIONS", 60, "mixed", conditions=[])
    assert jg.judge(Scripted(conditional), case()).degraded
    with_conditions = judge_json("APPROVE_WITH_CONDITIONS", 60, "mixed", conditions=["Wait for the spread to narrow"])
    assert jg.judge(Scripted(with_conditions), case()).rating == "APPROVE_WITH_CONDITIONS"
    assert jg.judge(Scripted(judge_json("REJECT", 65, "mixed", catalysts=[])), case()).rating == "REJECT"
    review = jg.judge(Scripted(judge_json("REVIEW", 20, "weak", band_justification="")), case())
    assert review.rating == jg.REVIEW and review.output is not None and not review.degraded


def test_TA_08_the_judge_does_not_size_or_decide_orders():
    sized = judge_json(rationale="We should size the position at two percent of equity")
    result = jg.judge(Scripted(sized), case())
    assert result.rating == jg.REVIEW and result.degraded
    assert "size" not in "".join(jg.JudgeOutput.__dataclass_fields__)


@pytest.mark.parametrize("line", ["DT", "SWC", "SWO", "LP"])
def test_TA_41_amos_prompt_per_line_is_english_has_no_origin_and_carries_its_declared_skills_only(line):
    prompt = amos_prompt(line)
    names = [row[len("## Skill: "):] for row in prompt.splitlines() if row.startswith("## Skill: ")]
    assert sorted(names) == ["debate-moderator", "double-review", "overfitting-checks", "strategy-review-rubric"]
    assert contains_execution_terms(prompt) is None and "\u2014" not in prompt
    assert repo_rules.origin_hits(prompt) == [] and repo_rules.spanish_hits(prompt) == []
    for other in ("DT", "SWC", "SWO", "LP"):
        assert (f"### Line {other}" in prompt) is (other == line)       # the variant of another line never enters


def test_TA_41_the_three_seat_files_and_five_skills_exist_with_the_front_matter_the_creation_of_a_desk_expects():
    from hokma.desks.core.skills import split_frontmatter
    expected = {"bull-advocate": [], "bear-advocate": ["devils-advocate"],
                "desk-judge": ["debate-moderator", "strategy-review-rubric", "overfitting-checks", "double-review"]}
    for slug, skills in expected.items():
        meta, body = split_frontmatter((KIT.parent / "agents" / f"{slug}.md").read_text(), slug)
        assert meta["slug"] == slug and meta["lines"] == ["DT", "SWC", "SWO", "LP"] and meta["skills"] == skills
        assert "SWO" in meta["lines"] and body.strip()
        for name in skills:
            assert parse_skill((KIT / "skills" / name / "SKILL.md").read_text(), name).name == name
