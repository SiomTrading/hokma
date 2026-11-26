"""A threshold that grows with the account (SPEC-026, section 3.2, test A8). A percentage and a scale of steps are not saved as a number in
USD: the USD is worked out again at every evaluation from the base the guard gives (the equity), so it moves with the account and the rule
is never edited. `steps` is a list of {"up_to_equity": A, "usd": X} in ascending order of A: the first step whose A is at least the base
applies, and the last step covers what is left."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

MAX_STEPS = 20


def clean_steps(value: Any) -> list[dict[str, float]]:
    """The steps checked. Raises ValueError with the sentence for the owner."""
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_STEPS:
        raise ValueError(f"steps must be a list of 1 to {MAX_STEPS} steps")
    out: list[dict[str, float]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"up_to_equity", "usd"}:
            raise ValueError("a step is {up_to_equity, usd}")
        for key in ("up_to_equity", "usd"):
            n = item[key]
            if isinstance(n, bool) or not isinstance(n, (int, float)) or not 0 < n <= 1e12 or n != n:
                raise ValueError(f"{key} of a step must be a number above 0")
        out.append({"up_to_equity": float(item["up_to_equity"]), "usd": float(item["usd"])})
    if any(b["up_to_equity"] <= a["up_to_equity"] for a, b in zip(out, out[1:])):
        raise ValueError("the steps go in ascending order of up_to_equity")
    return out


def usd_for(steps: Sequence[Mapping[str, Any]], base: Decimal) -> Decimal:
    """The USD of the step that the base falls in. The last step covers what is left."""
    for step in steps:
        if base <= Decimal(str(step["up_to_equity"])):
            return Decimal(str(step["usd"]))
    return Decimal(str(steps[-1]["usd"]))


def effective_usd(fixed: Any, steps: Sequence[Mapping[str, Any]] | None, base: Decimal | None) -> Decimal | None:
    """The USD cap a loss or a drawdown has right now, the same one the account guard applies: the fixed USD and the step of the base
    both hold, so the smaller wins. None when neither is set. A scale of steps with no base to place it in gives its smallest step
    (fails closed: the strictest cap, never a looser one)."""
    caps: list[Decimal] = []
    if fixed is not None:
        caps.append(Decimal(str(fixed)))
    if steps:
        caps.append(usd_for(steps, base) if base is not None else min(Decimal(str(s["usd"])) for s in steps))
    return min(caps) if caps else None
