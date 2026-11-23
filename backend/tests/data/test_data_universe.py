"""Instrument universe, identifiers, discovery and sample data (SPEC-017: FD-001 to FD-009, CC-12, CC-13, FT-097, SK-118)."""
import pandas as pd
import pytest

from hokma.data import _lib, sentinels, universe as universe_module
from hokma.data.universe import (ASSET_CLASS_USE, ASSET_CLASSES, CATALOG_FILES, DESCRIPTOR_FIELDS, DISCOVERY_KINDS, Catalog, compress_catalog, describe_files,
                                 discover, identifier_kind, load_catalogs, normalise_identifier, recipe, reconcile_identifiers, repair_identifier, sample,
                                 universe, validate_cusip, validate_figi, validate_isin)

from .support import FIXTURES, no_network, when  # noqa: F401

DIRECTORY = FIXTURES / "compression"


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "catalog-cache"


@pytest.fixture
def equities(cache_dir) -> Catalog:
    return Catalog("equities", directory=DIRECTORY, cache_dir=cache_dir)


# ------------------------------------------------------------------------------------------------ FD-001, FD-005, CC-12
def test_catalog_loads_the_compressed_file_of_an_asset_class_FD_001(equities) -> None:
    assert equities.asset_class == "equities" and len(equities.data) == 4 and equities.columns()[0] == "symbol"
    assert "AAPL" in equities.data.index and "isin" in equities.columns()
    with pytest.raises(ValueError):
        Catalog("bonds", directory=DIRECTORY)


def test_the_universe_loads_the_seven_compressed_files_and_no_other_source_CC_12(cache_dir) -> None:
    assert len(CATALOG_FILES) == 7 and set(CATALOG_FILES.values()) == {f"{n}.bz2" for n in ASSET_CLASSES}
    catalogs = load_catalogs(directory=DIRECTORY, cache_dir=cache_dir)
    assert set(catalogs) == set(ASSET_CLASSES) and all(len(c.data) >= 1 for c in catalogs.values())
    assert catalogs["currencies"].data.loc["EURUSD=X", "base_currency"] == "EUR"
    empty = universe(directory=DIRECTORY.parent / "no_such_folder", cache_dir=cache_dir)
    assert empty.status == sentinels.DATA_UNAVAILABLE and empty.source == "catalog" and empty.value is None


def test_a_url_source_is_passed_to_the_loader_and_a_folder_wins_CC_12(monkeypatch, cache_dir) -> None:
    seen = {}

    def fake(asset_class, *, directory=None, base_url=None, cache_dir=None):
        seen.update(asset_class=asset_class, directory=directory, base_url=base_url)
        return type("Loaded", (), {"data": pd.DataFrame({"name": ["x"]}, index=pd.Index(["S"], name="symbol")), "get_columns": lambda self: ["symbol", "name"]})()

    monkeypatch.setattr(universe_module._lib, "load_asset_class", fake)
    Catalog("etfs", base_url="https://catalog.example/files/")
    assert seen == {"asset_class": "etfs", "directory": None, "base_url": "https://catalog.example/files/"}


def test_files_of_a_folder_are_described_FD_005(tmp_path) -> None:
    report = describe_files(DIRECTORY)
    assert all(r["present"] and r["bytes"] > 0 for r in report.values()) and report["equities"]["file"] == "equities.bz2"
    assert not any(r["present"] for r in describe_files(tmp_path).values())


# ------------------------------------------------------------------------------------------------ FD-002, FD-003, FD-004
def test_search_filters_by_column_and_hides_delisted_by_default_FD_002(equities) -> None:
    us = equities.search(country="United States")
    assert sorted(us.index) == ["AAPL", "MSFT"]                                       # the delisted company is out
    assert sorted(equities.search(country="United States", exclude_delisted=False).index) == ["AAPL", "MSFT", "OLDCO"]
    assert list(equities.search(name="apple").index) == ["AAPL"]                      # case-insensitive regular expression
    assert sorted(equities.search(sector=["Industrials", "Utilities"]).index) == ["BAE.L"] and equities.search(sector="Utilities").empty


