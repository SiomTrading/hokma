"""TA-45: settlement measures from the real entry fill, not from the signal, and only for executed orders. Promotion is TA-44 / J4."""
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures)
from lessons.support import fill, make_decision, make_order

import hokma.lessons as lessons


@pytest.fixture
def lesson(desks_db):
    desk, decision = make_desk(desks_db)["id"], make_decision(desks_db)
    order = make_order(desks_db, desk, decision_id=decision, broker_order_id="ORD-1", price=21400.0)       # the signal asked 21400
    return desks_db, desk, lessons.propose(desk, decision, order, "Enter on the retest.", "risk")


def test_the_outcome_is_measured_from_the_real_entry_fill_not_the_signal_TA_45(lesson):
    db, desk, item = lesson
    fills = [fill("ORD-1", "BUY", 2, 21410.0, "2026-11-02 15:00:00", 2.0), fill("EXIT-9", "SELL", 2, 21510.0, "2026-11-03 15:00:00", 2.0)]
    done = lessons.settle(item.id, fills=fills, multiplier=20.0, now="2026-11-04 00:00:00", db=db)
    out = done.outcome
    assert done.status == "settled" and done.measured_from == "2026-11-02 15:00:00" and done.settled_at == "2026-11-04 00:00:00"
    assert out["entry_price"] == 21410.0 and out["signal_price"] == 21400.0 and out["exit_price"] == 21510.0
    assert out["return"] == pytest.approx(100 / 21410.0) and out["return"] != pytest.approx(110 / 21400.0)       # not measured from the signal
    assert out["pnl"] == pytest.approx(100 * 2 * 20 - 4.0) and out["costs"] == 4.0 and out["exit_basis"] == "closing_fills"
    assert out["entry_slippage"] == pytest.approx(10 / 21400.0) and out["direction"] == "long" and out["holding_days"] == pytest.approx(1.0)


def test_a_short_wins_when_price_falls_and_a_mark_closes_the_rest_TA_45(desks_db):
    desk, decision = make_desk(desks_db)["id"], make_decision(desks_db)
    order = make_order(desks_db, desk, decision_id=decision, broker_order_id="S-1")
    item = lessons.propose(desk, decision, order, "Fade the spike.", "risk")
    out = lessons.settle(item.id, fills=[fill("S-1", "SELL", 4, 100.0, "2026-11-02 15:00:00", 0.0), fill("C-1", "BUY", 1, 90.0, "2026-11-03 15:00:00", 0.0)],
                         mark_price=95.0, mark_at="2026-11-09 15:00:00", db=desks_db).outcome
    assert out["direction"] == "short" and out["exit_basis"] == "mixed"
    assert out["exit_price"] == pytest.approx((1 * 90 + 3 * 95) / 4) and out["return"] == pytest.approx((100 - 93.75) / 100)


def test_several_entry_fills_are_averaged_TA_45(desks_db):
    desk, decision = make_desk(desks_db)["id"], make_decision(desks_db)
    item = lessons.propose(desk, decision, make_order(desks_db, desk, decision_id=decision, broker_order_id="P-1"), "Scale in.", "risk")
    out = lessons.settle(item.id, fills=[fill("P-1", "BUY", 1, 100.0, "2026-11-02 15:00:01"), fill("P-1", "BUY", 3, 104.0, "2026-11-02 15:00:00"),
                                         fill("X", "SELL", 4, 110.0, "2026-11-05 15:00:00")], db=desks_db).outcome
    assert out["entry_price"] == pytest.approx(103.0) and out["entry_qty"] == 4 and out["return"] == pytest.approx(7 / 103.0)


def test_an_order_with_no_fill_is_not_settled_TA_45(lesson):
    db, desk, item = lesson
    for fills in ([], [fill("SOMEONE-ELSE", "BUY", 1, 100.0)]):
        with pytest.raises(lessons.NotExecuted):
            lessons.settle(item.id, fills=fills, mark_price=100.0, db=db)
    assert lessons.store.get(item.id, db=db).status == "proposed"


def test_a_lesson_whose_order_stopped_being_executed_is_not_settled_TA_45(desks_db):
    desk, decision = make_desk(desks_db)["id"], make_decision(desks_db)
    order = make_order(desks_db, desk, decision_id=decision)
    item = lessons.propose(desk, decision, order, "t", "risk")
    with desks_db.write() as u:
        u.session.execute("UPDATE broker_orders SET rule_result = 'refused' WHERE id = :i", {"i": order})
    with pytest.raises(lessons.NotExecuted):
        lessons.settle(item.id, fills=[fill("ORD-1")], mark_price=100, db=desks_db)


def test_an_open_position_without_a_mark_is_not_ready_TA_45(lesson):
    db, desk, item = lesson
    with pytest.raises(lessons.NotReady):
        lessons.settle(item.id, fills=[fill("ORD-1")], db=db)
    assert lessons.store.get(item.id, db=db).outcome is None


