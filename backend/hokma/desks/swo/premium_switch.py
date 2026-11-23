"""The premium selling switch seen from the SWO desk (SPEC-019: ECT-042, ECT-098, spec 4.5).

The code control lives in `hokma.exec.guards.premium_selling` and decides at the filter before an order. This module is what
the desk reads: whether the switch is open for an underlying, which structures the desk may offer, and the sentence for
the owner. It only reads. It cannot open the switch: there is no function here, in the API or in a tool that writes
the table of calibrated thresholds; only the backtest of H4 does, and opening it by hand is a decision of the owner.

While the switch is closed the desk offers purchases and debit spreads only. The structures that sell premium stay built and
tested, and the filter refuses them if they are proposed anyway.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from hokma.db import Database
from hokma.exec.guards.premium_selling import SwitchState, switch_state
from hokma.i18n import t

PURCHASE_STRUCTURES: tuple[str, ...] = ("long_call", "long_put", "debit_call_spread", "debit_put_spread")
SELLING_STRUCTURES: tuple[str, ...] = ("short_put", "short_call", "credit_call_spread", "credit_put_spread", "iron_condor",
                                       "short_straddle", "short_strangle", "covered_call", "cash_secured_put")
ALL_STRUCTURES: tuple[str, ...] = PURCHASE_STRUCTURES + SELLING_STRUCTURES


class PremiumSwitchClosed(Exception):
    """A structure that sells premium was asked for while the switch of the desk and underlying is closed."""

    def __init__(self, structure: str, underlying: str) -> None:
        self.structure, self.underlying = structure, underlying
        super().__init__(owner_text(underlying))


def owner_text(underlying: str) -> str:
    """The reason in the language of the owner."""
    return t("exec.why.premium_selling_closed", underlying=underlying)


def is_selling_structure(structure: str) -> bool:
    """True for a structure that sells premium. An unknown name raises ValueError: a name the desk does not know is never offered."""
    if structure in SELLING_STRUCTURES:
        return True
    if structure in PURCHASE_STRUCTURES:
        return False
    raise ValueError(f"unknown structure: {str(structure)[:40]}")


def state(desk_id: int, underlying: str, *, db: Database | None = None) -> SwitchState:
    """The switch of a desk and an underlying. Closed on any error."""
    return switch_state(desk_id, underlying, db=db)


def selling_open(desk_id: int, underlying: str, *, db: Database | None = None) -> bool:
    """The flag `premium_selling_open` that `options.structure_matrix` takes. It is True only for a calibrated threshold."""
    return state(desk_id, underlying, db=db).is_open


def allowed_structures(desk_id: int, underlying: str, *, db: Database | None = None) -> tuple[str, ...]:
    """The structures the desk may offer for an underlying: purchases and debit spreads, plus the selling ones only when the switch is open."""
    return ALL_STRUCTURES if selling_open(desk_id, underlying, db=db) else PURCHASE_STRUCTURES


def require_offerable(structure: str, desk_id: int, underlying: str, *, db: Database | None = None) -> None:
    """Raise PremiumSwitchClosed when the structure sells premium and the switch is closed. The filter checks again at send time."""
    if is_selling_structure(structure) and not selling_open(desk_id, underlying, db=db):
        raise PremiumSwitchClosed(structure, underlying)


def describe(desk_id: int, underlyings: Iterable[str], *, db: Database | None = None) -> list[dict[str, Any]]:
    """One row per underlying for the chat and the desk view: the state, the threshold when open and the owner sentence when closed."""
    rows = []
    for name in underlyings:
        s = state(desk_id, name, db=db)
        rows.append({"underlying": s.underlying, "state": s.state, "metric": s.metric,
                     "threshold": None if s.threshold is None else str(s.threshold), "backtest_run_ref": s.backtest_run_ref,
                     "reason": s.reason, "text": None if s.is_open else owner_text(s.underlying)})
    return rows


def matrix_arguments(desk_id: int, underlying: str, direction: str, vol_view: str, *, db: Database | None = None) -> dict[str, Any]:
    """The arguments of `options.structure_matrix` with the switch read from the table, never taken from an agent."""
    return {"direction": direction, "vol_view": vol_view, "premium_selling_open": selling_open(desk_id, underlying, db=db)}


def filter_structures(names: Sequence[str], desk_id: int, underlying: str, *, db: Database | None = None) -> list[str]:
    """The names of a list that the desk may offer now, in the same order."""
    allowed = set(allowed_structures(desk_id, underlying, db=db))
    return [n for n in names if n in allowed]
