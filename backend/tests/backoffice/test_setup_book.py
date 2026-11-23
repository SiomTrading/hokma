"""Practice book of setup candidates and the scoring of blind decisions (SPEC-018: CTS-019). Values by hand."""
from decimal import Decimal

import pytest

from hokma.backoffice import setup_book as sb
from hokma.backoffice.setup_book import Bar, Candidate, Decision, Outcome, Scored, SetupBookError

D = Decimal


def bar(day, o, h, lo, c):
    return Bar.of(day, o, h, lo, c)


CAND = Candidate("c1", "AAA", "2026-03-02", D(100), D(95), D(110), captured_on="2026-03-02", rule_expected="enter")
SLOW = [bar("2026-03-03", 100, 101, "99.5", "100.5"), bar("2026-03-04", 100.5, "106.2", 100, 104), bar("2026-03-05", 104, 105, 102, "103.8"),
        bar("2026-03-06", "103.8", 105, 103, 104), bar("2026-03-09", 104, 105, 103, 104)]


def test_costs_lower_the_figures_before_the_label_is_read():
    raw = sb.outcome(CAND, SLOW, 3)
    assert (raw.forward_return_pct, raw.mfe_pct, raw.mae_pct) == (D("3.8"), D("6.2"), D("-0.5")) and raw.label == "WORKED"         # mfe 6.2 reaches 6
    net = sb.outcome(CAND, SLOW, 3, cost_pct=D("0.5"))
    assert (net.forward_return_pct, net.mfe_pct, net.mae_pct) == (D("3.3"), D("5.7"), D("-1.0")) and net.label == "NEUTRAL"       # costs keep it under both marks
    assert not net.stop_hit and not net.target_hit and net.exit_date == "2026-03-05"


def test_the_five_day_window_is_pending_until_enough_bars_exist():
    assert sb.outcome(CAND, SLOW[:3], 5).label == "PENDING" and not sb.outcome(CAND, SLOW[:3], 5).matured
    assert sb.outcome(CAND, SLOW, 5).matured
    row = sb.book_row(CAND, SLOW[:3])
    assert row["outcomes"]["5d"]["label"] == "PENDING" and row["outcomes"]["3d"]["matured"] is True and row["overall"] == row["outcomes"]["3d"]["label"]


def test_when_the_stop_and_the_target_are_in_the_same_bar_the_stop_wins():
    both = [bar("2026-03-03", 100, 101, 99, 100), bar("2026-03-04", 100, 111, 94, 100), bar("2026-03-05", 100, 101, 99, 100)]
    o = sb.outcome(CAND, both, 3)
    assert o.stop_hit and not o.target_hit and o.label == "FAILED_STOP" and o.exit_date == "2026-03-04"
    assert o.forward_return_pct == D(-5)                                  # out at the stop, 95
    assert o.mfe_pct == D(1)                                              # the high of the stop bar is not counted, the bar before it is


def test_a_gap_through_the_stop_exits_at_the_open():
    gap = [bar("2026-03-03", 93, 94, 92, 93), bar("2026-03-04", 93, 94, 92, 93), bar("2026-03-05", 93, 94, 92, 93)]
    o = sb.outcome(CAND, gap, 3)
    assert o.stop_hit and o.forward_return_pct == D(-7)


def test_a_target_reached_without_the_stop_ends_the_trade_there():
    up = [bar("2026-03-03", 100, 101, 99, 100), bar("2026-03-04", 101, 111, 100, 109), bar("2026-03-05", 109, 120, 100, 119)]
    o = sb.outcome(CAND, up, 3, cost_pct=D(1))
    assert o.target_hit and not o.stop_hit and o.forward_return_pct == D(9) and o.mfe_pct == D(9) and o.label == "STRONG_WINNER"