def test_search_result_hands_its_symbols_to_the_toolkit_FD_003(equities) -> None:
    toolkit = equities.to_toolkit(equities.search(country="United States"), api_key="", progress_bar=False)
    assert toolkit._tickers == ["AAPL", "MSFT"]


def test_every_asset_class_has_a_use_and_loads_FD_004(cache_dir) -> None:
    assert set(ASSET_CLASS_USE) == set(ASSET_CLASSES)
    for name in ASSET_CLASSES:
        catalog = Catalog(name, directory=DIRECTORY, cache_dir=cache_dir)
        assert len(catalog.data) >= 1, name
    assert "options" in ASSET_CLASS_USE["equities"] and "CFD" in ASSET_CLASS_USE["currencies"] and "fixed income" in ASSET_CLASS_USE["moneymarkets"]


def test_universe_returns_a_frame_with_source_and_a_survivorship_warning_for_the_past_FD_004(cache_dir) -> None:
    now = lambda: when("2026-11-22T12:00:00")
    live = universe(directory=DIRECTORY, cache_dir=cache_dir, clock=now, country="United States")
    assert live.ok and live.source == "catalog" and sorted(live.value.index) == ["AAPL", "MSFT"] and live.received_at == now()
    assert live.warnings == ("catalog_is_current_not_point_in_time",) and live.meta["rows"] == 2
    past = universe(directory=DIRECTORY, cache_dir=cache_dir, clock=now, as_of=when("2015-01-01"), country="United States")
    assert "past_as_of_with_todays_listings_survivorship_risk" in past.warnings and "delisted_instruments_excluded" in past.warnings and past.as_of == when("2015-01-01")
    keep = universe(directory=DIRECTORY, cache_dir=cache_dir, clock=now, as_of=when("2015-01-01"), exclude_delisted=False, country="United States")
    assert "delisted_instruments_excluded" not in keep.warnings and "OLDCO" in keep.value.index
    none = universe(directory=DIRECTORY, cache_dir=cache_dir, clock=now, sector="Utilities")
    assert none.status == sentinels.NO_DATA and none.reason == sentinels.REASON_EMPTY
    other = universe(asset_class="etfs", directory=DIRECTORY, cache_dir=cache_dir, clock=now, name="S&P")
    assert other.ok and list(other.value.index) == ["SPY"] and other.meta["asset_class"] == "etfs"
    with pytest.raises(ValueError):
        universe(asset_class="bonds", directory=DIRECTORY, cache_dir=cache_dir)


# ------------------------------------------------------------------------------------------------ FD-006
def test_descriptor_fields_exist_in_each_catalog_FD_006(cache_dir) -> None:
    for name in ASSET_CLASSES:
        columns = set(Catalog(name, directory=DIRECTORY, cache_dir=cache_dir).columns())
        assert set(DESCRIPTOR_FIELDS[name]) <= columns, (name, set(DESCRIPTOR_FIELDS[name]) - columns)
    assert {"sector", "industry", "country", "isin", "cusip", "figi"} <= set(DESCRIPTOR_FIELDS["equities"])
    assert {"category", "family"} <= set(DESCRIPTOR_FIELDS["etfs"])


# ------------------------------------------------------------------------------------------------ FD-007, CC-13
VALID = {"isin": ["US0378331005", "US5949181045", "GB0002634946"], "cusip": ["037833100", "594918104", "17275R102"], "figi": ["BBG000B9XRY4", "BBG000BLNNH6", "BBG000BPH459"]}
INVALID = {"isin": ["US0378331006", "US5949181040", "GB0002634947"], "cusip": ["037833101", "594918105", "17275R103"], "figi": ["BBG000B9XRY5", "BBG000BLNNH7", "BBG000BPH450"]}


@pytest.mark.parametrize("kind", ["isin", "cusip", "figi"])
def test_identifier_validators_three_valid_and_three_invalid_per_type_CC_13(kind) -> None:
    check = {"isin": validate_isin, "cusip": validate_cusip, "figi": validate_figi}[kind]
    assert [check(v) for v in VALID[kind]] == [True, True, True]
    assert [check(v) for v in INVALID[kind]] == [False, False, False]


