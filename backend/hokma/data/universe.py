"""Instrument universe: the first filter for screeners (SPEC-017: FD-001 to FD-009, CC-12, CC-13, FT-097, SK-118).

The catalog is read from seven compressed files, one per asset class (`CATALOG_FILES`), from a local folder or from a URL and
from no other source. It describes instruments as they are NOW (with their delisted flag), not as they were on a past date:
a query with a past `as_of` still runs but says so, because a universe built from today's listings carries survivorship
bias. Prices and fundamentals never come from here.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from hokma.data import _lib, sentinels
from hokma.data.providers import KeyResolver, NotConfiguredError, RateLimitError, connections_key
from hokma.data.sentinels import DataResult, parse_time, utcnow

# ---------------------------------------------------------------------------------------------------- FD-005, FD-004
CATALOG_FILES: dict[str, str] = dict(_lib.CATALOG_FILES)     # asset class -> compressed file, 7 entries
ASSET_CLASSES: tuple[str, ...] = tuple(CATALOG_FILES)

# FD-004: what each asset class is for in the plantel.
ASSET_CLASS_USE: dict[str, str] = {
    "equities": "long-term portfolios: stocks; stock options universe",
    "etfs": "long-term portfolios: funds traded on exchanges",
    "funds": "fixed income: funds",
    "indices": "CFD swing: index underlyings",
    "currencies": "CFD swing: currency pairs",
    "cryptos": "long-term portfolios: crypto held",
    "moneymarkets": "fixed income: money market funds",
}

# FD-006: the descriptor fields each asset class carries.
DESCRIPTOR_FIELDS: dict[str, tuple[str, ...]] = {
    "equities": ("symbol", "name", "sector", "industry_group", "industry", "country", "exchange", "market", "market_cap", "currency",
                 "isin", "cusip", "figi", "composite_figi", "shareclass_figi", "delisted"),
    "etfs": ("symbol", "name", "category_group", "category", "family", "exchange", "currency", "isin"),
    "funds": ("symbol", "name", "category_group", "category", "family", "exchange", "currency"),
    "indices": ("symbol", "name", "category_group", "category", "exchange", "currency"),
    "currencies": ("symbol", "name", "base_currency", "quote_currency"),
    "cryptos": ("symbol", "name", "cryptocurrency", "currency", "exchange"),
    "moneymarkets": ("symbol", "name", "family", "currency", "exchange"),
}


class Catalog:
    """FD-001: one asset class of the instrument catalog, loaded from `directory` (compressed files) or `base_url`.

    Loading from a URL needs the network; nothing is loaded from any other source.
    """

    def __init__(self, asset_class: str, *, directory: str | Path | None = None, base_url: str | None = None,
                 cache_dir: str | Path | None = None) -> None:
        if asset_class not in CATALOG_FILES:
            raise ValueError(f"unknown asset class {asset_class!r}; expected one of {ASSET_CLASSES}")
        self.asset_class = asset_class
        self._db = _lib.load_asset_class(asset_class, directory=Path(directory) if directory else None, base_url=base_url,
                                         cache_dir=Path(cache_dir) if cache_dir else None)

    @property
    def data(self) -> pd.DataFrame:
        return self._db.data

    def columns(self) -> list[str]:
        return list(self._db.get_columns())

    def search(self, **kwargs: Any) -> pd.DataFrame:
        """FD-002: rows whose columns match the queries (regular expressions; a list matches any of them). Delisted entries are
        excluded unless `exclude_delisted=False` is passed."""
        return self._db.search(**kwargs)

    def select(self, **kwargs: Any) -> pd.DataFrame:
        """Exact selection by the categorical columns of the class (country, sector, category, ...)."""
        select = getattr(self._db, "select", None)
        if select is None:
            raise ValueError(f"{self.asset_class} has no categorical selection; use search()")
        return select(**kwargs)

    def to_toolkit(self, frame: pd.DataFrame | None = None, **kwargs: Any) -> Any:
        """FD-003: hand the symbols of a result to the fundamentals toolkit (a `FinanceFrame` result from this catalog)."""
        return _lib.to_analysis_toolkit(self._db.data if frame is None else frame, **kwargs)


def load_catalogs(*, directory: str | Path | None = None, base_url: str | None = None, cache_dir: str | Path | None = None,
                  asset_classes: tuple[str, ...] = ASSET_CLASSES) -> dict[str, Catalog]:
    """CC-12: the catalogs of the asset classes asked, from the seven compressed files of a folder or a URL."""
    return {name: Catalog(name, directory=directory, base_url=base_url, cache_dir=cache_dir) for name in asset_classes}


def describe_files(directory: str | Path) -> dict[str, dict[str, Any]]:
    """FD-005: which of the seven files a folder holds and their size in bytes."""
    root = Path(directory)
    return {name: {"file": file, "present": (root / file).is_file(), "bytes": (root / file).stat().st_size if (root / file).is_file() else 0}
            for name, file in CATALOG_FILES.items()}


def universe(*, asset_class: str = "equities", directory: str | Path | None = None, base_url: str | None = None,
             cache_dir: str | Path | None = None, as_of: datetime | None = None, clock: Callable[[], datetime] = utcnow,
             **filters: Any) -> DataResult:
    """The first universe filter: instruments of an asset class that match `filters` (columns and regular expressions).

    No rows: `NO_DATA`. The files cannot be loaded (no network for a URL, a missing file): `DATA_UNAVAILABLE`. The value is a
    DataFrame indexed by symbol. A past `as_of` adds a survivorship warning: the catalog is today's.
    """
    now = clock()
    cut = parse_time(as_of) if as_of is not None else None
    warnings: list[str] = ["catalog_is_current_not_point_in_time"]
    if cut is not None and cut.date() < now.date():
        warnings.append("past_as_of_with_todays_listings_survivorship_risk")
        if filters.get("exclude_delisted", True) in (True, "True"):
            warnings.append("delisted_instruments_excluded")
    try:
        catalog = Catalog(asset_class, directory=directory, base_url=base_url, cache_dir=cache_dir)
        frame = catalog.search(**filters)
    except (OSError, ValueError) as error:
        if isinstance(error, ValueError) and "unknown asset class" in str(error):
            raise
        return sentinels.unavailable("catalog", sentinels.REASON_UNREACHABLE, as_of=cut, received_at=now, warnings=(str(error)[:200],))
    if len(frame) == 0:
        return sentinels.no_data("catalog", sentinels.REASON_EMPTY, as_of=cut, received_at=now, warnings=tuple(warnings))
    return sentinels.ok(pd.DataFrame(frame), source="catalog", as_of=cut, received_at=now, warnings=tuple(warnings),
                        meta={"asset_class": asset_class, "rows": int(len(frame))})


# ---------------------------------------------------------------------------------------------------- FD-009
def compress_catalog(frame: pd.DataFrame, path: str | Path) -> Path:
    """How a catalog file is made: the table as CSV, symbol first, compressed with bz2. The same format the loader reads."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    table = frame.reset_index() if frame.index.name == "symbol" else frame
    if "symbol" not in table.columns:
        raise ValueError("a catalog table needs a symbol column")
    ordered = ["symbol", *[c for c in table.columns if c != "symbol"]]
    table[ordered].to_csv(target, index=False, compression="bz2")
    return target


