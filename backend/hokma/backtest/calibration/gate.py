"""The gate of the calibration of the premium selling threshold (SPEC-020, section 4.9, decision 1260).

The defaults live in `kit/template/service/calibration/premium-gate.yaml`. The owner can tighten the gate; a gate that is looser than the
defaults in any of the fields listed in `LOOSENING_RULES` is refused until the owner asks for it (decision 1245): `loosened_fields`
names them. The gate never decides alone: `hokma.backtest.calibration.premium.calibrate` compares its numbers with the evidence of a
backtest, and the switch of SPEC-019 opens only when every one passes. No field of the gate opens the switch.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from hokma.broker import fees
from hokma.desks.swo.premium_switch import SELLING_STRUCTURES

Metric = Literal["iv_rv_ratio", "iv_rank", "iv_minus_rv"]
METRICS: tuple[str, ...] = ("iv_rv_ratio", "iv_rank", "iv_minus_rv")
UNSUPPORTED_STRUCTURES: tuple[str, ...] = ("covered_call",)         # it needs the shares of the account, which a chain history does not have
SUPPORTED_STRUCTURES: tuple[str, ...] = tuple(s for s in SELLING_STRUCTURES if s not in UNSUPPORTED_STRUCTURES)
EXIT_KEYS: tuple[str, ...] = ("profit_take_pct", "stop_loss_multiple", "close_at_dte")
LEG_KEYS: tuple[str, ...] = ("short_otm_pct", "wing_pct")
DEFAULT_EXITS: Mapping[str, Any] = {"profit_take_pct": 50.0, "stop_loss_multiple": 2.0, "close_at_dte": 14}
DEFAULT_LEGS: Mapping[str, Any] = {"short_otm_pct": 5.0, "wing_pct": 5.0}
MAX_STRUCTURES = 8


class GateError(ValueError):
    """The gate is not valid. `code` is stable: bad_gate, gate_loosened."""

    code = "bad_gate"


class GateLoosened(GateError):
    """A gate that is looser than the defaults was given without the owner asking for it."""

    code = "gate_loosened"


def gate_path() -> Path:
    from hokma.kit.integrity import app_root

    return app_root() / "kit" / "template" / "service" / "calibration" / "premium-gate.yaml"


def _number(name: str, value: Any, low: float, high: float, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise GateError(f"bad_gate: {name} is a number from {low:g} to {high:g}")
    if integer:
        if int(value) != value:
            raise GateError(f"bad_gate: {name} is a whole number")
        return int(value)
    return float(value)


def _plain_map(name: str, value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise GateError(f"bad_gate: {name} has exactly the keys {', '.join(keys)}")
    return dict(value)


@dataclass(frozen=True)
class PremiumGate:
    """What a backtest of selling premium must show before the switch of a desk opens (spec 4.9).

    `metric` is the one Caleb reads. `structures` are values of `SWOProposal.structure` that sell premium. `exits` come from the mandate of
    the desk when it states them and from the yaml otherwise. `max_drawdown_pct` None means the limit of the desk, and a number never
    goes above that limit: the lower one rules. The last four fields are the assumptions of the backtest that the gate fixes too."""

    metric: Metric = "iv_rv_ratio"
    structures: tuple[str, ...] = ("cash_secured_put", "credit_put_spread")
    dte: tuple[int, int] = (21, 60)
    exits: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_EXITS))
    min_oos_trades: int = 30
    min_dsr: float = 0.95
    max_pbo: float = 0.50
    min_wf_efficiency: float = 0.50
    folds: int = 5
    max_drawdown_pct: float | None = None
    legs: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_LEGS))
    margin_use_pct: float = 10.0
    fee_table: str = "ibkr_options"
    min_history_days: int = 504

    def __post_init__(self) -> None:
        if self.metric not in METRICS:
            raise GateError(f"bad_gate: metric is one of {', '.join(METRICS)}")
        names = tuple(self.structures) if isinstance(self.structures, (list, tuple)) else ()
        if not 1 <= len(names) <= MAX_STRUCTURES or len(set(names)) != len(names) or any(n not in SUPPORTED_STRUCTURES for n in names):
            raise GateError(f"bad_gate: structures are 1 to {MAX_STRUCTURES} different values of {', '.join(SUPPORTED_STRUCTURES)}")
        if not isinstance(self.dte, (list, tuple)) or len(self.dte) != 2:
            raise GateError("bad_gate: dte is a pair (lowest, highest) of days")
        low = _number("dte[0]", self.dte[0], 1, 365, integer=True)
        high = _number("dte[1]", self.dte[1], 1, 365, integer=True)
        if low >= high:
            raise GateError("bad_gate: the lowest dte is below the highest")
        exits = _plain_map("exits", self.exits, EXIT_KEYS)
        exits["profit_take_pct"] = _number("exits.profit_take_pct", exits["profit_take_pct"], 1, 100)
        exits["stop_loss_multiple"] = _number("exits.stop_loss_multiple", exits["stop_loss_multiple"], 0.1, 20)
        exits["close_at_dte"] = _number("exits.close_at_dte", exits["close_at_dte"], 0, 364, integer=True)
        if exits["close_at_dte"] >= low:
            raise GateError("bad_gate: the position closes at a dte below the lowest dte it opens at")
        legs = _plain_map("legs", self.legs, LEG_KEYS)
        legs = {k: _number(f"legs.{k}", legs[k], 0.5 if k == "wing_pct" else 0, 50) for k in LEG_KEYS}
        _number("min_oos_trades", self.min_oos_trades, 1, 100_000, integer=True)
        _number("min_dsr", self.min_dsr, 0.5, 1.0)
        _number("max_pbo", self.max_pbo, 0.0, 1.0)
        _number("min_wf_efficiency", self.min_wf_efficiency, 0.0, 10.0)
        _number("folds", self.folds, 2, 20, integer=True)
        if self.max_drawdown_pct is not None:
            _number("max_drawdown_pct", self.max_drawdown_pct, 0.01, 100)
        _number("margin_use_pct", self.margin_use_pct, 1, 100)
        _number("min_history_days", self.min_history_days, 60, 20_000, integer=True)
        if not isinstance(self.fee_table, str) or self.fee_table not in fees.tables():
            raise GateError(f"bad_gate: fee_table is one of {', '.join(fees.tables())}")
        if "option" not in fees.load(self.fee_table).rules:
            raise GateError("bad_gate: the fee table has no rule for options")
        object.__setattr__(self, "structures", names)
        object.__setattr__(self, "dte", (low, high))
        object.__setattr__(self, "exits", exits)
        object.__setattr__(self, "legs", legs)

    def to_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "structures": list(self.structures), "dte": list(self.dte), "exits": dict(self.exits),
                "min_oos_trades": self.min_oos_trades, "min_dsr": self.min_dsr, "max_pbo": self.max_pbo, "min_wf_efficiency": self.min_wf_efficiency,
                "folds": self.folds, "max_drawdown_pct": self.max_drawdown_pct, "legs": dict(self.legs), "margin_use_pct": self.margin_use_pct,
                "fee_table": self.fee_table, "min_history_days": self.min_history_days}


def from_dict(data: Mapping[str, Any]) -> PremiumGate:
    """A gate from plain values (the yaml file or a saved copy). A key that is not a field of the gate is refused."""
    if not isinstance(data, Mapping):
        raise GateError("bad_gate: the gate is a mapping")
    allowed = set(PremiumGate.__dataclass_fields__)
    extra = sorted(set(data) - allowed)
    if extra:
        raise GateError(f"bad_gate: unknown field {extra[0]!r}")
    values = dict(data)
    if "structures" in values:
        values["structures"] = tuple(values["structures"]) if isinstance(values["structures"], (list, tuple)) else values["structures"]
    if "dte" in values:
        values["dte"] = tuple(values["dte"]) if isinstance(values["dte"], (list, tuple)) else values["dte"]
    return PremiumGate(**values)


def load_default(path: str | Path | None = None) -> PremiumGate:
    """The defaults of the kit. The file is read as data (`safe_load`) and every field is checked by `PremiumGate`."""
    found = Path(path) if path is not None else gate_path()
    try:
        data = yaml.safe_load(found.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GateError(f"bad_gate: the gate file cannot be read: {exc}") from exc
    return from_dict(data)


# What counts as looser than the base, field by field. A bigger number is looser for a maximum and a smaller one for a minimum. The
# structures and the window of dte are looser when they reach beyond the base; the fee table is looser when it is another one.
LOOSENING_RULES: tuple[str, ...] = ("min_oos_trades", "min_dsr", "min_wf_efficiency", "folds", "min_history_days", "max_pbo", "structures", "dte", "fee_table")


def loosened_fields(candidate: PremiumGate, base: PremiumGate) -> list[str]:
    """The fields of `candidate` that are looser than `base`. A gate that tightens or keeps everything gives an empty list."""
    found = [name for name in ("min_oos_trades", "min_dsr", "min_wf_efficiency", "folds", "min_history_days") if getattr(candidate, name) < getattr(base, name)]
    if candidate.max_pbo > base.max_pbo:
        found.append("max_pbo")
    if set(candidate.structures) - set(base.structures):
        found.append("structures")
    if candidate.dte[0] < base.dte[0] or candidate.dte[1] > base.dte[1]:
        found.append("dte")
    if candidate.fee_table != base.fee_table:
        found.append("fee_table")
    return found


def require_not_looser(candidate: PremiumGate, base: PremiumGate | None = None) -> PremiumGate:
    """`candidate` itself when it is not looser than the defaults; GateLoosened (code gate_loosened) naming the fields when it is."""
    loose = loosened_fields(candidate, base or load_default())
    if loose:
        raise GateLoosened(f"gate_loosened: only the owner can loosen {', '.join(loose)}")
    return candidate


def tightened(base: PremiumGate, **changes: Any) -> PremiumGate:
    """`base` with some fields changed, refused when a change loosens it."""
    return require_not_looser(replace(base, **changes), base)
