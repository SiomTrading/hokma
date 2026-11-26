"""Execution by slices (SPEC-020: L-54): TWAP, VWAP, standard deviation and spread, for SWO (split an options order) and LP (split a big
purchase of shares). A parent order becomes child `OrderProposal` values; every child carries the fingerprint of the parent and goes through the
one door (`hokma.exec.submit`, so through the filter). If the filter, the owner or the broker stops a slice, the next ones are not emitted.

Two parts, kept apart on purpose:
- the pure part (`slice_order`, `parent_of`, `run_slices`) reads no database and calls no broker: the backtest simulates the same slices with it,
  judging every child with the guards of the filter (`hokma.backtest.guards_bridge`);
- the submitting part (`execute`) is the only one that reaches `hokma.exec.submit`, imported when it runs, never when the module loads.

The schedule (when a slice leaves) belongs to the caller: the slices come in order and `execute` sends one after the other, calling `wait` before
every slice but the first. Nothing is invented: the volume profile, the mean and deviation and the quote are arguments.
"""
from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from hokma.exec.money import MoneyError, dec, round_to_tick
from hokma.exec.proposal import OrderProposal
from hokma.quant import limits

ALGOS = ("twap", "vwap", "stdev", "spread")
MAX_SLICES = 100
HALTING = ("refused", "rejected_by_broker", "expired", "canceled")      # a slice that ends like this stops the slices after it
_MARK = re.compile(r"^\[(twap|vwap|stdev|spread) (\d{1,3})/(\d{1,3}) of ([0-9a-f]{16,128})\] ")
_CENT = Decimal("0.01")


class AlgoError(ValueError):
    """The slicing cannot be done as asked. `code` is stable (bad_algo)."""

    code = "bad_algo"


@dataclass(frozen=True)
class SliceOutcome:
    index: int                      # 1 based
    child: OrderProposal
    status: str                     # an `ExecResult` status
    code: str
    result: Any = None              # the `ExecResult` of the door (None in a simulation)


@dataclass(frozen=True)
class AlgoRun:
    parent_fingerprint: str
    algo: str
    planned: int
    outcomes: tuple[SliceOutcome, ...]      # the slices that were emitted, in order
    halted_at: int | None = None            # the 1 based slice that stopped the line, if any
    halt_code: str | None = None

    @property
    def emitted(self) -> int:
        return len(self.outcomes)

    @property
    def complete(self) -> bool:
        return self.halted_at is None and self.emitted == self.planned


# --------------------------------------------------------------------------------------------------------- pure slicing

def _number(value: Any, name: str, *, positive: bool = True) -> Decimal:
    try:
        number = dec(value, name)
    except MoneyError as exc:
        raise AlgoError(str(exc)) from exc
    if positive and number <= 0:
        raise AlgoError(f"{name} must be above zero")
    return number


def _count(value: Any, name: str = "n_slices") -> int:
    try:
        n = limits.as_int(name, value)
    except Exception as exc:                     # BadInput of the limits module
        raise AlgoError(str(exc)) from exc
    if not 1 <= n <= MAX_SLICES:
        raise AlgoError(f"{name} is a whole number from 1 to {MAX_SLICES}")
    return n