# ---------------------------------------------------------------------------------------------------- FD-007, CC-13
def _value(char: str) -> int:
    return int(char) if char.isdigit() else ord(char) - 55


def _double_add(values: list[int]) -> int:
    total = 0
    for position, value in enumerate(values):
        if position % 2 == 1:
            value *= 2
        total += value // 10 + value % 10
    return (10 - total % 10) % 10


def isin_check_digit(body: str) -> int:
    digits = "".join(str(_value(c)) for c in body)
    total = 0
    for position, char in enumerate(reversed(digits)):
        digit = int(char) * (2 if position % 2 == 0 else 1)
        total += digit - 9 if digit > 9 else digit
    return (10 - total % 10) % 10


def cusip_check_digit(body: str) -> int:
    return _double_add([36 if c == "*" else 37 if c == "@" else 38 if c == "#" else _value(c) for c in body])


def figi_check_digit(body: str) -> int:
    return _double_add([_value(c) for c in body])


_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
_CUSIP = re.compile(r"^[A-Z0-9*@#]{8}\d$")
_FIGI = re.compile(r"^[B-DF-HJ-NP-TV-Z]{2}G[B-DF-HJ-NP-TV-Z0-9]{8}\d$")
_FIGI_FORBIDDEN_PREFIX = {"BS", "BM", "GG", "GB", "GH", "KY", "VG"}


