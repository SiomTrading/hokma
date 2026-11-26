"""Shared builders for the factory tests."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hokma.contracts.identity import ContractSheet

T0 = datetime(2025, 3, 3, 15, 0, tzinfo=timezone.utc)


def nq() -> ContractSheet:
    return ContractSheet("FUT:NQ:2025-03", "future", "NQ", None, Decimal("20"), Decimal("0.25"), Decimal("5"), None, None, None, "USD", "test", T0)


def stock() -> ContractSheet:
    return ContractSheet("EQ:SPY", "equity", None, "SPY", Decimal("1"), None, None, None, None, None, "USD", "test", T0)


def minutes(n: int, start: datetime = T0) -> list[datetime]:
    return [start + timedelta(minutes=i) for i in range(n)]
