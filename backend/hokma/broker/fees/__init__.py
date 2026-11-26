"""Commission tables per broker (SPEC-020 N-10; the same files are read by the simulated broker and by the backtest).

A table is a JSON file in this folder: `rules` maps an asset type (equity, option, future, cfd) to a rule with a `kind` (per_contract,
per_share, per_order, bps), a `value`, an optional `minimum` per order and an optional `cap_bps` (the commission never goes above that
share of the traded value, the cap of option tables). A table of a CFD broker may carry `cfd_swap` (the nightly rates). Every amount is computed by `hokma.broker.costs.commission`: this module has no
formula of its own. A table is read by NAME: a name is lowercase letters, digits and underscores, never a path."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from hokma.broker import costs
from hokma.db.errors import ValidationError

FEES_DIR = Path(__file__).resolve().parent
KINDS = ("per_contract", "per_share", "per_order", "bps")
_NAME = re.compile(r"[a-z0-9_]{1,40}")


@dataclass(frozen=True)
class FeeRule:
    kind: str
    value: Decimal
    minimum: Decimal = Decimal(0)
    cap_bps: Decimal | None = None


@dataclass(frozen=True)
class SwapRates:
    """Overnight financing of a CFD as a fraction of the notional per night, charged to the trader when positive.
    `triple_weekday` is the weekday (Monday is 0) that pays three nights for the weekend, or None."""
    long_daily: Decimal
    short_daily: Decimal
    triple_weekday: int | None = None


@dataclass(frozen=True)
class FeeTable:
    broker: str
    currency: str
    reference: str
    rules: Mapping[str, FeeRule]
    swap: SwapRates | None = None


def _rule(raw: Mapping[str, object]) -> FeeRule:
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ValidationError(f"fee kind must be one of {', '.join(KINDS)}; got {kind!r}")
    cap = raw.get("cap_bps")
    return FeeRule(str(kind), Decimal(str(raw["value"])), Decimal(str(raw.get("minimum", "0"))), None if cap is None else Decimal(str(cap)))


def load(name: str) -> FeeTable:
    """The table called `name` (`schwab`, `zero`, with or without `.json`). An unknown or malformed name is a ValidationError."""
    stem = name[:-5] if isinstance(name, str) and name.endswith(".json") else name
    if not isinstance(stem, str) or not _NAME.fullmatch(stem):
        raise ValidationError("a fee table name is lowercase letters, digits and underscores")
    return _read(stem)


@lru_cache(maxsize=32)
def _read(stem: str) -> FeeTable:
    path = FEES_DIR / f"{stem}.json"
    if not path.is_file():
        raise ValidationError(f"unknown fee table {stem!r}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    swap = raw.get("cfd_swap")
    rates = None if swap is None else SwapRates(Decimal(str(swap["long_daily"])), Decimal(str(swap["short_daily"])), swap.get("triple_weekday"))
    return FeeTable(str(raw["broker"]), str(raw["currency"]), str(raw["reference"]),
                    {asset: _rule(rule) for asset, rule in raw["rules"].items()}, rates)


def tables() -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in FEES_DIR.glob("*.json")))


def fee(table: FeeTable, asset_type: str, *, qty: Decimal, price: Decimal, multiplier: Decimal = Decimal(1)) -> Decimal:
    """The commission of ONE side of an order of `qty` units at `price`. A contract of a future or an option pays per contract whatever
    the multiplier is. Zero quantity pays nothing."""
    rule = table.rules.get(asset_type)
    if rule is None:
        raise ValidationError(f"fee table {table.broker!r} has no rule for {asset_type!r}")
    notional = float(abs(qty) * price * multiplier)
    if rule.kind == "per_order":
        amount = max(rule.value, rule.minimum) if qty else Decimal(0)
    else:
        cfg = ({"type": "bps", "value": float(rule.value), "min": float(rule.minimum)} if rule.kind == "bps"
               else {"type": "per_share", "value": float(rule.value), "min": float(rule.minimum)})
        amount = Decimal(str(costs.commission(notional, float(abs(qty)), cfg)))
    if rule.cap_bps is not None and qty:
        cap = Decimal(str(costs.commission(notional, float(abs(qty)), {"type": "bps", "value": float(rule.cap_bps), "min": 0.0})))
        amount = min(amount, cap)
    return amount
