"""TA-47: point-in-time reading: nothing created after as_of, nothing of another desk, general only when promoted."""
from datetime import UTC, datetime

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures)
from lessons.support import fill, make_decision, make_order

import hokma.lessons as lessons


def add(db, desk, text, *, created="2026-11-01 10:00:00", settle=None, promote=None, broker_id=None):
    """A lesson with chosen dates (the clock of the tests, not the wall clock)."""
    decision = make_decision(db, text)
    order = make_order(db, desk, decision_id=decision, broker_order_id=broker_id or f"B-{text}")
    item = lessons.propose(desk, decision, order, text, "risk")
    with db.write() as u:
        u.session.update("lessons", {"id": item.id}, {"created_at": created})
    if settle:
        lessons.settle(item.id, fills=[fill(broker_id or f"B-{text}", price=100.0, at=created), fill("X" + text, "SELL", 1, 102.0, settle)],
                       now=settle, db=db)
    if promote:
        lessons.promote(item.id, approved_by="owner", now=promote, db=db)
    return item.id


@pytest.fixture
def two(desks_db):
    return desks_db, make_desk(desks_db, name="One")["id"], make_desk(desks_db, name="Two")["id"]


def texts(found):
    return [lesson.text for lesson in found]


def test_a_lesson_created_after_as_of_is_excluded_TA_47(two):
    db, one, _ = two
    add(db, one, "early", created="2026-11-01 10:00:00")
    add(db, one, "late", created="2026-11-20 10:00:00")
    assert texts(lessons.for_desk(one, as_of="2026-11-10 00:00:00", db=db)) == ["early"]
    assert texts(lessons.for_desk(one, as_of=datetime(2026, 11, 21, tzinfo=UTC), db=db)) == ["early", "late"]
    assert lessons.for_desk(one, as_of="2026-10-01 00:00:00", db=db) == []


def test_a_lesson_of_another_desk_never_appears_TA_47(two):
    db, one, other = two
    add(db, other, "theirs")
    add(db, one, "mine")
    assert texts(lessons.for_desk(one, as_of="2026-12-01 00:00:00", db=db)) == ["mine"]
    assert texts(lessons.for_desk(other, as_of="2026-12-01 00:00:00", db=db)) == ["theirs"]


def test_general_lessons_appear_only_when_promoted_TA_47(two):
    db, one, other = two
    add(db, other, "settled but not promoted", settle="2026-11-03 10:00:00")
    add(db, other, "promoted", settle="2026-11-03 10:00:00", promote="2026-11-05 10:00:00")
    found = lessons.for_desk(one, as_of="2026-12-01 00:00:00", db=db)
    assert texts(found) == ["promoted"] and found[0].scope == "general" and found[0].status == "promoted"


def test_a_promotion_made_after_as_of_is_not_visible_yet_TA_47(two):
    db, one, other = two
    add(db, other, "later promoted", settle="2026-11-03 10:00:00", promote="2026-11-15 10:00:00")
    assert lessons.for_desk(one, as_of="2026-11-10 00:00:00", db=db) == []
    mine = add(db, one, "my own later promoted", settle="2026-11-03 10:00:00", promote="2026-11-15 10:00:00")
    own = lessons.for_desk(one, as_of="2026-11-10 00:00:00", db=db)
    assert [(x.id, x.scope, x.status) for x in own] == [(mine, "desk", "settled")] and own[0].approved_by is None and own[0].outcome is not None


def test_an_outcome_measured_after_as_of_is_hidden_TA_47(two):
    db, one, _ = two
    add(db, one, "result comes later", created="2026-11-01 10:00:00", settle="2026-11-12 10:00:00")
    before = lessons.for_desk(one, as_of="2026-11-05 00:00:00", db=db)[0]
    assert before.text == "result comes later" and before.outcome is None and before.status == "proposed" and before.settled_at is None
    after = lessons.for_desk(one, as_of="2026-11-13 00:00:00", db=db)[0]
    assert after.status == "settled" and after.outcome["return"] == pytest.approx(0.02)


def test_retired_lessons_are_not_read_and_limit_keeps_the_latest_TA_47(two):
    db, one, _ = two
    ids = [add(db, one, f"l{i}", created=f"2026-11-0{i + 1} 10:00:00") for i in range(4)]
    lessons.retire(ids[0], db=db)
    assert texts(lessons.for_desk(one, as_of="2026-12-01 00:00:00", db=db)) == ["l1", "l2", "l3"]
    assert texts(lessons.for_desk(one, as_of="2026-12-01 00:00:00", limit=2, db=db)) == ["l2", "l3"]


def test_as_of_is_required_TA_47(two):
    db, one, _ = two
    with pytest.raises(ValueError):
        lessons.for_desk(one, as_of=None, db=db)
