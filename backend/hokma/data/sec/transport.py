"""How the SEC module reaches the SEC: a small protocol, the live implementation, and the errors both share.

The live implementation is the only code that makes network calls. Tests give the module a transport that replays
recorded responses, so nothing here is reached from a test over the network.
"""
from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.parse import urlencode

from hokma.data import _lib

DATA_HOST = "https://data.sec.gov"
WWW_HOST = "https://www.sec.gov"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

KIND_IDENTITY = "identity"
KIND_RATE_LIMITED = "rate_limited"
KIND_NOT_FOUND = "not_found"
KIND_NETWORK = "network"


class TransportError(Exception):
    """The SEC could not be asked or did not answer. `kind` is one of identity, rate_limited, not_found, network."""

    def __init__(self, kind: str, message: str = "") -> None:
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}" if message else kind)


class SecTransport(Protocol):
    def submissions(self, cik: int) -> dict[str, Any]: ...
    def submissions_file(self, name: str) -> dict[str, Any]: ...
    def company_tickers(self) -> dict[str, Any]: ...
    def company_facts(self, cik: int) -> dict[str, Any]: ...
    def submission_text(self, cik: int, accession: str) -> str: ...
    def current_feed(self, form: str, count: int) -> bytes: ...
    def full_text_search(self, query: str, *, forms: tuple[str, ...], cik: int | None, start: str | None, end: str | None) -> dict[str, Any]: ...


def submissions_url(cik: int) -> str:
    return f"{DATA_HOST}/submissions/CIK{int(cik):010d}.json"


def company_facts_url(cik: int) -> str:
    return f"{DATA_HOST}/api/xbrl/companyfacts/CIK{int(cik):010d}.json"


def submission_text_url(cik: int, accession: str) -> str:
    return f"{WWW_HOST}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}.txt"


def efts_url(query: str, *, forms: tuple[str, ...], cik: int | None, start: str | None, end: str | None) -> str:
    params: dict[str, str] = {"q": query}
    if forms:
        params["forms"] = ",".join(forms)
    if cik is not None:
        params["ciks"] = f"{int(cik):010d}"
    if start or end:
        params["dateRange"] = "custom"
        if start:
            params["startdt"] = start
        if end:
            params["enddt"] = end
    return f"{EFTS_URL}?{urlencode(params)}"


class LiveTransport:
    """Asks the SEC with the identity given at construction. Every failure becomes a TransportError."""

    def __init__(self, identity: str) -> None:
        self._identity = identity

    def _text(self, url: str) -> str:
        try:
            return _lib.sec_download_text(url, self._identity)
        except _lib.SecFetchError as exc:
            raise TransportError(exc.kind, str(exc)) from exc

    def _json(self, url: str) -> dict[str, Any]:
        text = self._text(url)
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise TransportError(KIND_NETWORK, "the response is not JSON") from exc
        if not isinstance(data, dict):
            raise TransportError(KIND_NETWORK, "the response is not a JSON object")
        return data

    def submissions(self, cik: int) -> dict[str, Any]:
        return self._json(submissions_url(cik))

    def submissions_file(self, name: str) -> dict[str, Any]:
        return self._json(f"{DATA_HOST}/submissions/{name}")

    def company_tickers(self) -> dict[str, Any]:
        return self._json(f"{WWW_HOST}/files/company_tickers.json")

    def company_facts(self, cik: int) -> dict[str, Any]:
        return self._json(company_facts_url(cik))

    def submission_text(self, cik: int, accession: str) -> str:
        return self._text(submission_text_url(cik, accession))

    def current_feed(self, form: str, count: int) -> bytes:
        return self._text(_lib.sec_current_feed_url(form, count)).encode("utf-8")

    def full_text_search(self, query: str, *, forms: tuple[str, ...], cik: int | None, start: str | None, end: str | None) -> dict[str, Any]:
        return self._json(efts_url(query, forms=forms, cik=cik, start=start, end=end))
