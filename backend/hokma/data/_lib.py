"""The only file of the data module that imports quantitative or data libraries (SPEC-017, rule 4).

Every other file reaches those libraries through this one. A scan test fails if any other file imports them.
"""


# ---------------------------------------------------------------------------------------------------------------------
# SEC EDGAR section (module hokma.data.sec). Library handles are imported lazily inside each function.
# ---------------------------------------------------------------------------------------------------------------------
from typing import Any


class SecFetchError(Exception):
    """A request to the SEC failed. `kind` is one of identity, rate_limited, not_found, network."""

    def __init__(self, kind: str, message: str = "") -> None:
        self.kind = kind
        super().__init__(message or kind)


def _sec_kind(exc: BaseException) -> str:
    names = {c.__name__ for c in type(exc).__mro__}
    if names & {"IdentityError", "IdentityNotSetError", "SECIdentityError"}:
        return "identity"
    if "TooManyRequestsError" in names:
        return "rate_limited"
    if names & {"NotFoundError", "FileNotFoundError"}:
        return "not_found"
    text = str(exc)
    if "404" in text:
        return "not_found"
    if "429" in text:
        return "rate_limited"
    return "network"


def sec_set_identity(identity: str) -> None:
    """Declares who is calling the SEC. Called before every request."""
    import edgar

    edgar.set_identity(identity)


def sec_download_text(url: str, identity: str) -> str:
    """GET a SEC url as text with the declared identity. Failures raise SecFetchError."""
    import edgar
    from edgar import httprequests

    try:
        edgar.set_identity(identity)
        content = httprequests.download_file(url, as_text=True)
    except Exception as exc:                             # noqa: BLE001  (every failure is classified)
        raise SecFetchError(_sec_kind(exc), f"{type(exc).__name__}: {exc}"[:300]) from exc
    if content is None:
        raise SecFetchError("not_found", "empty response")
    return content if isinstance(content, str) else content.decode("utf-8", errors="replace")


def sec_parse_current_feed(content: bytes) -> list[dict]:
    """Parses the Atom feed of the live filing stream into plain dicts (form, company, cik, filing_date, accession, accepted)."""
    from datetime import datetime as _dt

    from edgar.current_filings import _parse_feed, parse_summary, parse_title
    from edgar.xmltools import child_text, find_all_elements

    feed = _parse_feed(content)
    entries: list[dict] = []
    for entry in find_all_elements(feed, "entry") if feed is not None else []:
        form_type, company, cik, _status = parse_title(child_text(entry, "title"))
        filing_date, accession = parse_summary(child_text(entry, "summary"))
        entries.append({"form": form_type, "company": company, "cik": int(cik), "filing_date": filing_date,
                        "accession": accession, "accepted": _dt.fromisoformat(child_text(entry, "updated"))})
    return entries


def sec_current_feed_url(form: str, count: int) -> str:
    from edgar.current_filings import get_current_url

    return get_current_url(atom=True, count=count, start=0, form=form, owner="include")


def sec_describe_form(form: str) -> str:
    """One line description of a SEC form type, empty when the form is unknown."""
    from edgar.reference.forms import describe_form

    try:
        text = describe_form(form, prepend_form=False)
    except Exception:                                    # noqa: BLE001  (an unknown form has no description)
        return ""
    return "" if text.strip().lower() == f"form {form}".lower() else text


def sec_form_d(xml: str) -> Any:
    """Typed Form D (exempt offering notice) from its XML."""
    from edgar.offerings import FormD

    return FormD.from_xml(xml)


def sec_form_c(xml: str) -> Any:
    """Typed Form C (crowdfunding) from its XML."""
    from edgar.offerings import FormC

    return FormC.from_xml(xml)


class SecTtmError(Exception):
    """No gap free four quarter window exists for the request."""


def sec_entity_facts(payload: dict) -> Any:
    """Typed entity facts from a company facts payload (None when nothing could be parsed)."""
    from edgar.entity.parser import EntityFactsParser

    return EntityFactsParser.parse_company_facts(payload)


def sec_concept_facts(entity_facts: Any, concept: str) -> list:
    """All facts of one concept (for example `us-gaap:Revenues`)."""
    return list(entity_facts.query().by_concept(concept).execute())


