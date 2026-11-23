"""TA-44: the lesson log: migration, propose (needs an executed order), idempotence and no delete path."""
import ast
import pathlib
import sqlite3

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures)
from lessons.support import make_decision, make_order

import hokma.lessons as lessons
from hokma.db import get_database, migrate
from hokma.db.errors import IntegrityViolation
from hokma.lessons import store


@pytest.fixture
def setup(desks_db):
    desk = make_desk(desks_db)["id"]
    decision = make_decision(desks_db)
    order = make_order(desks_db, desk, decision_id=decision)
    return desks_db, desk, decision, order


def test_the_migration_creates_the_two_tables_on_sqlite_TA_44(env):
    migrate()
    from hokma.db.paths import db_path
    con = sqlite3.connect(str(db_path()))
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"lessons", "thesis_events"} <= names
    columns = {r[1] for r in con.execute("PRAGMA table_info(lessons)")}
    assert {"desk_id", "scope", "decision_id", "order_id", "text", "outcome_json", "measured_from", "status", "proposed_by",
            "approved_by", "created_at", "settled_at", "promoted_at"} <= columns
    assert migrate().applied == []


def test_the_migration_also_renders_for_postgres_TA_44():
    from hokma.db.migrate import MIGRATIONS_DIR, render
    from hokma.db.postgres import PostgresBackend

    text = (MIGRATIONS_DIR / "0021_lessons.sql").read_text(encoding="utf-8")
    script = render(text, PostgresBackend("postgresql://u:p@localhost/db"))
    assert "CREATE TABLE lessons" in script and "{PK}" not in script and "{TS}" not in script and "{" not in script


def test_the_transfer_recognises_the_two_new_tables_TA_44():
    from hokma.db import transfer
    from hokma.db.migrate import discover

    plan = [t.name for t in transfer._plan(discover())]
    assert {"lessons", "thesis_events"} <= set(plan)
    assert plan.index("lessons") > max(plan.index("desks"), plan.index("decisions"), plan.index("broker_orders"))
    assert plan.index("thesis_events") > plan.index("desks") and set(transfer.LESSON_TABLES) == {"lessons", "thesis_events"}
    assert next(t for t in transfer._plan(discover()) if t.name == "lessons").identity


def test_propose_with_an_executed_order_creates_a_desk_lesson_TA_44(setup):
    db, desk, decision, order = setup
    lesson = lessons.propose(desk, decision, order, "Wait for the retest before adding.", "risk")
    assert lesson.scope == "desk" and lesson.status == "proposed" and lesson.desk_id == desk and lesson.order_id == order
    assert lesson.decision_id == decision and lesson.proposed_by == "risk" and lesson.outcome is None and lesson.created_at
    assert lesson.id and lesson.approved_by is None and lesson.promoted_at is None


def test_propose_is_idempotent_for_the_same_text_TA_44(setup):
    db, desk, decision, order = setup
    first = lessons.propose(desk, decision, order, "Same lesson", "risk")
    again = lessons.propose(desk, decision, order, "  Same lesson ", "iris")
    other = lessons.propose(desk, decision, order, "A different lesson", "risk")
    assert again.id == first.id and other.id != first.id
    with db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM lessons") == 2


@pytest.mark.parametrize("kwargs", [
    {"action": "preview"}, {"action": "cancel"}, {"rule_result": "refused"}, {"rule_result": "needs_owner"}, {"http_status": 400},
    {"http_status": 500}, {"broker_order_id": None}, {"response": {"error": {"code": "x", "status": 400}}},
])
def test_propose_without_an_executed_order_raises_NotExecuted_TA_44(desks_db, kwargs):
    desk, decision = make_desk(desks_db)["id"], make_decision(desks_db)
    kwargs = {"rule_result": "auto", **kwargs} if "rule_result" not in kwargs else kwargs
    order = make_order(desks_db, desk, decision_id=decision, **kwargs)
    with pytest.raises(lessons.NotExecuted):
        lessons.propose(desk, decision, order, "text", "risk")
    with desks_db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM lessons") == 0


def test_propose_with_a_missing_or_foreign_order_raises_NotExecuted_TA_44(desks_db):
    one, two = make_desk(desks_db, name="One")["id"], make_desk(desks_db, name="Two")["id"]
    decision = make_decision(desks_db)
    foreign = make_order(desks_db, two, decision_id=decision)
    with pytest.raises(lessons.NotExecuted, match="another desk"):
        lessons.propose(one, decision, foreign, "text", "risk")
    with pytest.raises(lessons.NotExecuted, match="does not exist"):
        lessons.propose(one, decision, 9999, "text", "risk")


@pytest.mark.parametrize("text", ["", "   ", "x" * 4001])
def test_a_bad_lesson_text_is_refused_TA_44(setup, text):
    db, desk, decision, order = setup
    with pytest.raises(lessons.LessonError):
        lessons.propose(desk, decision, order, text, "risk")


def test_a_missing_desk_or_decision_is_refused_TA_44(setup):
    db, desk, decision, order = setup
    with pytest.raises(lessons.LessonError, match="desk 999"):
        lessons.propose(999, decision, order, "t", "risk")
    with pytest.raises(lessons.LessonError, match="decision 999"):
        lessons.propose(desk, 999, order, "t", "risk")
    with pytest.raises(lessons.LessonError):
        lessons.propose(desk, decision, order, "t", "")


def test_the_database_enforces_the_shape_TA_44(setup):
    db, desk, decision, order = setup
    base = {"scope": "desk", "desk_id": desk, "decision_id": decision, "order_id": order, "text": "t", "proposed_by": "risk"}
    with db.write() as u:
        u.session.insert("lessons", base)
    for bad in ({"scope": "world"}, {"status": "weird"}, {"desk_id": None}, {"text": ""}, {"text": "x" * 4001}, {"proposed_by": ""},
                {"status": "promoted"}, {"order_id": 987654}):
        with pytest.raises(IntegrityViolation):
            with db.write() as u:
                u.session.insert("lessons", {**base, **bad})


def test_lessons_are_never_deleted_TA_44():
    package = pathlib.Path(lessons.__file__).parent
    names, sql = [], []
    for path in sorted(package.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        sql.append(source.upper())
        names += [n.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)]
    assert not [n for n in names if n.startswith(("delete", "remove", "drop", "purge", "erase"))]
    assert not [s for s in sql if "DELETE FROM LESSONS" in s or "DELETE FROM THESIS_EVENTS" in s or "DROP TABLE" in s]
    assert not [n for n in dir(lessons) if n.startswith(("delete", "remove"))]


def test_a_desk_and_an_order_cannot_be_removed_under_a_lesson_TA_44(setup):
    db, desk, decision, order = setup
    lessons.propose(desk, decision, order, "t", "risk")
    for sql in ("DELETE FROM broker_orders WHERE id = :i", "DELETE FROM desks WHERE id = :d", "DELETE FROM decisions WHERE id = :x"):
        with pytest.raises(IntegrityViolation):
            with db.write() as u:
                u.session.execute(sql, {"i": order, "d": desk, "x": decision})


def test_retiring_a_lesson_keeps_the_row_TA_44(setup):
    db, desk, decision, order = setup
    lesson = lessons.propose(desk, decision, order, "t", "risk")
    assert store.retire(lesson.id).status == "retired" and store.get(lesson.id).status == "retired"
    with pytest.raises(lessons.UnknownLesson):
        store.retire(12345)
    assert get_database() is not None
