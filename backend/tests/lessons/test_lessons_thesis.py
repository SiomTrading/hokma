"""CTS-023: life cycle of a thesis: open, confirmed, invalidated, closed; valid transitions only; every change is dated."""
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures)

from hokma.db.errors import IntegrityViolation
from hokma.lessons import thesis


@pytest.fixture
def desk(desks_db):
    return desks_db, make_desk(desks_db)["id"]


def test_the_happy_path_is_open_confirmed_closed_and_each_move_is_dated_CTS_023(desk):
    db, desk_id = desk
    clock = ["2026-11-02 14:00:00", "2026-11-03 15:30:00", "2026-11-06 20:00:00"]
    for state, now in zip(("open", "confirmed", "closed"), clock):
        event = thesis.record(desk_id, "NQZ6", state, f"moved to {state}", now=now, db=db)
        assert event.state == state and event.created_at == now and event.note == f"moved to {state}" and event.symbol == "NQZ6"
    assert [(e.state, e.created_at) for e in thesis.history(desk_id, "NQZ6", db=db)] == list(zip(("open", "confirmed", "closed"), clock))
    assert thesis.current_state(desk_id, "NQZ6", db=db) == "closed" and thesis.open_theses(desk_id, db=db) == {}


@pytest.mark.parametrize("path", [("open", "confirmed", "invalidated", "closed"), ("open", "invalidated", "closed"), ("open", "closed"),
                                  ("open", "confirmed", "closed"), ("open", "invalidated")])
def test_every_valid_path_is_accepted_CTS_023(desk, path):
    db, desk_id = desk
    for state in path:
        thesis.record(desk_id, "ES", state, db=db)
    assert thesis.current_state(desk_id, "ES", db=db) == path[-1]


@pytest.mark.parametrize("before,bad", [
    (None, "confirmed"), (None, "invalidated"), (None, "closed"), ("open", "open"), ("confirmed", "open"), ("confirmed", "confirmed"),
    ("invalidated", "confirmed"), ("invalidated", "open"), ("closed", "confirmed"), ("closed", "invalidated"), ("closed", "closed"),
])
def test_an_invalid_transition_raises_and_writes_nothing_CTS_023(desk, before, bad):
    db, desk_id = desk
    walk = {None: (), "open": ("open",), "confirmed": ("open", "confirmed"), "invalidated": ("open", "invalidated"),
            "closed": ("open", "closed")}[before]
    for state in walk:
        thesis.record(desk_id, "CL", state, db=db)
    with pytest.raises(thesis.InvalidTransition):
        thesis.record(desk_id, "CL", bad, db=db)
    assert thesis.current_state(desk_id, "CL", db=db) == before and len(thesis.history(desk_id, "CL", db=db)) == len(walk)


def test_a_closed_symbol_can_start_a_new_thesis_CTS_023(desk):
    db, desk_id = desk
    for state in ("open", "closed", "open", "confirmed"):
        thesis.record(desk_id, "gc", state, db=db)
    assert thesis.current_state(desk_id, "GC", db=db) == "confirmed" and len(thesis.history(desk_id, "GC", db=db)) == 4


def test_symbols_and_desks_are_independent_CTS_023(desks_db):
    one, two = make_desk(desks_db, name="One")["id"], make_desk(desks_db, name="Two")["id"]
    thesis.record(one, "NQ", "open", db=desks_db)
    thesis.record(one, "ES", "open", db=desks_db)
    thesis.record(one, "ES", "confirmed", db=desks_db)
    thesis.record(two, "NQ", "open", db=desks_db)
    thesis.record(two, "NQ", "closed", db=desks_db)
    assert thesis.open_theses(one, db=desks_db) == {"NQ": "open", "ES": "confirmed"} and thesis.open_theses(two, db=desks_db) == {}
    assert thesis.current_state(two, "ES", db=desks_db) is None


@pytest.mark.parametrize("symbol", ["", "   ", "A B", "x" * 41, "tab\tx"])
def test_a_bad_symbol_is_refused_CTS_023(desk, symbol):
    db, desk_id = desk
    with pytest.raises(thesis.ThesisError):
        thesis.record(desk_id, symbol, "open", db=db)


def test_futures_symbols_with_a_slash_are_accepted_and_normalised_CTS_023(desk):
    db, desk_id = desk
    assert thesis.record(desk_id, " /nqz26 ", "open", db=db).symbol == "/NQZ26"


def test_unknown_state_note_too_long_and_missing_desk_CTS_023(desk):
    db, desk_id = desk
    with pytest.raises(thesis.ThesisError, match="unknown state"):
        thesis.record(desk_id, "NQ", "IDEA", db=db)
    with pytest.raises(thesis.ThesisError, match="note"):
        thesis.record(desk_id, "NQ", "open", "n" * 4001, db=db)
    with pytest.raises(thesis.ThesisError, match="desk 999"):
        thesis.record(999, "NQ", "open", db=db)


def test_the_table_refuses_a_state_outside_the_cycle_CTS_023(desk):
    db, desk_id = desk
    with pytest.raises(IntegrityViolation):
        with db.write() as u:
            u.session.insert("thesis_events", {"desk_id": desk_id, "symbol": "NQ", "state": "IDEA"})