def sec_fact(entity_facts: Any, concept: str, period: str | None = None) -> Any:
    """One fact of a concept for a period such as `2023-FY` or `2024-Q1`, or None."""
    import warnings as _warnings

    try:
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            return entity_facts.get_fact(concept, period)
    except Exception:                                    # noqa: BLE001  (a concept that is not reported has no fact)
        return None


def sec_standard_value(entity_facts: Any, field: str, period: str | None = None, *, annual: bool = True) -> float | None:
    """Value of a standardized statement field (revenue, net_income, gross_profit, operating_income, total_assets,
    total_liabilities, shareholders_equity) for a period, or None."""
    getters = {"revenue": "get_revenue", "net_income": "get_net_income", "gross_profit": "get_gross_profit",
               "operating_income": "get_operating_income", "total_assets": "get_total_assets",
               "total_liabilities": "get_total_liabilities", "shareholders_equity": "get_shareholders_equity"}
    import warnings as _warnings

    method = getattr(entity_facts, getters[field])
    try:
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            if field in {"revenue", "net_income"}:
                return method(period=period, annual=annual)
            return method(period=period)
    except Exception:                                    # noqa: BLE001  (a field the filer does not report has no value)
        return None


def sec_ttm(facts: list, *, as_of: Any, filed_by: Any, split_adjust: bool = False) -> dict:
    """Trailing twelve months of one concept from facts filed on or before `filed_by`.

    Returns value, unit, window end, the fiscal periods used and flags. Raises SecTtmError when there is no gap free window.
    With `split_adjust`, per share and share count facts are put on the post split basis first and the splits are returned.
    """
    from edgar.exceptions import ValidationError
    from edgar.ttm.calculator import STALE_THRESHOLD_DAYS, TTMCalculator
    from edgar.ttm.splits import apply_split_adjustments, detect_splits

    splits: list = []
    if split_adjust:
        splits = detect_splits(facts)
        facts = [f for f in facts if "StockSplitConversionRatio" not in f.concept]
        facts = apply_split_adjustments(facts, splits)
    try:
        metric = TTMCalculator(facts).calculate_ttm(as_of=as_of, filed_by=filed_by)
    except (ValidationError, ValueError) as exc:
        raise SecTtmError(str(exc).splitlines()[0]) from exc
    return {"value": metric.value, "unit": metric.unit, "window_end": metric.as_of_date, "periods": list(metric.periods),
            "has_gaps": metric.has_gaps, "has_calculated_q4": metric.has_calculated_q4, "is_stale": metric.is_stale,
            "warning": metric.warning, "stale_threshold_days": STALE_THRESHOLD_DAYS, "concept": metric.concept,
            "splits": [{"date": s["date"], "ratio": float(s["ratio"])} for s in splits]}


def sec_parse_ownership(form: str, xml: str) -> Any:
    """Typed Form 3, 4 or 5 from its XML. The parser looks the reporting owner up on the SEC site to decide whether it is a
    company; that lookup is switched off here, so parsing never touches the network and names stay as written in the filing."""
    import edgar
    import edgar.ownership.owners as owners

    class _OfflineEntity:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __bool__(self) -> bool:
            return False

    parsers = {"3": edgar.Form3, "4": edgar.Form4, "5": edgar.Form5}
    previous = owners.Entity
    owners.Entity = _OfflineEntity
    try:
        return parsers[form].parse_xml(xml)
    finally:
        owners.Entity = previous


def sec_parse_form144(xml: str) -> dict:
    """Plain dict of a Form 144 (notice of proposed sale) from its XML."""
    from edgar import Form144

    return Form144.parse_xml(xml)


def sec_parse_schedule13(kind: str, xml: str) -> dict:
    """Plain dict of a Schedule 13D or 13G from its XML. `kind` is `13D` or `13G`."""
    from edgar import Schedule13D, Schedule13G

    return (Schedule13D if kind == "13D" else Schedule13G).parse_xml(xml)


def sec_thirteenf(text: str) -> Any:
    """Typed 13F holdings report from a full submission text, or None when the filing is not a holdings report."""
    from edgar import Filing

    typed = Filing.from_sgml_text(text).obj()
    return typed if type(typed).__name__ == "ThirteenF" else None


