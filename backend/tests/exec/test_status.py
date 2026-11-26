"""N-18: every order state of the backtest engine translates to a state of ExecResult (SPEC-020)."""
import typing

import pytest
from exec_support import db, desks_db, env, proposal, ready  # noqa: F401  (fixtures)

from hokma import exec as hexec
from hokma.backtest import fills
from hokma.exec import ENGINE_STATUS, NoTranslation, from_engine_state
from hokma.exec.submit import Status

ENGINE_STATES = typing.get_args(typing.get_type_hints(fills.Resolution)["status"])
# the lifecycle states of an order that the engine passes through before the backtest reads its outcome
LIFECYCLE = ("initialized", "emulated", "released", "submitted", "accepted", "triggered", "pending_update", "pending_cancel", "partially_filled",
             "canceled", "expired", "denied")


def test_the_states_the_backtest_reads_back_are_known():
    assert set(ENGINE_STATES) == {"filled", "open", "rejected"}                  # if the backtest adds a state, this and the next test fail until it is translated


@pytest.mark.parametrize("state", sorted(set(ENGINE_STATES) | set(LIFECYCLE)))
def test_every_engine_state_has_a_state_of_exec_result(state):
    translated = from_engine_state(state)
    assert translated in typing.get_args(Status)
    assert hexec.status(engine_state=state) == translated


def test_the_translation_of_each_state():
    assert {s: from_engine_state(s) for s in ("filled", "open", "rejected", "partially_filled", "canceled", "expired", "denied", "accepted")} == {
        "filled": "filled", "open": "sent", "rejected": "rejected_by_broker", "partially_filled": "partially_filled", "canceled": "canceled",
        "expired": "expired", "denied": "refused", "accepted": "sent"}
    assert set(ENGINE_STATUS) >= set(ENGINE_STATES) | set(LIFECYCLE)


def test_a_state_with_no_translation_fails():
    for unknown in ("teleported", "", None, 3):
        with pytest.raises(NoTranslation) as err:
            from_engine_state(unknown)
        assert err.value.code == "no_translation"
    with pytest.raises(NoTranslation):
        hexec.status(engine_state="teleported")


def test_a_new_state_of_the_engine_without_a_line_in_the_table_is_caught():
    missing = [s for s in typing.get_args(typing.get_type_hints(fills.Resolution)["status"]) if s not in ENGINE_STATUS]
    assert missing == []


def test_the_status_of_a_proposal_still_works_and_the_two_ways_are_not_mixed(ready):
    out = hexec.submit(proposal(ready), who=ready.owner, db=ready.db)
    assert hexec.status(out.proposal_id, db=ready.db).fingerprint == out.fingerprint
    with pytest.raises(ValueError):
        hexec.status(out.proposal_id, engine_state="filled", db=ready.db)
    with pytest.raises(ValueError):
        hexec.status()
    assert {"approve", "flatten", "reject", "set_level", "status", "submit"} <= set(hexec.__all__)