def validate_isin(text: str) -> bool:
    value = str(text)
    return bool(_ISIN.match(value)) and isin_check_digit(value[:-1]) == int(value[-1])


def validate_cusip(text: str) -> bool:
    value = str(text)
    return bool(_CUSIP.match(value)) and cusip_check_digit(value[:-1]) == int(value[-1])


def validate_figi(text: str) -> bool:
    value = str(text)
    return bool(_FIGI.match(value)) and value[:2] not in _FIGI_FORBIDDEN_PREFIX and figi_check_digit(value[:-1]) == int(value[-1])


VALIDATORS: dict[str, Callable[[str], bool]] = {"isin": validate_isin, "cusip": validate_cusip, "figi": validate_figi}
_BODY_LENGTH = {"isin": 11, "cusip": 8, "figi": 11}
_CHECKERS = {"isin": isin_check_digit, "cusip": cusip_check_digit, "figi": figi_check_digit}


def identifier_kind(text: str) -> str | None:
    """The kind whose check passes: `figi`, `cusip` or `isin` (the most specific format first, since a 12 character FIGI can also pass the ISIN check by chance); None when none does."""
    value = normalise_identifier(text)
    return next((kind for kind in ("figi", "cusip", "isin") if VALIDATORS[kind](value)), None)    # the most specific format first


def normalise_identifier(text: str) -> str:
    """Upper case, without spaces or hyphens."""
    return re.sub(r"[\s-]", "", str(text)).upper()


def repair_identifier(kind: str, text: str) -> tuple[str | None, str]:
    """Return (identifier, note). Repairs only what is certain: case, spaces and hyphens, and a MISSING check digit. A wrong
    check digit is not corrected (it may hide a typo): the answer is None with the reason."""
    if kind not in VALIDATORS:
        raise ValueError(f"unknown identifier kind {kind!r}; expected one of {sorted(VALIDATORS)}")
    value = normalise_identifier(text)
    if VALIDATORS[kind](value):
        return value, "valid" if value == str(text) else "normalised"
    if len(value) == _BODY_LENGTH[kind]:
        candidate = value + str(_CHECKERS[kind](value))
        if VALIDATORS[kind](candidate):
            return candidate, "check_digit_added"
    return None, "invalid_check_digit_or_format"


def reconcile_identifiers(frame: pd.DataFrame) -> pd.DataFrame:
    """Check the `isin`, `cusip` and `figi` columns of a catalog result. One row per symbol and identifier that is present, with
    whether it is valid and what repair is possible."""
    rows = []
    for symbol, row in frame.iterrows():
        for kind in VALIDATORS:
            raw = row.get(kind)
            if raw is None or (isinstance(raw, float) and pd.isna(raw)) or str(raw).strip() == "":
                continue
            fixed, note = repair_identifier(kind, str(raw))
            rows.append({"symbol": symbol, "kind": kind, "value": str(raw), "valid": VALIDATORS[kind](str(raw)),
                         "repaired": fixed if fixed != str(raw) else None, "note": note})
    return pd.DataFrame(rows, columns=["symbol", "kind", "value", "valid", "repaired", "note"])


# ---------------------------------------------------------------------------------------------------- FD-008
def recipe() -> list[dict[str, str]]:
    """The getting-started recipe, in order. Each step names a function of this module."""
    return [
        {"step": "1", "call": "describe_files", "what": "check that the seven compressed files are in the folder"},
        {"step": "2", "call": "load_catalogs", "what": "load the catalogs from the folder or from a URL"},
        {"step": "3", "call": "universe", "what": "filter one asset class, for example equities by sector and country"},
        {"step": "4", "call": "reconcile_identifiers", "what": "check the identifiers of the result before using them to join other data"},
        {"step": "5", "call": "Catalog.to_toolkit", "what": "hand the symbols found to the fundamentals toolkit"},
    ]


