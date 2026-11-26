"""Execution (SPEC-018, H2): the one road from a proposal to an order. The public door is `hokma.exec.submit.submit`; this
package exports it with `approve`, `reject`, `flatten`, `status` and `set_level`.

Imports are lazy: `hokma.broker.service` imports `hokma.exec.submit` for its send, and `hokma.exec` imports the service for the
owner check, so nothing here loads the other at import time."""
from __future__ import annotations

from typing import Any

from hokma.core import processguard

processguard.require(__name__)          # runtime limit: only the Hokma server loads this package (core/processguard.py)

__all__ = ["ENGINE_STATUS", "NoTranslation", "approve", "flatten", "from_engine_state", "reject", "set_level", "status", "submit"]

# N-18 (SPEC-020): the states of an order of the backtest engine, each translated to the `Status` of `ExecResult`. A state of the engine with no
# line here is refused (`no_translation`), so a backtest never reports an order state that live execution cannot say.
ENGINE_STATUS: dict[str, str] = {
    "initialized": "sent", "emulated": "sent", "released": "sent", "submitted": "sent", "accepted": "sent", "triggered": "sent",
    "pending_update": "sent", "pending_cancel": "sent", "open": "sent",
    "partially_filled": "partially_filled", "filled": "filled",
    "denied": "refused", "rejected": "rejected_by_broker", "canceled": "canceled", "expired": "expired",
}


class NoTranslation(ValueError):
    """An order state of the backtest engine that has no state of `ExecResult`. `code` is stable (no_translation)."""

    code = "no_translation"


def from_engine_state(state: str) -> str:
    """The `ExecResult` status of an order state of the backtest engine."""
    try:
        return ENGINE_STATUS[state]
    except (KeyError, TypeError):
        raise NoTranslation(f"the engine order state {str(state)[:40]!r} has no state in ExecResult") from None


# `hokma.exec.submit` is the module of the door AND callable (the module class, at the end of submit.py, calls its `submit` function):
# `hokma.exec.submit(proposal, who=...)` and `from hokma.exec.submit import dispatch` both work. It is not defined here on purpose:
# a function with that name would be hidden by the module the first time something imports it.

def approve(proposal_id: int, *, who: Any, db: Any = None) -> Any:
    from hokma.exec import approvals

    return approvals.approve(proposal_id, who=who, db=db)


def reject(proposal_id: int, *, who: Any, db: Any = None) -> Any:
    from hokma.exec import approvals

    return approvals.reject(proposal_id, who=who, db=db)


def flatten(desk_id: int, *, reason: str, who: Any, db: Any = None) -> Any:
    from hokma.exec.submit import flatten as door_flatten

    return door_flatten(desk_id, reason=reason, who=who, db=db)


def status(proposal_id: int | None = None, *, db: Any = None, engine_state: str | None = None) -> Any:
    """The status of a proposal, or with `engine_state` the `ExecResult` status that an order state of the backtest engine translates to."""
    if engine_state is not None:
        if proposal_id is not None:
            raise ValueError("give a proposal id or an engine state, not both")
        return from_engine_state(engine_state)
    if proposal_id is None:
        raise ValueError("give a proposal id or an engine state")
    from hokma.exec.submit import status as door_status

    return door_status(proposal_id, db=db)


def set_level(desk_id: int, level: int, *, rules: dict | None = None, who: Any, db: Any = None) -> dict:
    from hokma.exec import levels

    return levels.set_level(desk_id, level, rules=rules, who=who, db=db)