def sec_resolve_13f_unit(frame: Any, schema_version: str | None, report_period: Any, override: str | None = None) -> Any:
    """Which unit (dollars or thousands) the values of a 13F table use, with the evidence behind the choice."""
    from edgar.thirteenf.units import resolve_value_unit

    return resolve_value_unit(frame, schema_version, report_period, override=override)


def sec_xbrl_instance(xml: str) -> Any:
    """Typed XBRL object of an instance document (the library reads it from a file, so the text goes through a temporary one)."""
    import tempfile
    from pathlib import Path

    from edgar.xbrl import XBRL

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "instance.xml"
        path.write_text(xml, encoding="utf-8")
        return XBRL.from_files(instance_file=path)


def sec_parse_nport(xml: str) -> dict:
    """Plain parts (header, general_info, fund_info, investments) of an N-PORT report from its XML."""
    from edgar import FundReport

    return FundReport.parse_fund_xml(xml)


def sec_parse_money_market(xml: str) -> Any:
    """Typed money market fund report (N-MFP3) from its XML."""
    from edgar import MoneyMarketFund

    return MoneyMarketFund.parse_nmfp3_xml(xml)


def sec_parse_proxy_votes(xml: str) -> Any:
    """Typed vote table of an N-PX filing from its XML."""
    from edgar.npx import ProxyVoteTableExtractor

    return ProxyVoteTableExtractor(xml.encode("utf-8")).extract()


def sec_typed_filing(text: str) -> Any:
    """The typed object the filing library builds for a full submission text (N-CEN, N-CSR, 497K, ATS-N and others)."""
    from edgar import Filing

    return Filing.from_sgml_text(text).obj()


def sec_parse_efts_hit(hit: dict) -> dict:
    """One hit of the full text search answer as a plain dict."""
    from edgar.search.efts import _parse_hit

    found = _parse_hit(hit)
    return {"accession": found.accession_number, "form": found.form, "filed": found.filed, "company": found.company, "cik": found.cik,
            "period": found.period, "file_type": found.file_type, "file_description": found.file_description,
            "document_id": found.document_id, "items": list(found.items or []), "score": found.score}


def sec_grep_text(text: str, pattern: str, location: str, regex: bool = False, context_chars: int = 100) -> list[dict]:
    """Case insensitive search of a text. Matches come with their location and the text around them."""
    from edgar.search.grep import _grep_text

    return [{"location": m.location, "match": m.match, "context": m.context} for m in _grep_text(text, pattern, location, regex, context_chars)]


def sec_library_storage_info(root: str) -> dict:
    """Points the filing library's local storage at `root` and reports what it holds."""
    import os

    import edgar

    os.makedirs(root, exist_ok=True)
    edgar.set_local_storage_path(root)
    info = edgar.storage_info(force_refresh=True)
    return {"storage_path": str(info.storage_path), "file_count": info.file_count, "filing_count": info.filing_count,
            "total_size_bytes": info.total_size_bytes, "by_form": dict(info.by_form), "by_year": dict(info.by_year)}

# --- end of SEC section (append SEC helpers above this line) ---


# ---------------------------------------------------------------------------------------------------------------------
# Instrument catalog (SVC-data, universe). Imports are lazy so that importing hokma.data stays cheap.
# ---------------------------------------------------------------------------------------------------------------------
import os
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ASSET_CLASSES = ("equities", "etfs", "funds", "indices", "currencies", "cryptos", "moneymarkets")
CATALOG_FILES = {name: f"{name}.bz2" for name in ASSET_CLASSES}


@contextmanager
def _catalog_environment(directory: Path | None, cache_dir: Path | None):
    """Point the catalog loader at a local folder of compressed files and at a cache folder, then restore it."""
    from financedatabase import database_controller

    previous_path = database_controller.COMPRESSION_PATH
    previous_cache = os.environ.get("FINANCEDATABASE_CACHE_DIR")
    try:
        if directory is not None:
            database_controller.COMPRESSION_PATH = Path(directory)
        if cache_dir is not None:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            os.environ["FINANCEDATABASE_CACHE_DIR"] = str(cache_dir)
        yield
    finally:
        database_controller.COMPRESSION_PATH = previous_path
        if previous_cache is None:
            os.environ.pop("FINANCEDATABASE_CACHE_DIR", None)
        else:
            os.environ["FINANCEDATABASE_CACHE_DIR"] = previous_cache