# ---------------------------------------------------------------------------------------------------- FT-097
DISCOVERY_KINDS: dict[str, str] = {
    "search_instruments": "search_instruments", "screener": "get_stock_screener", "stock_list": "get_stock_list",
    "sectors_performance": "get_sectors_performance", "biggest_gainers": "get_biggest_gainers", "biggest_losers": "get_biggest_losers",
    "most_active": "get_most_active_stocks", "etf_list": "get_etf_list", "index_list": "get_index_list",
    "index_constituents": "get_index_constituents", "ipo_calendar": "get_ipo_calendar", "splits_calendar": "get_stock_splits_calendar",
    "mergers_acquisitions": "get_mergers_acquisitions_latest", "stock_news": "get_stock_news", "general_news": "get_general_news",
    "earnings_calendar": "get_earnings_calendar",
}
DISCOVERY_LIVE_ONLY = frozenset(set(DISCOVERY_KINDS) - {"ipo_calendar", "splits_calendar", "earnings_calendar", "stock_news"})


def discover(kind: str, *, as_of: datetime, key: KeyResolver = connections_key, factory: Callable[..., Any] | None = None,
             clock: Callable[[], datetime] = utcnow, **kwargs: Any) -> DataResult:
    """Discovery of instruments, lists, calendars and news through the paid provider `fmp`. Without its key:
    `DATA_UNAVAILABLE` with `provider_key_missing`. Lists and rankings describe today and are withheld for a past `as_of`."""
    cut, now = parse_time(as_of), clock()
    if kind not in DISCOVERY_KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {sorted(DISCOVERY_KINDS)}")
    secret = key("fmp")
    if not secret:
        return sentinels.unavailable("fmp", sentinels.REASON_KEY_MISSING, as_of=cut, received_at=now)
    if kind in DISCOVERY_LIVE_ONLY and cut.date() < now.date():
        return sentinels.no_data("fmp", sentinels.REASON_UNSUPPORTED, as_of=cut, received_at=now,
                                 warnings=(f"{kind} describes the present and has no vintage",))
    try:
        frame = getattr((factory or _lib.discovery)(secret), DISCOVERY_KINDS[kind])(**kwargs)
    except ValueError as error:
        if "api key" in str(error).lower():
            return sentinels.unavailable("fmp", sentinels.REASON_KEY_MISSING, as_of=cut, received_at=now)
        raise
    except OSError as error:
        return sentinels.unavailable("fmp", sentinels.REASON_UNREACHABLE, as_of=cut, received_at=now,
                                     warnings=(sentinels.scrub(str(error), secret)[:200],))
    if frame is None or len(frame) == 0:
        return sentinels.no_data("fmp", sentinels.REASON_EMPTY, as_of=cut, received_at=now)
    return sentinels.ok(frame, source="fmp", as_of=cut, received_at=now)


# ---------------------------------------------------------------------------------------------------- SK-118
SAMPLE_DATASETS = ("sp500", "sp500_index", "factors", "ftse100", "nasdaq", "sp500_implied_vol")


def sample(name: str, *, as_of: datetime | None = None, download: bool = False, clock: Callable[[], datetime] = utcnow) -> DataResult:
    """Sample datasets for examples and tests, with no provider (S&P 500 prices, factors, FTSE 100, Nasdaq, implied
    volatility). They are stale and not for trading. `sp500`, `sp500_index` and `factors` are bundled; the others are downloaded
    once when `download=True`, otherwise `DATA_UNAVAILABLE`. Rows after `as_of` are cut."""
    now = clock()
    cut = parse_time(as_of) if as_of is not None else None
    if name not in SAMPLE_DATASETS:
        raise ValueError(f"unknown sample dataset {name!r}; expected one of {SAMPLE_DATASETS}")
    try:
        frame = _lib.sample_dataset(name, download=download)
    except OSError as error:
        return sentinels.unavailable("sample_data", sentinels.REASON_UNREACHABLE, as_of=cut, received_at=now, warnings=(str(error)[:200],))
    if cut is not None:
        index = pd.DatetimeIndex(frame.index)
        frame = frame[index.tz_localize(None) <= pd.Timestamp(cut).tz_localize(None)]
    if len(frame) == 0:
        return sentinels.no_data("sample_data", "no_rows_by_as_of", as_of=cut, received_at=now)
    return sentinels.ok(frame, source="sample_data", as_of=cut, received_at=now, warnings=("sample_data_not_for_trading",))


sentinels.make_module_callable(__name__, "universe")
