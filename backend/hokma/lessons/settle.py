"""Deferred settlement of a lesson (SPEC-017: TA-45).

The outcome of a decision is measured from the price the order really filled at, never from the price of the signal, and only
for an order that executed. It waits until the window has traded: with no closing fill and no mark price the lesson stays
`proposed` and `settle` raises NotReady, so a partial window is never recorded as a result. The line of the desk fixes the
horizon (`horizon_days`); this module only enforces that it has passed.

The entry is the fill(s) of the order's own broker order id. A long return is (exit - entry) / entry, a short one the opposite.
Costs are the commissions of the fills used. `entry_slippage` is adverse when positive (paid more than the signal on a buy, sold for less
on a sell). The outcome is stored as JSON and the lesson becomes `settled`.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Sequence

from hokma.broker.types import Fill
from hokma.db import Database, from_db_time, get_database
from hokma.db.timeutil import to_db_time, utcnow
from hokma.lessons.store import Lesson, LessonError, NotExecuted, NotReady, UnknownLesson, executed_order


@dataclass(frozen=True)
class _Leg:
    qty: float
    price: float
    commission: float
    at: str


def _side_sign(side: str) -> int:
    text = str(side).strip().lower()
    if text.startswith("buy") or text in ("long", "b"):
        return 1
    if text.startswith("sell") or text.startswith("short") or text == "s":
        return -1
    raise LessonError(f"unknown fill side: {side!r}")


def _vwap(legs: Sequence[_Leg]) -> float:
    total = sum(leg.qty for leg in legs)
    return sum(leg.qty * leg.price for leg in legs) / total


def _positive(value: float, what: str) -> float:
    if value is None or not math.isfinite(float(value)) or float(value) <= 0:
        raise LessonError(f"{what} must be a positive finite number")
    return float(value)


def _signal_price(order: dict[str, Any]) -> float | None:
    """The price the signal asked for, read from the masked request of the order (a limit or stop price), if any."""
    try:
        request = json.loads(order["request_json"]) if order.get("request_json") else {}
    except ValueError:
        return None
    if not isinstance(request, dict):
        return None
    for key in ("signal_price", "price", "limit_price", "stopPrice"):
        value = request.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
            return float(value)
    return None


def measure(order: dict[str, Any], fills: Sequence[Fill], *, mark_price: float | None = None, mark_at: str | None = None,
            horizon_days: float | None = None, multiplier: float = 1.0, benchmark_return: float | None = None,
            signal_price: float | None = None) -> tuple[dict[str, Any], str]:
    """(outcome, measured_from) for an executed order. Pure: it reads `order` and `fills`, writes nothing.

    Raises NotExecuted when none of the fills belongs to the order (the order never filled), NotReady when the window has not
    traded."""
    broker_id = str(order["broker_order_id"])
    entry_fills = [f for f in fills if f.order_id is not None and str(f.order_id) == broker_id]
    if not entry_fills:
        raise NotExecuted(f"order {order['id']} has no fill: it did not execute")
    by_symbol: dict[str, list[Fill]] = {}
    for f in entry_fills:
        by_symbol.setdefault(f.symbol, []).append(f)
    if len(by_symbol) > 1:
        return _measure_structure(order, fills, by_symbol, multiplier=multiplier, signal_price=signal_price)
    direction = _side_sign(entry_fills[0].side)
    if any(_side_sign(f.side) != direction for f in entry_fills):
        raise LessonError("the fills of one order have different sides")
    symbol = entry_fills[0].symbol
    entry = [_Leg(_positive(f.qty, "fill quantity"), _positive(f.price, "fill price"), float(f.commission or 0.0), str(f.at))
             for f in entry_fills]
    entry.sort(key=lambda leg: from_db_time(leg.at))
    entry_qty, entry_price = sum(leg.qty for leg in entry), _vwap(entry)
    measured_from = to_db_time(entry[0].at) or entry[0].at
    first_at = from_db_time(entry[0].at)

    later = [f for f in fills if f.order_id is None or str(f.order_id) != broker_id]
    closing = sorted((f for f in later if f.symbol == symbol and _side_sign(f.side) == -direction
                      and from_db_time(str(f.at)) > first_at), key=lambda f: from_db_time(str(f.at)))
    closed_legs: list[_Leg] = []
    remaining = entry_qty
    for f in closing:
        if remaining <= 1e-12:
            break
        take = min(remaining, _positive(f.qty, "fill quantity"))
        closed_legs.append(_Leg(take, _positive(f.price, "fill price"), float(f.commission or 0.0) * take / f.qty, str(f.at)))
        remaining -= take
    mark_leg = None
    if remaining > 1e-12:
        if mark_price is None:
            raise NotReady("the position is not closed and there is no mark price yet")
        if horizon_days is not None:
            if mark_at is None:
                raise NotReady("a horizon needs the date of the mark price")
            if from_db_time(mark_at) < first_at + timedelta(days=horizon_days):
                raise NotReady(f"the window of {horizon_days:g} days has not traded yet")
        mark_leg = _Leg(remaining, _positive(mark_price, "mark price"), 0.0, mark_at or utcnow())
    legs = closed_legs + ([mark_leg] if mark_leg else [])
    exit_price = _vwap(legs)
    basis = "mixed" if closed_legs and mark_leg else ("mark" if mark_leg else "closing_fills")
    measured_to = max((from_db_time(leg.at) for leg in legs), default=first_at)
    gross = (exit_price - entry_price) * direction
    ret = gross / entry_price
    mult = _positive(multiplier, "multiplier")
    costs = sum(leg.commission for leg in entry) + sum(leg.commission for leg in closed_legs)
    pnl = gross * entry_qty * mult - costs
    reference = signal_price if signal_price is not None else _signal_price(order)
    outcome: dict[str, Any] = {
        "symbol": symbol, "direction": "long" if direction > 0 else "short", "entry_price": entry_price, "entry_qty": entry_qty,
        "exit_price": exit_price, "exit_basis": basis, "return": ret, "pnl": pnl, "costs": costs, "multiplier": mult,
        "signal_price": reference,
        "entry_slippage": None if reference is None else (entry_price - reference) / reference * direction,
        "benchmark_return": benchmark_return, "alpha": None if benchmark_return is None else ret - benchmark_return,
        "holding_days": (measured_to - first_at).total_seconds() / 86400.0,
        "measured_to": measured_to.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return outcome, measured_from


def _measure_structure(order: dict[str, Any], fills: Sequence[Fill], by_symbol: dict[str, list[Fill]], *, multiplier: float,
                       signal_price: float | None) -> tuple[dict[str, Any], str]:
    """A structure of several legs (a spread, a condor) is measured by the NET of its legs, each leg from its own entry and closing fills.

    Every leg has to be closed by fills: the cycle hands over one mark price, and a mark of one symbol says nothing of the others, so a
    leg that is still open leaves the lesson pending (NotReady). Entry and exit are the net value per unit (a debit is positive, a credit
    negative); the return is the profit over the net amount put in or taken in."""
    legs: list[tuple[dict[str, Any], str]] = []
    for symbol, entry in by_symbol.items():
        mine = list(entry) + [f for f in fills if f.symbol == symbol and (f.order_id is None or str(f.order_id) != str(order["broker_order_id"]))]
        try:
            legs.append(measure(order, mine, multiplier=multiplier))
        except NotReady as exc:
            raise NotReady(f"the structure of {len(by_symbol)} legs is not settled: {symbol} is not closed by fills yet ({exc})") from exc
    units = min(o["entry_qty"] for o, _ in legs)
    sign = lambda o: 1 if o["direction"] == "long" else -1   # noqa: E731
    entry_net = sum(sign(o) * o["entry_price"] * o["entry_qty"] for o, _ in legs) / units
    exit_net = sum(sign(o) * o["exit_price"] * o["entry_qty"] for o, _ in legs) / units
    if entry_net == 0:
        raise LessonError("a structure whose legs net to zero at entry has no amount to measure a return against")
    pnl, costs = sum(o["pnl"] for o, _ in legs), sum(o["costs"] for o, _ in legs)
    reference = signal_price if signal_price is not None else _signal_price(order)
    first = min(legs, key=lambda x: from_db_time(x[1]))[1]
    measured_to = max(from_db_time(o["measured_to"]) for o, _ in legs)
    outcome: dict[str, Any] = {
        "symbol": "+".join(sorted(by_symbol)), "direction": "long" if entry_net > 0 else "short", "structure": "debit" if entry_net > 0 else "credit",
        "legs": [{"symbol": o["symbol"], "direction": o["direction"], "qty": o["entry_qty"], "entry_price": o["entry_price"], "exit_price": o["exit_price"],
                  "pnl": o["pnl"]} for o, _ in legs],
        "entry_price": abs(entry_net), "entry_qty": units, "exit_price": abs(exit_net), "exit_basis": "closing_fills",
        "return": (exit_net - entry_net) / abs(entry_net), "pnl": pnl, "costs": costs, "multiplier": multiplier, "signal_price": reference,
        "entry_slippage": None, "benchmark_return": None, "alpha": None,
        "holding_days": (measured_to - from_db_time(first)).total_seconds() / 86400.0,
        "measured_to": measured_to.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return outcome, first


def settle(lesson_id: int, *, fills: Sequence[Fill], mark_price: float | None = None, mark_at: str | None = None,
           horizon_days: float | None = None, multiplier: float = 1.0, benchmark_return: float | None = None,
           signal_price: float | None = None, now: str | None = None, db: Database | None = None) -> Lesson:
    """Measure the outcome of a lesson from the real entry fill and mark it `settled`.

    `fills` are the broker's fills (the entry fill carries the order's own broker order id; later opposite fills close it).
    A settled lesson is returned as it is: its outcome is never measured twice. An order with no fill raises NotExecuted;
    a window that has not traded raises NotReady and leaves the lesson as it was."""
    with (db or get_database()).write() as u:
        s = u.session
        row = s.one("SELECT * FROM lessons WHERE id = :i", {"i": lesson_id})
        if row is None:
            raise UnknownLesson(f"lesson {lesson_id} does not exist")
        if row["status"] in ("settled", "promoted"):
            return Lesson.from_row(row)
        if row["status"] == "retired":
            raise LessonError(f"lesson {lesson_id} is retired")
        order = executed_order(s, int(row["order_id"]), int(row["desk_id"]) if row.get("desk_id") is not None else -1)
        outcome, measured_from = measure(order, fills, mark_price=mark_price, mark_at=mark_at, horizon_days=horizon_days,
                                         multiplier=multiplier, benchmark_return=benchmark_return, signal_price=signal_price)
        s.update("lessons", {"id": lesson_id}, {"outcome_json": json.dumps(outcome, separators=(",", ":")),
                                                "measured_from": measured_from, "status": "settled",
                                                "settled_at": to_db_time(now) if now else utcnow()})
        return Lesson.from_row(s.one("SELECT * FROM lessons WHERE id = :i", {"i": lesson_id}))
