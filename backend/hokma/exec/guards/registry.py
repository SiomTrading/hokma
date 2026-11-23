"""Guards by type (SPEC-018: OA-47). The desk configuration (`desk_limits.config`) can carry `guards`: the list of types that run. A
type that does not exist fails when the configuration is SAVED (`validate_types`), never when an order is sent. The contract
guard and the rules of the broker always run: no configuration turns them off."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from hokma.contracts import discipline
from hokma.contracts.identity import ContractSheet
from hokma.exec.guards import Guard, GuardContext, GuardResult, blocked, passed
from hokma.exec.guards.account import AccountGuard
from hokma.exec.guards.broker_rules import BrokerRulesGuard
from hokma.exec.guards.cooldown import CooldownGuard
from hokma.exec.guards.event_window import EventWindowGuard
from hokma.exec.guards.limits_guard import LimitsGuard
from hokma.exec.guards.max_position import MaxPositionSizeGuard
from hokma.exec.guards.monitor_guard import MonitorGuard
from hokma.exec.guards.premium_selling import PremiumSellingGuard
from hokma.exec.guards.whitelist import SymbolWhitelistGuard


class ContractDisciplineGuard:
    """OA-48 at the border of the filter: every leg has a sheet with its multiplier, and a future or an option has its expiration (and strike)."""
    name = "contract"
    applies_to_closing = True

    def check(self, ctx: GuardContext) -> GuardResult:
        try:
            for leg in ctx.proposal.legs:
                discipline.check_sheet(ctx.contracts.get(leg.contract_id), leg.contract_id)
        except discipline.DisciplineError as exc:
            return blocked(self.name, exc.code, exc.why)
        return passed(self.name)


# The fixed order of the pipeline. The premium-selling switch (SPEC-019, spec 4.5) sits between "limits" and "broker_rules".
ORDER: tuple[tuple[str, Callable[[], Guard]], ...] = (
    ("contract", ContractDisciplineGuard),
    ("whitelist", SymbolWhitelistGuard),
    ("event_window", EventWindowGuard),
    ("cooldown", CooldownGuard),
    ("max_position", MaxPositionSizeGuard),
    ("monitor", MonitorGuard),
    ("account", AccountGuard),
    ("limits", LimitsGuard),
    ("premium_selling", PremiumSellingGuard),
    ("broker_rules", BrokerRulesGuard),
)
ALWAYS = ("contract", "monitor", "premium_selling", "broker_rules")


def known_types() -> tuple[str, ...]:
    return tuple(name for name, _ in ORDER)


def validate_types(value: Any) -> list[str]:
    """The `guards` list of a configuration. Raises ValueError for anything that is not a known type."""
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("guards must be a list of guard types")
    unknown = sorted({x for x in value if x not in known_types()})
    if unknown:
        raise ValueError(f"unknown guard type: {unknown[0][:40]}")
    return list(dict.fromkeys(value))


def build(config: Mapping[str, Any] | None) -> list[Guard]:
    """The guards of a desk, in the fixed order. Without a `guards` list every type runs."""
    wanted = None
    if config and config.get("guards") is not None:
        wanted = set(validate_types(list(config["guards"]))) | set(ALWAYS)
    return [factory() for name, factory in ORDER if wanted is None or name in wanted]
