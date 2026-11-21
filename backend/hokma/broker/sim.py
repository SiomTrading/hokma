"""The internal broker of Hokma (task 3294, C7-5b): accounts whose money lives inside Hokma, with no orders. Its balance is the
initial capital. Costs live in the broker's config and are editable. Orders with real money go through a broker that takes
them (Schwab, SPEC-015 18)."""
from __future__ import annotations

from typing import Any, Mapping

from hokma.broker.contract import BaseBroker
from hokma.broker.costs import DEFAULT_COMMISSION, DEFAULT_SLIPPAGE_BPS, MAX_BPS, _number, validate_commission


class SimBroker(BaseBroker):
    @property
    def commission(self) -> dict[str, Any]:
        return self.row.config.get("commission", DEFAULT_COMMISSION)

    @property
    def slippage_bps(self) -> float:
        return self.row.config.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)

    def set_costs(self, commission: Mapping[str, Any] | None = None, slippage_bps: float | None = None) -> None:
        """Change the costs; what is not given stays. Validated before it is saved."""
        config = {"commission": validate_commission(commission) if commission is not None else self.commission,
                  "slippage_bps": _number(slippage_bps, "slippage", MAX_BPS) if slippage_bps is not None else self.slippage_bps}
        self.row = self.repo.set_broker_config(self.id, config)
