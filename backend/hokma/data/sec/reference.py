"""Reference data: ticker to CIK table and the SEC form catalog (ED-018)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from hokma.data import _lib
from hokma.data.sec import result
from hokma.data.sec.client import SecClient
from hokma.data.sec.transport import TransportError

FAMILIES: dict[str, tuple[str, ...]] = {
    "periodic_report": ("10-K", "10-Q", "20-F", "40-F", "6-K", "10-KT", "10-QT", "11-K"),
    "current_report": ("8-K",),
    "insider": ("3", "4", "5", "144"),
    "beneficial_ownership": ("SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G"),
    "institutional_holdings": ("13F-HR", "13F-NT"),
    "offering": ("D", "S-1", "S-3", "S-4", "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8", "DRS", "C", "EFFECT"),
    "fund": ("NPORT-P", "N-CEN", "N-CSR", "N-CSRS", "N-MFP", "N-MFP2", "N-MFP3", "497K"),
    "proxy": ("DEF 14A", "DEFA14A", "PRE 14A", "N-PX"),
}


@dataclass(frozen=True)
class TickerEntry:
    cik: int
    ticker: str
    name: str


def _norm(ticker: str) -> str:
    return ticker.strip().upper().replace("-", ".")


class TickerTable:
    """The SEC ticker table: case insensitive, `BRK-B` and `BRK.B` are the same ticker."""

    def __init__(self, entries: list[TickerEntry]) -> None:
        self.entries = entries
        self._by_ticker = {_norm(e.ticker): e for e in entries}
        self._by_cik: dict[int, list[TickerEntry]] = {}
        for e in entries:
            self._by_cik.setdefault(e.cik, []).append(e)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TickerTable":
        rows = payload.get("data") if isinstance(payload.get("data"), list) else None
        entries: list[TickerEntry] = []
        if rows is not None:                                    # the columnar layout: fields + data
            fields = payload.get("fields", [])
            for row in rows:
                rec = dict(zip(fields, row))
                entries.append(TickerEntry(int(rec["cik"]), str(rec["ticker"]), str(rec.get("name", ""))))
        else:                                                   # the keyed layout: {"0": {"cik_str": ...}}
            for rec in payload.values():
                if isinstance(rec, dict) and "ticker" in rec and "cik_str" in rec:
                    entries.append(TickerEntry(int(rec["cik_str"]), str(rec["ticker"]), str(rec.get("title", ""))))
        return cls(entries)

    def cik_for(self, ticker: str) -> int | None:
        entry = self._by_ticker.get(_norm(ticker))
        return entry.cik if entry else None

    def tickers_for(self, cik: int) -> list[str]:
        return [e.ticker for e in self._by_cik.get(int(cik), [])]

    def name_for(self, cik: int) -> str | None:
        entries = self._by_cik.get(int(cik))
        return entries[0].name if entries else None

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class FormInfo:
    code: str
    description: str
    family: str          # one of FAMILIES or "other"


def form_family(form: str) -> str:
    code = form.upper().strip()
    base = code.split("/")[0]
    for family, codes in FAMILIES.items():
        if code in codes or base in codes:
            return family
    return "other"


def form_info(form: str) -> FormInfo:
    return FormInfo(code=form.strip(), description=_lib.sec_describe_form(form.strip()), family=form_family(form))


def resolve(client: SecClient, symbol: str | int, as_of: datetime | date) -> result.SecResult:
    """Ticker (or CIK) to `{cik, ticker, name}`. NO_DATA when the SEC table does not know the ticker."""
    blocked = client.preflight(as_of)
    if blocked is not None:
        return blocked
    try:
        cik = client.cik_of(symbol)
        if cik is None:
            return result.no_data("ticker_not_in_sec_table", as_of, now=client.now)
        table = client.ticker_table() if not isinstance(symbol, int) and not str(symbol).isdigit() else None
        tickers = table.tickers_for(cik) if table else []
        name = table.name_for(cik) if table else None
    except TransportError as exc:
        return client.fail(exc, as_of)
    return result.ok({"cik": cik, "tickers": tickers, "name": name}, as_of, now=client.now)