def test_label_rules_follow_the_thresholds():
    th = sb.Thresholds()
    assert sb.label_for(D(-3), D(1), D(-4), stop_hit=False, th=th) == "FAILED_FADE"
    assert sb.label_for(D(1), D(1), D(-6), stop_hit=False, th=th) == "CHOPPY_FAILURE"
    assert sb.label_for(D(8), D(1), D(0), stop_hit=False, th=th) == "STRONG_WINNER"
    assert sb.label_for(D(8), D(1), D(0), stop_hit=True, th=th) == "FAILED_STOP"
    assert sb.label_for(D(0), D(0), D(0), stop_hit=False, th=th) == "NEUTRAL"


def test_adherence_and_result_are_scored_on_separate_axes():
    rallies = [bar("2026-03-03", 100, 103, 100, 102), bar("2026-03-04", 102, 106, 102, 105), bar("2026-03-05", 105, 108, 105, 107)]
    falls = [bar("2026-03-03", 100, 100, 98, 99), bar("2026-03-04", 99, 99, 97, 98), bar("2026-03-05", 98, 98, 96, 97)]
    must_enter = CAND
    must_not = Candidate("c2", "BBB", "2026-03-02", D(100), D(95), D(110), captured_on="2026-03-02", rule_expected="not_enter")
    cases = {
        "followed_favorable": (Decision(must_enter, "enter"), rallies),
        "followed_adverse": (Decision(must_enter, "enter"), falls),
        "deviated_favorable": (Decision(must_not, "enter"), rallies),
        "deviated_adverse": (Decision(must_enter, "wait"), rallies),
    }
    for expected, (decision, bars) in cases.items():
        assert sb.score_decision(decision, bars, horizon=3).quadrant == expected
    passed_on_a_faller = sb.score_decision(Decision(must_not, "pass"), falls, horizon=3)
    assert passed_on_a_faller.adherence == "followed" and passed_on_a_faller.result == "favorable"


def test_late_captures_and_pending_cases_are_left_out_of_the_tallies():
    rallies = [bar("2026-03-03", 100, 103, 100, 102), bar("2026-03-04", 102, 106, 102, 105), bar("2026-03-05", 105, 108, 105, 107)]
    late = Candidate("late", "CCC", "2026-03-02", D(100), D(95), None, captured_on="2026-03-03", rule_expected="enter")
    s_late = sb.score_decision(Decision(late, "enter"), rallies, horizon=3)
    assert s_late.excluded == "late_capture" and s_late.quadrant is None
    s_pending = sb.score_decision(Decision(CAND, "enter"), rallies[:2], horizon=3)
    assert s_pending.excluded == "pending"
    s_ok = sb.score_decision(Decision(CAND, "enter"), rallies, horizon=3)
    summary = sb.summarize([s_late, s_pending, s_ok])
    assert summary["scored"] == 1 and summary["excluded"] == {"late_capture": 1, "pending": 1} and summary["quadrants"]["followed_favorable"] == 1
    assert summary["adherence_rate_pct"] == "100"


def scored(n, quadrant):
    adherence, result = quadrant.split("_")
    out = Outcome(3, True, "NEUTRAL")
    return [Scored(f"{quadrant}{i}", adherence, result, quadrant, None, out) for i in range(n)]


def test_with_fewer_than_twenty_cases_nothing_is_suggested():
    nineteen = scored(19, "followed_adverse")
    r = sb.suggestions(nineteen)
    assert r == {"scored": 19, "enough": False, "suggestions": []}
    twenty = sb.suggestions(scored(20, "followed_adverse"))
    assert twenty["enough"] and [s["code"] for s in twenty["suggestions"]] == ["review_rule"]
    mixed = sb.suggestions(scored(10, "followed_favorable") + scored(6, "deviated_favorable") + scored(4, "deviated_adverse"))
    assert {s["code"] for s in mixed["suggestions"]} == {"review_rule_gap", "adherence"}


def test_bad_candidates_bars_and_decisions_are_refused():
    with pytest.raises(SetupBookError):
        Candidate("x", "A", "2026-03-02", D(100), D(105))
    with pytest.raises(SetupBookError):
        Bar.of("2026-03-03", 100, 99, 98, 100)
    with pytest.raises(SetupBookError):
        Decision(CAND, "buy")