def load_asset_class(asset_class: str, *, directory: Path | None = None, base_url: str | None = None,
                     cache_dir: Path | None = None) -> Any:
    """The catalog object of one asset class, read from compressed files in `directory` or from `base_url`."""
    import financedatabase as catalog

    classes = {
        "equities": catalog.Equities, "etfs": catalog.ETFs, "funds": catalog.Funds, "indices": catalog.Indices,
        "currencies": catalog.Currencies, "cryptos": catalog.Cryptos, "moneymarkets": catalog.Moneymarkets,
    }
    if asset_class not in classes:
        raise ValueError(f"unknown asset class {asset_class!r}; expected one of {sorted(classes)}")
    with _catalog_environment(directory, cache_dir), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if directory is not None:
            return classes[asset_class](use_local_location=True)
        if base_url is not None:
            return classes[asset_class](base_url=base_url)
        return classes[asset_class]()


def to_analysis_toolkit(frame: Any, **kwargs: Any) -> Any:
    """Hand the symbols of a catalog frame to the fundamentals toolkit."""
    return frame.to_toolkit(**kwargs)


def sample_dataset(name: str, *, download: bool = False) -> Any:
    """One of the sample datasets of the portfolio library. `sp500`, `sp500_index` and `factors` ship with the package;
    the others are fetched once and cached, which only happens when `download` is true."""
    from skfolio import datasets

    bundled = {
        "sp500": datasets.load_sp500_dataset,
        "sp500_index": datasets.load_sp500_index,
        "factors": datasets.load_factors_dataset,
    }
    fetched = {
        "ftse100": datasets.load_ftse100_dataset,
        "nasdaq": datasets.load_nasdaq_dataset,
        "sp500_implied_vol": datasets.load_sp500_implied_vol_dataset,
    }
    if name in bundled:
        return bundled[name]()
    if name in fetched:
        return fetched[name](download_if_missing=download)
    raise ValueError(f"unknown sample dataset {name!r}; expected one of {sorted([*bundled, *fetched])}")


def synthetic_panel(**kwargs: Any) -> Any:
    """A synthetic asset panel (prices, returns and characteristics) of the portfolio library; seed it with random_state."""
    from skfolio.datasets import make_synthetic_characteristics

    return make_synthetic_characteristics(**kwargs)


# ---------------------------------------------------------------------------------------------------------------------
# Macro, fixed income and discovery (SVC-data, macro and universe).
# ---------------------------------------------------------------------------------------------------------------------
def economics(**kwargs: Any) -> Any:
    from financetoolkit import Economics

    return Economics(**kwargs)


def fixed_income(**kwargs: Any) -> Any:
    from financetoolkit import FixedIncome

    return FixedIncome(**kwargs)


def discovery(api_key: str, **kwargs: Any) -> Any:
    from financetoolkit import Discovery

    return Discovery(api_key=api_key, **kwargs)


def public_methods(kind: str) -> frozenset[str]:
    """Names of the public getters and searches of the `economics`, `fixed_income` or `discovery` classes."""
    import financetoolkit

    classes = {"economics": financetoolkit.Economics, "fixed_income": financetoolkit.FixedIncome,
               "discovery": financetoolkit.Discovery}
    cls = classes[kind]
    return frozenset(name for name in dir(cls) if name.startswith(("get_", "search_")) and callable(getattr(cls, name)))


# ---------------------------------------------------------------------------------------------------------------------
# News feed section (module hokma.data.news). The library is imported lazily so that importing the data package stays light.
# ---------------------------------------------------------------------------------------------------------------------
def news_headlines(query: str, count: int = 30) -> list[dict]:
    """Latest headlines of a symbol or a search text from the free feed. Failures propagate to the caller, which classifies them."""
    import yfinance as feed

    return list(feed.Ticker(query).get_news(count=count) or [])