def test_the_window_must_have_traded_TA_45(lesson):
    db, desk, item = lesson
    entry = fill("ORD-1", at="2026-11-02 15:00:00")
    with pytest.raises(lessons.NotReady, match="5 days"):
        lessons.settle(item.id, fills=[entry], mark_price=21500.0, mark_at="2026-11-05 15:00:00", horizon_days=5, db=db)
    with pytest.raises(lessons.NotReady, match="date of the mark"):
        lessons.settle(item.id, fills=[entry], mark_price=21500.0, horizon_days=5, db=db)
    done = lessons.settle(item.id, fills=[entry], mark_price=21500.0, mark_at="2026-11-07 15:00:00", horizon_days=5, db=db)
    assert done.status == "settled" and done.outcome["exit_basis"] == "mark"


def test_benchmark_alpha_and_a_given_signal_price_TA_45(lesson):
    db, desk, item = lesson
    out = lessons.settle(item.id, fills=[fill("ORD-1", price=100.0, qty=1), fill("X", "SELL", 1, 103.0, "2026-11-03 15:00:00")],
                         benchmark_return=0.01, signal_price=99.0, db=db).outcome
    assert out["alpha"] == pytest.approx(0.03 - 0.01) and out["signal_price"] == 99.0 and out["entry_slippage"] == pytest.approx(1 / 99.0)


def test_settling_twice_never_measures_again_TA_45(lesson):
    db, desk, item = lesson
    first = lessons.settle(item.id, fills=[fill("ORD-1", price=100.0), fill("X", "SELL", 1, 110.0, "2026-11-03 15:00:00")], db=db)
    again = lessons.settle(item.id, fills=[fill("ORD-1", price=100.0), fill("X", "SELL", 1, 50.0, "2026-11-03 15:00:00")], db=db)
    assert again.outcome == first.outcome and again.settled_at == first.settled_at


@pytest.mark.parametrize("bad", [dict(price=0.0), dict(price=-5.0), dict(price=float("nan")), dict(qty=0.0), dict(side="hold")])
def test_a_bad_fill_is_an_error_not_a_result_TA_45(lesson, bad):
    db, desk, item = lesson
    with pytest.raises(lessons.LessonError):
        lessons.settle(item.id, fills=[fill("ORD-1", **bad)], mark_price=100.0, db=db)


def test_a_retired_or_unknown_lesson_is_not_settled_TA_45(lesson):
    db, desk, item = lesson
    with pytest.raises(lessons.UnknownLesson):
        lessons.settle(999, fills=[], db=db)
    lessons.retire(item.id, db=db)
    with pytest.raises(lessons.LessonError, match="retired"):
        lessons.settle(item.id, fills=[fill("ORD-1")], mark_price=1.0, db=db)


# ---- promotion ------------------------------------------------------------------------------------------------------

def settled(db):
    desk, decision = make_desk(db)["id"], make_decision(db)
    item = lessons.propose(desk, decision, make_order(db, desk, decision_id=decision), "General truth", "risk")
    lessons.settle(item.id, fills=[fill("ORD-1", price=100.0), fill("X", "SELL", 1, 101.0, "2026-11-03 15:00:00")], db=db)
    return desk, item.id


@pytest.mark.parametrize("approver", ["owner", "hokma"])
def test_the_owner_or_the_manager_promotes_a_settled_lesson_J4(desks_db, approver):
    desk, lesson_id = settled(desks_db)
    done = lessons.promote(lesson_id, approved_by=approver, now="2026-11-10 10:00:00", db=desks_db)
    assert done.status == "promoted" and done.scope == "general" and done.approved_by == approver and done.promoted_at == "2026-11-10 10:00:00"
    assert lessons.promote(lesson_id, approved_by=approver, db=desks_db).promoted_at == "2026-11-10 10:00:00"          # twice changes nothing


@pytest.mark.parametrize("approver", ["risk", "desk-chief", "iris", "Owner", "   "])
def test_nobody_else_can_promote_J4(desks_db, approver):
    desk, lesson_id = settled(desks_db)
    with pytest.raises(lessons.LessonError):
        lessons.promote(lesson_id, approved_by=approver, db=desks_db)
    assert lessons.store.get(lesson_id, db=desks_db).status == "settled"


def test_only_a_settled_lesson_can_be_promoted_J4(desks_db):
    desk, decision = make_desk(desks_db)["id"], make_decision(desks_db)
    item = lessons.propose(desk, decision, make_order(desks_db, desk, decision_id=decision), "t", "risk")
    with pytest.raises(lessons.NotPromotable, match="settled"):
        lessons.promote(item.id, approved_by="owner", db=desks_db)
    with pytest.raises(lessons.UnknownLesson):
        lessons.promote(404, approved_by="owner", db=desks_db)
    assert lessons.promote.__name__ == "promote"


def test_a_custom_manager_slug_is_honoured_J4(desks_db):
    desk, lesson_id = settled(desks_db)
    with pytest.raises(lessons.NotPromotable):
        lessons.promote(lesson_id, approved_by="hokma", manager="abe", db=desks_db)
    assert lessons.promote(lesson_id, approved_by="abe", manager="abe", db=desks_db).status == "promoted"
