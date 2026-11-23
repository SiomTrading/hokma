"""Live filing streams and alerts (ED-020): the current feed of new filings, the latest filings of a company, and alerts for filings
that appeared since a moment.

Everything is cut at `as_of`: a filing accepted after `as_of` is not returned, so a replay never sees the future.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from hokma.data import _lib
from hokma.data.sec import result
from hokma.data.sec.client import SecClient
from hokma.data.sec.filings import list_filings
from hokma.data.sec.index import FilingRef, base_form
from hokma.data.sec.transport import TransportError

FEED_MAX_AGE = timedelta(hours=36)         # the live feed covers about the last day; older than this is not "current"


@dataclass(frozen=True)
class LiveFiling:
    form: str
    company: str
    cik: int
    filing_date: date
    accession: str
    accepted: datetime


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _moment(as_of: datetime | date) -> datetime:
    if isinstance(as_of, datetime):
        return _utc(as_of)
    return datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=timezone.utc)


def current_filings(client: SecClient, *, as_of: datetime | date, form: str = "", count: int = 40) -> result.SecResult:
    """Filings in the live feed accepted at or before `as_of`, newest first. STALE when the feed is older than 36 hours at `as_of`."""
    blocked = client.preflight(as_of)
    if blocked is not None:
        return blocked
    try:
        content = client.transport.current_feed(form, count)
    except TransportError as exc:
        return client.fail(exc, as_of)
    try:
        entries = _lib.sec_parse_current_feed(content)
    except ValueError as exc:
        return client.fail(TransportError("network", f"unreadable feed: {exc}"), as_of)
    moment = _moment(as_of)
    live = sorted((LiveFiling(e["form"], e["company"], e["cik"], e["filing_date"], e["accession"], _utc(e["accepted"])) for e in entries
                   if _utc(e["accepted"]) <= moment and (not form or e["form"].upper() == form.upper())), key=lambda f: f.accepted, reverse=True)
    if not live:
        return result.no_data("no_filing_in_feed_at_as_of", as_of, now=client.now)
    if moment - live[0].accepted > FEED_MAX_AGE:
        return result.stale(f"newest_feed_entry_is_{(moment - live[0].accepted).days}_days_old", as_of, now=client.now)
    return result.ok(live[:count], as_of, now=client.now)


def latest_filings(client: SecClient, symbol: str | int, *, as_of: datetime | date, form: str | Iterable[str] | None = None, count: int = 5) -> result.SecResult:
    """The newest `count` filings of a company visible at `as_of`."""
    return list_filings(client, symbol, as_of=as_of, form=form, limit=count)


def filings_since(client: SecClient, symbol: str | int, *, since: datetime, as_of: datetime | date, form: str | Iterable[str] | None = None) -> result.SecResult:
    """Filings of a company accepted after `since` and visible at `as_of`, oldest first (the alert list)."""
    listed = list_filings(client, symbol, as_of=as_of, form=form)
    if not listed.ok:
        return listed
    since_utc = _utc(since)
    fresh = [r for r in listed.value if _accepted(r) > since_utc]
    if not fresh:
        return result.no_data("nothing_new_since", as_of, now=client.now)
    return result.ok(sorted(fresh, key=_accepted), as_of, now=client.now)


def _accepted(ref: FilingRef) -> datetime:
    if ref.accepted is not None:
        return _utc(ref.accepted)
    return datetime(ref.filing_date.year, ref.filing_date.month, ref.filing_date.day, 23, 59, 59, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Alert:
    symbol: str | int
    ref: FilingRef


def check_alerts(client: SecClient, symbols: Iterable[str | int], forms: Iterable[str], seen: set[str], *, as_of: datetime | date) -> result.SecResult:
    """New filings of the forms given for each symbol, not in `seen`. Returns the alerts and the updated seen set; the caller keeps the set.

    A symbol that cannot be read is listed in the warnings and does not stop the others.
    """
    wanted = {base_form(f) for f in forms}
    alerts: list[Alert] = []
    updated = set(seen)
    warnings: list[str] = []
    blocked = client.preflight(as_of)
    if blocked is not None:
        return blocked
    for symbol in symbols:
        got = list_filings(client, symbol, as_of=as_of, form=tuple(wanted))
        if got.status == result.DATA_UNAVAILABLE:
            warnings.append(f"{symbol}: {got.reason}")
            continue
        for ref in (got.value or []):
            if ref.accession not in updated:
                updated.add(ref.accession)
                alerts.append(Alert(symbol, ref))
    alerts.sort(key=lambda a: _accepted(a.ref))
    return result.ok({"alerts": alerts, "seen": updated}, as_of, now=client.now, warnings=tuple(warnings))
