"""The client every SEC function receives: transport, identity, clock and small per-instance caches."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from hokma.data.sec import identity as identity_mod
from hokma.data.sec.index import FilingRef, parse_submissions
from hokma.data.sec.result import Clock, SecResult, unavailable, utc_now
from hokma.data.sec.transport import LiveTransport, SecTransport, TransportError


class SecClient:
    """Holds what a SEC call needs. `transport` is built from the saved identity unless one is given."""

    def __init__(self, transport: SecTransport | None = None, *, identity_reader: identity_mod.IdentityReader | None = None,
                 clock: Clock = utc_now) -> None:
        self._transport = transport
        self._reader = identity_reader
        self.now = clock
        self._refs: dict[int, list[FilingRef]] = {}
        self._facts: dict[int, dict[str, Any]] = {}
        self._table: Any = None
        self.cache: dict[Any, Any] = {}              # parsed objects other modules keep per client

    def identity(self) -> str | None:
        return identity_mod.resolve_identity(self._reader)

    def preflight(self, as_of: datetime | date | None) -> SecResult | None:
        """DATA_UNAVAILABLE when no identity is saved; None when the call may go on."""
        if self.identity() is None:
            return unavailable(identity_mod.MISSING_REASON, as_of, now=self.now)
        return None

    def fail(self, exc: TransportError, as_of: datetime | date | None) -> SecResult:
        return unavailable(f"sec_{exc.kind}: {exc.message}".rstrip(": "), as_of, now=self.now)

    @property
    def transport(self) -> SecTransport:
        if self._transport is None:
            identity = self.identity()
            if identity is None:
                raise TransportError("identity", identity_mod.MISSING_REASON)
            self._transport = LiveTransport(identity)
        return self._transport

    # ---- cached reads (raise TransportError) ----

    def ticker_table(self) -> Any:
        from hokma.data.sec.reference import TickerTable

        if self._table is None:
            self._table = TickerTable.from_payload(self.transport.company_tickers())
        return self._table

    def cik_of(self, symbol: str | int) -> int | None:
        if isinstance(symbol, int):
            return symbol
        text = str(symbol).strip()
        if text.isdigit():
            return int(text)
        return self.ticker_table().cik_for(text)

    def refs(self, cik: int) -> list[FilingRef]:
        if cik not in self._refs:
            payload = self.transport.submissions(cik)
            pages = [self.transport.submissions_file(f["name"]) for f in payload.get("filings", {}).get("files", []) if f.get("name")]
            self._refs[cik] = parse_submissions(payload, extra_pages=pages)
        return self._refs[cik]

    def facts(self, cik: int) -> dict[str, Any]:
        if cik not in self._facts:
            self._facts[cik] = self.transport.company_facts(cik)
        return self._facts[cik]

    def submission_text(self, cik: int, accession: str) -> str:
        return self.transport.submission_text(cik, accession)
