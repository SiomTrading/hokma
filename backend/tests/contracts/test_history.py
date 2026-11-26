"""Historical contract properties, margins and root table (SPEC-020: L-01, L-02, L-08)."""
import csv
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from hokma.contracts import expiry, history, identity, sessions, symbols
from hokma.contracts.history import HISTORICAL_MARGIN_MISSING, MarginUnavailable
from hokma.contracts.identity import ContractSheet, NoContract

DAY = date(2025, 11, 10)


@pytest.fixture(autouse=True)
def clean():
    identity.clear_providers()
    yield
    identity.clear_providers()


def test_the_sheet_of_the_history_has_multiplier_and_tick():
    nq, es, mnq = (history.sheet(root, on=DAY) for root in ("NQ", "ES", "MNQ"))
    assert (nq.multiplier, nq.tick_size, nq.tick_value) == (Decimal(20), Decimal("0.25"), Decimal(5))
    assert (es.multiplier, es.tick_size) == (Decimal(50), Decimal("0.25"))
    assert mnq.multiplier == Decimal(2)
    assert nq.source == "history_table" and nq.exchange == "cme"


def test_gold_and_crude_properties_are_the_exchange_ones():
    gc, cl = history.sheet("GC", on=DAY), history.sheet("CL", on=DAY)
    assert (gc.multiplier, gc.tick_size, gc.tick_value) == (Decimal(100), Decimal("0.1"), Decimal(10))
    assert (cl.multiplier, cl.tick_size, cl.tick_value) == (Decimal(1000), Decimal("0.01"), Decimal(10))


def test_the_history_table_agrees_with_the_live_sheet_of_every_root_that_h2_knows():
    """The recorded live sheet is the one of the contracts module; if a value ever differs the test names the root and the field."""
    known = identity._specs()["roots"]
    disagreements = {}
    for root in known:
        live = identity.resolve(f"FUT:{root}:2025-12")
        found = history.differences(live)
        if found:
            disagreements[root] = found
    assert disagreements == {}


def test_a_live_sheet_that_differs_is_reported_by_field():
    odd = ContractSheet("FUT:NQ:2025-12", "future", "NQ", "NQ", Decimal(25), Decimal("0.5"), Decimal("12.5"), date(2025, 12, 1), None, None,
                        "USD", "provider", datetime(2025, 11, 10, tzinfo=timezone.utc))
    assert history.differences(odd) == ("multiplier", "tick_size")


def test_only_a_future_has_historical_properties():
    with pytest.raises(NoContract):
        history.differences(identity.resolve("EQ:AAPL"))


def test_a_margin_before_the_end_of_the_series_is_the_value_of_the_series():
    m = history.margin("NQ", date(2021, 6, 1))
    assert (m.initial, m.maintenance, m.source, m.effective_from, m.warnings) == (Decimal(17600), Decimal(16000), "series", date(2020, 12, 11), ())
    assert history.margin("NQ", date(2021, 8, 18)).initial == Decimal(18700)        # the day a row starts, the row applies
    assert history.margin("NQ", date(2021, 8, 17)).initial == Decimal(17600)        # the day before, the older one


def test_a_margin_after_the_series_is_the_current_sheet_with_a_warning():
    m = history.margin("NQ", date(2025, 11, 10))
    assert (m.initial, m.maintenance, m.source, m.effective_from) == (Decimal(16500), Decimal(15000), "current_sheet", None)
    assert m.warnings == (HISTORICAL_MARGIN_MISSING,) == ("historical_margin_missing",)
    live = history.margin("NQ", date(2025, 11, 10), current={"NQ": (Decimal(21000), Decimal(19000))})
    assert (live.initial, live.maintenance, live.source) == (Decimal(21000), Decimal(19000), "current_sheet")


def test_the_last_day_of_the_series_still_belongs_to_it():
    assert history.margin("NQ", date(2022, 1, 21)).source == "series"
    assert history.margin("NQ", date(2022, 1, 22)).source == "current_sheet"
    assert history.margin("MNQ", date(2022, 1, 21)).initial == Decimal(1650)


def test_no_margin_is_given_before_the_series_starts():
    with pytest.raises(MarginUnavailable) as error:
        history.margin("NQ", date(2011, 1, 3))
    assert error.value.code == "margin_before_series" and "2012-03-26" in str(error.value)
    with pytest.raises(NoContract):
        history.margin("XYZ", date(2021, 1, 4))


def _table(name):
    with (history.DATA / name).open(encoding="utf-8", newline="") as handle:
        return {r["root"] for r in csv.DictReader(handle)}


def test_every_data_file_covers_the_same_roots():
    roots = _table("futures_roots.csv")
    assert roots == _table("futures_properties.csv") == {p.stem for p in (history.DATA / "margins").glob("*.csv")}
    assert roots == set(sessions._table()[1])


def test_symbols_spell_every_root_of_the_table_and_read_it_back():
    for root in sorted(_table("futures_roots.csv")):
        written = symbols.to_provider(f"FUT:{root}:2026-12", "schwab")
        assert written == f"/{root}Z26"
        assert symbols.from_provider("schwab", written) == f"FUT:{root}:2026-12"


def test_an_unknown_root_raises_no_contract():
    with pytest.raises(NoContract):
        symbols.from_provider("ibkr", "XYZZ6")
    with pytest.raises(NoContract):
        history.sheet("XYZ", on=DAY)
    with pytest.raises(NoContract):
        expiry.root_info("XYZ")


def test_the_roots_that_the_exchange_spec_table_of_h2_also_knows_read_back_without_a_slash():
    for root in identity._specs()["roots"]:
        assert symbols.from_provider("ibkr", f"{root}Z6") == f"FUT:{root}:2026-12"