def allocate(total: Decimal, weights: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """`total` shared by `weights`, adding up to `total` exactly. A whole total goes in whole units (the largest remainders take the units that are
    left, the earlier slice first on a tie); a total with decimals goes to the exact decimal place of the total, the remainder to the last slice."""
    if not weights or any(w < 0 for w in weights) or sum(weights) <= 0:
        raise AlgoError("the weights are numbers of zero or more with a sum above zero")
    whole = total == total.to_integral_value()
    step = Decimal(1) if whole else Decimal(1).scaleb(total.as_tuple().exponent)
    units = (total / step).to_integral_value()
    weight_sum = sum(weights)
    raw = [units * w / weight_sum for w in weights]
    floors = [r.to_integral_value(rounding=ROUND_DOWN) for r in raw]
    left = int(units - sum(floors))
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[:left]:
        floors[i] += 1
    return tuple(f * step for f in floors)


def _plan(parent: OrderProposal, algo: str, n_slices: int | None, profile: Sequence[Any] | None, mean: Any, std: Any, bands: Sequence[Any] | None,
          bid: Any, ask: Any, tick: Any) -> tuple[list[Decimal], list[Decimal | None]]:
    """(the weight of each slice, the limit price of each slice or None to keep the parent's price)."""
    buy = parent.legs[0].side == "buy"
    step = None if tick is None else _number(tick, "tick")
    if algo == "twap":
        n = _count(n_slices)
        return [Decimal(1)] * n, [None] * n
    if algo == "vwap":
        if not isinstance(profile, (list, tuple)) or not profile:
            raise AlgoError("a vwap needs the volume profile: one volume per slice")
        n = _count(len(profile), "profile")
        volumes = [_number(v, "a volume in the profile", positive=False) for v in profile]
        if any(v < 0 for v in volumes) or sum(volumes) <= 0:
            raise AlgoError("a volume profile has volumes of zero or more and a sum above zero")
        return volumes, [None] * n
    if parent.order_type not in ("market", "limit"):
        raise AlgoError(f"a {algo} slicing works on a market or limit parent, not on a {parent.order_type} order")
    if algo == "stdev":
        centre, spread = _number(mean, "mean"), _number(std, "std")
        if not isinstance(bands, (list, tuple)) or not bands:
            raise AlgoError("a stdev slicing needs its bands: multiples of the deviation")
        n = _count(len(bands), "bands")
        prices = []
        for band in bands:
            k = _number(band, "a band", positive=False)
            if k < 0:
                raise AlgoError("a band is a multiple of the deviation of zero or more")
            price = centre - k * spread if buy else centre + k * spread
            if price <= 0:
                raise AlgoError("a band puts the price at zero or below")
            prices.append(round_to_tick(price, step, direction="down" if buy else "up"))
        return [Decimal(1)] * n, prices
    n = _count(n_slices)                                                       # spread
    low, high = _number(bid, "bid"), _number(ask, "ask")
    if high < low:
        raise AlgoError("the ask is under the bid")
    prices = []
    for i in range(n):
        share = Decimal(1) if n == 1 else Decimal(i) / Decimal(n - 1)           # a buy walks from the bid up to the ask, a sell from the ask down
        price = low + (high - low) * share if buy else high - (high - low) * share
        prices.append(round_to_tick(price, step, direction="down" if buy else "up"))
    return [Decimal(1)] * n, prices


def slice_order(parent: OrderProposal, algo: str, *, n_slices: int | None = None, profile: Sequence[Any] | None = None, mean: Any = None,
                std: Any = None, bands: Sequence[Any] | None = None, bid: Any = None, ask: Any = None, tick: Any = None) -> tuple[OrderProposal, ...]:
    """The child proposals of `parent`, in the order they leave. Each leg of the parent is shared by the same weights, so the legs of a spread keep
    their ratio; every leg adds up to the parent's quantity. A slice that would leave a leg with nothing while another leg has a quantity is not
    possible (a naked leg): it raises. A slice with nothing at all is left out. Prices of `stdev` and `spread` go on the grid of `tick`."""
    if algo not in ALGOS:
        raise AlgoError(f"algo must be one of {', '.join(ALGOS)}")
    if not isinstance(parent, OrderProposal):
        raise AlgoError("the parent is an order proposal")
    weights, prices = _plan(parent, algo, n_slices, profile, mean, std, bands, bid, ask, tick)
    shares = [allocate(leg.qty, weights) for leg in parent.legs]
    n = len(weights)
    kept = [i for i in range(n) if any(s[i] > 0 for s in shares)]
    for i in kept:
        if any(s[i] == 0 for s in shares):
            raise AlgoError(f"slice {i + 1} would leave a leg with no quantity: use fewer slices or quantities that split in the same ratio")
    fp, base = parent.fingerprint(), parent.legs[0].qty
    children = []
    for position, i in enumerate(kept, start=1):
        legs = tuple(dataclasses.replace(leg, qty=shares[k][i]) for k, leg in enumerate(parent.legs))
        changes: dict[str, Any] = {"legs": legs, "thesis": f"[{algo} {position}/{len(kept)} of {fp}] {parent.thesis}"[:4000]}
        if prices[i] is not None:
            changes.update(order_type="limit", limit_price=prices[i], stop_price=None)
        if parent.risk_usd is not None:
            changes["risk_usd"] = (parent.risk_usd * shares[0][i] / base).quantize(_CENT, rounding=ROUND_UP)
        children.append(dataclasses.replace(parent, **changes))
    return tuple(children)


def parent_of(child: OrderProposal) -> str | None:
    """The fingerprint of the parent order a child carries, or None when it is not a slice."""
    found = _MARK.match(child.thesis)
    return None if found is None else found.group(4)


def run_slices(parent_fingerprint: str, algo: str, children: Sequence[OrderProposal], gate: Callable[[OrderProposal], Any],
               *, wait: Callable[[int], None] | None = None) -> AlgoRun:
    """Emit the children in order through `gate` (the filter): it answers (status, code) or an object with `status` and `code`. The first slice
    that ends in a halting status (refused, rejected_by_broker, expired, canceled) stops the line: the next slices are not emitted (fail closed)."""
    outcomes: list[SliceOutcome] = []
    for index, child in enumerate(children, start=1):
        if index > 1 and wait is not None:
            wait(index)
        answer = gate(child)
        status, code = answer if isinstance(answer, tuple) else (answer.status, answer.code)
        outcomes.append(SliceOutcome(index, child, status, code, None if isinstance(answer, tuple) else answer))
        if status in HALTING:
            return AlgoRun(parent_fingerprint, algo, len(children), tuple(outcomes), index, code)
    return AlgoRun(parent_fingerprint, algo, len(children), tuple(outcomes))


# ------------------------------------------------------------------------------------------------------------- submitting

def execute(parent: OrderProposal, algo: str, *, who: Any, db: Any = None, wait: Callable[[int], None] | None = None,
            **params: Any) -> AlgoRun:
    """Slice `parent` and send every child through the door, one after the other. Blocking (the door is a network call): call it from a thread."""
    from hokma.exec.submit import submit              # imported here: the pure part of this module never loads the door

    children = slice_order(parent, algo, **params)
    return run_slices(parent.fingerprint(), algo, children, lambda child: submit(child, who=who, db=db), wait=wait)