def test_identifier_format_rules_CC_13() -> None:
    assert not validate_isin("US037833100") and not validate_isin("us0378331005") and not validate_cusip("03783310") and not validate_figi("BBG000B9XRY")
    assert not validate_figi("BSG000B9XRY4") and not validate_figi("AAG000B9XRY4")      # a forbidden prefix; vowels are not allowed
    assert identifier_kind("us-0378331005") == "isin" and identifier_kind("037833100") == "cusip" and identifier_kind("BBG000BPH459") == "figi" and identifier_kind("nonsense") is None


def test_repair_fixes_only_what_is_certain_FD_007() -> None:
    assert repair_identifier("isin", "US0378331005") == ("US0378331005", "valid")
    assert repair_identifier("isin", " us-0378331005 ") == ("US0378331005", "normalised")
    assert repair_identifier("isin", "US037833100") == ("US0378331005", "check_digit_added")
    assert repair_identifier("cusip", "03783310") == ("037833100", "check_digit_added")
    assert repair_identifier("figi", "BBG000B9XRY") == ("BBG000B9XRY4", "check_digit_added")
    assert repair_identifier("isin", "US0378331006") == (None, "invalid_check_digit_or_format")     # a wrong check digit may hide a typo: not corrected
    with pytest.raises(ValueError):
        repair_identifier("ssn", "x")
    assert normalise_identifier("bbg 000-b9xry4") == "BBG000B9XRY4"


def test_catalog_identifiers_are_reconciled_FD_007(equities) -> None:
    table = reconcile_identifiers(equities.search(country="United States", exclude_delisted=False))
    assert table.query("symbol == 'AAPL'")[["kind", "valid"]].values.tolist() == [["isin", True], ["cusip", True], ["figi", True]]
    old = table.query("symbol == 'OLDCO'")
    assert list(old["kind"]) == ["isin"] and list(old["valid"]) == [False] and list(old["note"]) == ["invalid_check_digit_or_format"]
    assert reconcile_identifiers(equities.data.iloc[0:0]).empty


# ------------------------------------------------------------------------------------------------ FD-008, FD-009
def test_the_recipe_names_functions_that_exist_FD_008() -> None:
    steps = recipe()
    assert [s["step"] for s in steps] == ["1", "2", "3", "4", "5"]
    for step in steps:
        owner, _, name = step["call"].rpartition(".")
        target = getattr(universe_module, owner) if owner else universe_module
        assert callable(getattr(target, name)), step


def test_a_catalog_file_round_trips_through_compression_FD_009(equities, tmp_path, cache_dir) -> None:
    path = compress_catalog(equities.data, tmp_path / "mine" / "equities.bz2")
    assert path.name == "equities.bz2" and path.read_bytes()[:3] == b"BZh"                  # the bz2 magic number
    again = Catalog("equities", directory=path.parent, cache_dir=cache_dir / "again")
    assert list(again.data.index) == list(equities.data.index) and again.data.loc["MSFT", "name"] == "Microsoft Corporation"
    with pytest.raises(ValueError):
        compress_catalog(pd.DataFrame({"name": ["x"]}), tmp_path / "bad.bz2")


# ------------------------------------------------------------------------------------------------ FT-097
class FakeDiscovery:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def get_biggest_gainers(self, **kwargs) -> pd.DataFrame:
        return pd.DataFrame({"symbol": ["AAA"], "change": [12.5]})

    def get_ipo_calendar(self, **kwargs) -> pd.DataFrame:
        return pd.DataFrame({"symbol": ["NEWCO"], "date": ["2024-03-05"]})

    def get_stock_list(self, **kwargs) -> pd.DataFrame:
        return pd.DataFrame()

    def get_etf_list(self, **kwargs):
        raise ConnectionError("no route to host")

    def get_stock_screener(self, **kwargs):
        raise ValueError("Please enter an API key from FinancialModelingPrep")


def test_discovery_without_the_providers_key_is_unavailable_with_the_reason_FT_097() -> None:
    result = discover("biggest_gainers", as_of=when("2026-11-22"), key=lambda p: None, factory=FakeDiscovery, clock=lambda: when("2026-11-22T12:00:00"))
    assert (result.status, result.source, result.reason, result.value) == (sentinels.DATA_UNAVAILABLE, "fmp", sentinels.REASON_KEY_MISSING, None)


