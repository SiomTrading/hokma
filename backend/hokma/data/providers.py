"""Provider contract, error taxonomy, exact-chain router and transport helpers (SPEC-017: AH-42, AH-44, OA-64, TA-52,
TA-54, TA-55, TA-56, TA-59, TA-60).

Rules that hold for every provider:
- an empty answer means the data does not exist; a failure raises one of the three errors below;
- the router tries exactly the providers that were configured, in order, and never falls to one that was not chosen;
- only successes are cached;
- a bar identifies its source: two feeds for the same symbol are two different series.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from hokma.data import sentinels
from hokma.data.cache import ResultCache, make_key
from hokma.data.sentinels import DataResult, utcnow

# ---------------------------------------------------------------------------------------------------- TA-54: errors


class ProviderError(Exception):
    """Base of every condition in which a provider could not return usable data."""


class NoDataError(ProviderError):
    """The provider answered and has no usable rows (empty result or stale rows). Another provider may have them."""

    def __init__(self, symbol: str, canonical: str | None = None, detail: str = "") -> None:
        self.symbol = symbol
        self.canonical = canonical or symbol
        self.detail = detail
        text = f"no data for {symbol!r}"
        if canonical and canonical != symbol:
            text += f" (queried as {canonical!r})"
        if detail:
            text += f": {detail}"
        super().__init__(text)


class RateLimitError(ProviderError):
    """The provider throttled the request or could not be reached. Says nothing about the instrument."""

    def __init__(self, message: str = "", *, reason: str = sentinels.REASON_RATE_LIMITED) -> None:
        self.reason = reason
        super().__init__(message or reason)


class NotConfiguredError(ProviderError, ValueError):
    """The provider was chosen but its key or configuration is missing."""

    def __init__(self, provider: str, message: str = "") -> None:
        self.provider = provider
        super().__init__(message or f"{provider}: key not configured")


# ---------------------------------------------------------------------------------------------------- AH-42: protocol
@runtime_checkable
class DataClient(Protocol):
    """Any object with a `name` and `fetch` is a provider; no inheritance needed.

    Contract: an empty list, an empty table or None means the data does not exist. Infrastructure failures (credentials,
    throttling, network, server errors) must raise one of the errors above: a provider that quietly returns empty on a
    failure turns a gap into "no signal".
    """

    name: str

    def fetch(self, category: str, **params: Any) -> Any: ...


def is_empty(value: Any) -> bool:
    if value is None:
        return True
    empty = getattr(value, "empty", None)
    if isinstance(empty, bool):
        return empty
    try:
        return len(value) == 0
    except TypeError:
        return False


# ---------------------------------------------------------------------------------------------------- AH-44: cache
class CachedDataClient:
    """Memoizes another client in a `ResultCache`. Only calls that returned are stored; a raised error is never cached
    and propagates unchanged."""

    def __init__(self, client: DataClient, cache: ResultCache, *, category: str = "bars", ttl_seconds: float = 3600.0,
                 refresh: bool = False) -> None:
        self._client = client
        self._cache = cache
        self._category = category
        self._ttl = ttl_seconds
        self._refresh = refresh
        self.name = client.name

    def fetch(self, category: str, **params: Any) -> Any:
        key = make_key(self._category, self.name, method=category, **params)
        if not self._refresh:
            hit = self._cache.get(self._category, key)
            if hit is not None:
                return hit.value
        value = self._client.fetch(category, **params)
        if value is not None:
            self._cache.put(self._category, key, value, source=self.name, ttl_seconds=self._ttl)
        return value


# ---------------------------------------------------------------------------------------------------- OA-64: bar ids
@dataclass(frozen=True)
class BarRef:
    """Identity of a bar series: the source and the symbol as that source spells it. `yfinance|AAPL` and `fmp|AAPL` are
    different series."""

    provider: str
    symbol: str

    def __str__(self) -> str:
        return f"{self.provider}|{self.symbol}"

    @staticmethod
    def parse(bar_id: str) -> "BarRef":
        provider, sep, symbol = str(bar_id).partition("|")
        if not sep or not provider.strip() or not symbol.strip():
            raise ValueError(f"a bar id looks like 'provider|symbol', got {bar_id!r}")
        return BarRef(provider.strip(), symbol.strip())


@dataclass
class RawBars:
    """What a bar source returns before the data module shapes it."""

    frame: Any                        # pandas DataFrame with open, high, low, close, volume and a time index
    received_at: datetime
    stale: bool = False               # served from an old cache because the network failed
    meta: dict = field(default_factory=dict)


BarFetcher = Callable[[str, str], RawBars]


class BarService:
    """Resolves a bar id to ONE source and fetches from it. A failing source is reported, never swapped for another."""

    def __init__(self, sources: Mapping[str, BarFetcher] | None = None) -> None:
        self._sources: dict[str, BarFetcher] = dict(sources or {})

    def register(self, provider: str, fetcher: BarFetcher) -> None:
        self._sources[provider] = fetcher

    def providers(self) -> list[str]:
        return sorted(self._sources)

    def fetch(self, bar_id: str | BarRef, interval: str) -> RawBars:
        ref = bar_id if isinstance(bar_id, BarRef) else BarRef.parse(bar_id)
        if ref.provider not in self._sources:
            raise NotConfiguredError(ref.provider, f"no bar source named {ref.provider!r}; known: {self.providers()}")
        return self._sources[ref.provider](ref.symbol, interval)


# ---------------------------------------------------------------------------------------------------- TA-56: config
OPTIONAL_CATEGORIES = frozenset({"macro", "prediction_markets", "social", "cot"})  # TA-55: flavour data

DEFAULT_CHAINS: dict[str, tuple[str, ...]] = {
    "bars": ("hokma_market",),
    "fundamentals": ("financial_datasets", "alpha_vantage"),
    "macro": ("fred",),
    "prediction_markets": ("polymarket",),
    "social": ("stocktwits", "reddit"),
    "cot": ("fmp",),
}


class ProviderConfig:
    """Provider chain per category, with an override per tool. A tool entry wins over its category entry. The value is
    the exact chain; `default` means every provider that serves the tool."""

    def __init__(self, categories: Mapping[str, str | Sequence[str]] | None = None,
                 tools: Mapping[str, str | Sequence[str]] | None = None) -> None:
        self.categories = {**DEFAULT_CHAINS, **dict(categories or {})}
        self.tools = dict(tools or {})

    @staticmethod
    def _names(value: str | Sequence[str]) -> list[str]:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return [str(part).strip() for part in value if str(part).strip()]

    def chain_for(self, category: str, tool: str | None = None, available: Iterable[str] = ()) -> tuple[str, ...]:
        raw = self.tools[tool] if tool is not None and tool in self.tools else self.categories.get(category, "default")
        names = self._names(raw)
        explicit = [name for name in names if name != "default"]
        if explicit:
            return tuple(explicit)
        return tuple(available)


# ---------------------------------------------------------------------------------------------------- TA-52, TA-55: router
def route(tool: str, impls: Mapping[str, Callable[..., Any]], chain: Sequence[str], *args: Any, category: str = "",
          optional: frozenset[str] = OPTIONAL_CATEGORIES, as_of: datetime | None = None,
          clock: Callable[[], datetime] = utcnow, **kwargs: Any) -> DataResult:
    """Call the providers of `chain` in order and return the first usable answer as a `DataResult`.

    Exactly the chain: a provider that is not in it is never tried. No rows from every provider is `NO_DATA`; a provider
    that was throttled, unreachable or without a key is `DATA_UNAVAILABLE` with its name and reason. An optional category
    (macro, prediction markets, social, positioning) degrades to `DATA_UNAVAILABLE` on any failure; for a core category an
    unexpected error is raised.
    """
    vendors = [name for name in chain if name in impls]
    if not vendors:
        raise ValueError(f"none of the configured providers {list(chain)} serves {tool!r}; available: {sorted(impls)}")
    last_no_data: tuple[str, str] | None = None
    last_unavailable: tuple[str, RateLimitError] | None = None
    key_missing: tuple[str, NotConfiguredError] | None = None
    first_error: tuple[str, Exception] | None = None
    for vendor in vendors:
        try:
            value = impls[vendor](*args, **kwargs)
        except RateLimitError as error:
            last_unavailable = (vendor, error)
            continue
        except NotConfiguredError as error:
            key_missing = key_missing or (vendor, error)
            continue
        except NoDataError as error:
            last_no_data = (vendor, error.detail or sentinels.REASON_EMPTY)
            continue
        except Exception as error:  # noqa: BLE001  one provider failing must not hide a provider that can serve
            first_error = first_error or (vendor, error)
            continue
        if is_empty(value):
            last_no_data = (vendor, sentinels.REASON_EMPTY)
            continue
        return sentinels.ok(value, source=vendor, as_of=as_of, received_at=clock())
    now = clock()
    if last_no_data is not None:
        return sentinels.no_data(last_no_data[0], last_no_data[1], as_of=as_of, received_at=now)
    if last_unavailable is not None:
        vendor, error = last_unavailable
        return sentinels.unavailable(vendor, error.reason, as_of=as_of, received_at=now, warnings=(str(error),))
    if key_missing is not None:
        return sentinels.unavailable(key_missing[0], sentinels.REASON_KEY_MISSING, as_of=as_of, received_at=now)
    if first_error is not None:
        vendor, error = first_error
        if category in optional or isinstance(error, ProviderError):
            return sentinels.unavailable(vendor, "optional_category_failed" if category in optional else "provider_error",
                                         as_of=as_of, received_at=now, warnings=(str(error),))
        raise error
    raise RuntimeError(f"no available provider for {tool!r}")


# ---------------------------------------------------------------------------------------------------- TA-59, TA-60
def _head_reachable(url: str, timeout: float) -> bool:
    try:
        urlopen(Request(url, method="HEAD"), timeout=timeout)  # noqa: S310  fixed https URL of a provider host
        return True
    except HTTPError:
        return True  # the host answered: it is reachable even if it refused the request
    except (URLError, OSError):
        return False


def vendor_reachable(url: str, timeout: float = 5.0, *, probe: Callable[[str, float], bool] = _head_reachable) -> bool:
    """Whether the provider host answers at all, for telling silence from an outage. Call it only when a result is empty."""
    return bool(probe(url, timeout))


def raise_for_empty(symbol: str, canonical: str, what: str, *, host: str,
                    reachable: Callable[[str], bool] = vendor_reachable) -> None:
    """Report an empty answer as an absence, or as an outage if the provider is down. Always raises."""
    if not reachable(host):
        raise RateLimitError(f"{host} is unreachable; no {what} was retrieved", reason=sentinels.REASON_UNREACHABLE)
    raise NoDataError(symbol, canonical, f"no {what}")


def retry_with_backoff(func: Callable[[], Any], *, max_retries: int = 3, base_delay: float = 2.0,
                       retry_on: tuple[type[Exception], ...] = (RateLimitError,),
                       sleep: Callable[[float], None] = time.sleep) -> Any:
    """Run `func`, retrying throttled calls with exponential backoff (base_delay, 2x, 4x, ...). Other errors propagate."""
    for attempt in range(max_retries + 1):
        try:
            return func()
        except retry_on:
            if attempt >= max_retries:
                raise
            sleep(base_delay * (2 ** attempt))
    raise RuntimeError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------------------------------- transport
Transport = Callable[[str, Mapping[str, Any], Mapping[str, str]], tuple[int, bytes]]
KeyResolver = Callable[[str], str | None]


def http_transport(url: str, params: Mapping[str, Any], headers: Mapping[str, str]) -> tuple[int, bytes]:
    """GET `url` with `params`. Returns (status, body); raises RateLimitError(provider_unreachable) when the network fails.
    Never used for prices: price bars come from `hokma.market` only."""
    query = urlencode({k: v for k, v in params.items() if v is not None})
    request = Request(f"{url}?{query}" if query else url, headers=dict(headers))
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310  https URL of a configured provider
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
    except (URLError, OSError) as error:
        raise RateLimitError(f"network failure: {type(error).__name__}", reason=sentinels.REASON_UNREACHABLE) from error


def connections_key(provider: str) -> str | None:
    """The key the student saved in Connections for `provider`, or None. Keys never leave this module in a result."""
    try:
        from hokma.connections import runtime

        service = runtime.get_service()
        if not service.loaded:
            service.load()
        return service.read_secret(provider)
    except Exception:  # noqa: BLE001  no connection store available: the provider counts as not configured
        return None


def check_status(provider: str, status: int, body: bytes = b"", *, secret: str | None = None) -> None:
    """Map an HTTP status to the taxonomy: 401 and 403 are a key problem, 429 a throttle, 5xx an outage."""
    if status < 400:
        return
    detail = sentinels.scrub(body[:200].decode("utf-8", "replace"), secret)
    if status in (401, 403):
        raise NotConfiguredError(provider, f"{provider} rejected the key ({status})")
    if status == 429:
        raise RateLimitError(f"{provider} throttled the request", reason=sentinels.REASON_RATE_LIMITED)
    if status >= 500:
        raise RateLimitError(f"{provider} server error {status}", reason=sentinels.REASON_UNREACHABLE)
    raise NoDataError(provider, None, f"request refused ({status}): {detail}")
