"""The public functions of section 4.5 exist with the documented signatures (SPEC-017: data package)."""
import inspect
from datetime import datetime, timezone

import hokma.data as data
from hokma.data import macro as macro_module, sentinels, snapshot as snapshot_module, universe as universe_module

from .support import FakeMarketSource, market_source, when  # noqa: F401


def test_the_six_functions_of_the_public_interface_exist() -> None:
    assert {"bars", "snapshot", "fundamentals", "filings", "macro", "universe"} <= set(data.__all__)
    sig = lambda f: list(inspect.signature(f).parameters)
    assert sig(data.bars) == ["symbol", "interval", "start", "end", "as_of"]
    assert sig(data.fundamentals) == ["symbol", "as_of", "statement", "period"]
    assert sig(data.filings)[:3] == ["symbol", "form", "as_of"]


def test_macro_universe_and_snapshot_are_callable_modules() -> None:
    assert data.macro is macro_module and data.universe is universe_module and data.snapshot is snapshot_module
    assert callable(data.macro) and callable(data.universe) and callable(data.snapshot)
    assert hasattr(data.macro, "FredProvider") and hasattr(data.universe, "Catalog") and hasattr(data.snapshot, "compute_indicator")


def test_bars_through_the_package_come_from_the_market_api(market_source) -> None:
    source = market_source(FakeMarketSource())
    result = data.bars("ACME", "1D", start=when("2024-01-01"), end=when("2024-02-01"), as_of=when("2024-02-01"))
    assert result.ok and source.calls == [("ACME", "1D")]


def test_filings_without_a_saved_identity_is_unavailable_with_a_reason(monkeypatch) -> None:
    from hokma.data.sec import SecClient

    class NoIdentity(SecClient):
        def identity(self):
            return None

    result = data.filings("ACME", form="10-Q", as_of=when("2024-04-30"), client=NoIdentity())
    assert result.status == sentinels.DATA_UNAVAILABLE and result.value is None and result.reason and result.source == "sec_edgar"


def test_fundamentals_through_the_package_without_a_key_is_a_sentinel() -> None:
    result = data.fundamentals("ACME", as_of=when("2024-04-30"), statement="metrics", period="ttm")
    assert result.status in (sentinels.DATA_UNAVAILABLE, sentinels.NO_DATA) and result.value is None
