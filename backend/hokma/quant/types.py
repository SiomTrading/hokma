"""Common result type of every quant function (SPEC-017, section 4.2)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from hokma.quant.errors import BadInput, BlockedFunction  # noqa: F401  (re-exported)


@dataclass(frozen=True)
class QuantResult:
    name: str                                   # registry name, e.g. "metrics.sharpe"
    value: float | dict | None                  # None when it could not be computed
    unit: str                                   # "ratio", "pct", "usd", "prob", ...
    method: str                                 # formula or estimator used
    params: dict = field(default_factory=dict)  # periods_per_year, confidence, seed, ...
    inputs_hash: str = ""                       # content hash of the inputs
    as_of: datetime | None = None               # last observation used
    warnings: tuple[str, ...] = ()              # bad data is explained here, never raised
    piece_ids: tuple[str, ...] = ()             # matrix ids behind this function

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat() if self.as_of else None
        data["warnings"] = list(self.warnings)
        data["piece_ids"] = list(self.piece_ids)
        return data

    @property
    def ok(self) -> bool:
        return self.value is not None

    @staticmethod
    def empty(name: str, unit: str, method: str, warning: str, **extra: Any) -> "QuantResult":
        """A result that could not be computed: value None and the reason in warnings."""
        return QuantResult(name=name, value=None, unit=unit, method=method, warnings=(warning,), **extra)