def test_discovery_lists_describe_today_and_calendars_have_dates_FT_097() -> None:
    kwargs = dict(key=lambda p: "FMP-SECRET", factory=FakeDiscovery, clock=lambda: when("2026-11-22T12:00:00"))
    today = discover("biggest_gainers", as_of=when("2026-11-22"), **kwargs)
    assert today.ok and today.source == "fmp" and today.value["symbol"].tolist() == ["AAA"] and today.received_at == when("2026-11-22T12:00:00")
    past = discover("biggest_gainers", as_of=when("2024-03-01"), **kwargs)
    assert (past.status, past.reason) == (sentinels.NO_DATA, sentinels.REASON_UNSUPPORTED) and "no vintage" in past.warnings[0]
    calendar = discover("ipo_calendar", as_of=when("2024-03-01"), **kwargs)
    assert calendar.ok and calendar.value["symbol"].tolist() == ["NEWCO"]                       # a dated calendar is served for a past date
    assert discover("stock_list", as_of=when("2026-11-22"), **kwargs).status == sentinels.NO_DATA


def test_discovery_failures_become_sentinels_and_never_leak_the_key_FT_097() -> None:
    kwargs = dict(key=lambda p: "FMP-SECRET", factory=FakeDiscovery, clock=lambda: when("2026-11-22T12:00:00"))
    down = discover("etf_list", as_of=when("2026-11-22"), **kwargs)
    assert (down.status, down.reason) == (sentinels.DATA_UNAVAILABLE, sentinels.REASON_UNREACHABLE) and "FMP-SECRET" not in str(down.to_dict())
    rejected = discover("screener", as_of=when("2026-11-22"), **kwargs)
    assert rejected.reason == sentinels.REASON_KEY_MISSING
    with pytest.raises(ValueError):
        discover("nonsense", as_of=when("2026-11-22"), **kwargs)


def test_the_real_library_has_every_discovery_getter_we_route_FT_097() -> None:
    assert set(DISCOVERY_KINDS.values()) <= _lib.public_methods("discovery")
    assert {"search_instruments", "screener", "biggest_gainers", "ipo_calendar", "splits_calendar", "mergers_acquisitions", "stock_news", "general_news"} <= set(DISCOVERY_KINDS)


# ------------------------------------------------------------------------------------------------ SK-118
def test_bundled_sample_data_is_cut_at_as_of_SK_118() -> None:
    full = sample("sp500", clock=lambda: when("2026-11-22T12:00:00"))
    assert full.ok and full.source == "sample_data" and full.value.shape == (8313, 20) and "sample_data_not_for_trading" in full.warnings
    assert str(full.value.index[0].date()) == "1990-01-02" and str(full.value.index[-1].date()) == "2022-12-28"
    cut = sample("sp500", as_of=when("2000-01-04"))
    assert str(cut.value.index[-1].date()) == "2000-01-04" and len(cut.value) < len(full.value) and cut.as_of == when("2000-01-04")
    assert sample("sp500", as_of=when("1980-01-01")).status == sentinels.NO_DATA
    assert sample("factors").ok and sample("sp500_index").ok
    with pytest.raises(ValueError):
        sample("nonsense")


def test_datasets_that_need_a_download_are_unavailable_without_it_SK_118(monkeypatch) -> None:
    def refuse(name, *, download=False):
        raise OSError("Data not found and `download_if_missing` is False")

    monkeypatch.setattr(universe_module._lib, "sample_dataset", refuse)
    result = sample("ftse100")
    assert (result.status, result.source, result.reason, result.value) == (sentinels.DATA_UNAVAILABLE, "sample_data", sentinels.REASON_UNREACHABLE, None)


def test_synthetic_panel_is_seeded_SK_118() -> None:
    a = _lib.synthetic_panel(n_assets=5, n_observations=30, random_state=7)
    b = _lib.synthetic_panel(n_assets=5, n_observations=30, random_state=7)
    assert type(a).__name__ == "AssetPanel" and repr(a) == repr(b)
