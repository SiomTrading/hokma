"""The economic calendar (SPEC-018: CTS-045). One query, one copy: the guard windows, the Today card and the agents read it here.

Sources, in order: the public Forex Factory weekly feed (no key, one week), then Finnhub when the owner has a key. A key is never
read here: the caller injects `finnhub_key`, a callable that returns the secret (or None, or raises `KeyUnavailable`).

The rule that matters: a calendar that could not be read is NEVER an empty list.
  - a failed request, a body that is not a calendar, or a range the sources do not cover  -> status "UNVERIFIED_EVENT_RISK"
  - a successful read with nothing in the range                                          -> status "NO_EVENTS"
  - a successful read with events                                                       -> status "ok"
An event without a complete timestamp (date, time and offset or UTC) is dropped: it cannot open a window.

Events are saved in `econ_events` (upsert by `event_id`). A second read within the TTL answers from there, so the feed (which
answers a few requests per minute) is not asked again.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("hokma.econ_calendar")

FOREX_FACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/economic"
HOSTS = ("nfs.faireconomy.media", "finnhub.io")
FEED_ZONE = ZoneInfo("America/New_York")          # the Forex Factory feed is in US Eastern time
MAX_BODY = 2 * 1024 * 1024
TIMEOUT_SECONDS = 15.0
FEED_TTL = 600.0                                  # seconds a feed read answers the next ones
FINNHUB_TTL = 300.0
DENIED_MEMORY = 3600.0
MAX_SPAN = timedelta(days=92)
MAX_FILTER = 20
MAX_NAME = 160
STATUSES = ("ok", "NO_EVENTS", "UNVERIFIED_EVENT_RISK")
IMPACT = {"low": 1, "medium": 2, "high": 3}
SOURCES = {"forexfactory": "Forex Factory", "finnhub": "Finnhub"}
FF_COUNTRY = {"USD": "US", "EUR": "EU", "GBP": "GB", "JPY": "JP", "CAD": "CA", "AUD": "AU", "NZD": "NZ", "CHF": "CH", "CNY": "CN"}
FF_IMPACT = {"low": "low", "medium": "medium", "high": "high", "holiday": "low"}
_SUFFIX = {"K": Decimal(1000), "M": Decimal(1000000), "B": Decimal(1000000000), "T": Decimal(1000000000000)}
_NUMBER = re.compile(r"^(-?\d+(?:\.\d+)?)([KMBT]?)%?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

Fetch = Callable[[str, dict[str, str]], tuple[int, str]]
KeyHook = Callable[[], "str | None"]


class KeyUnavailable(Exception):
    """The Finnhub key cannot be used now; `code` is a stable reason (finnhub_not_connected, finnhub_not_on_desk...)."""

    def __init__(self, code: str = "finnhub_not_connected") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EconEvent:
    event_id: str
    source: str                    # forexfactory | finnhub
    name: str
    country: str                   # ISO 3166 alpha 2 (EU for the euro area)
    impact: str                    # low | medium | high
    scheduled_at: datetime         # UTC
    actual: float | None
    consensus: float | None
    previous: float | None
    revised: float | None
    received_at: datetime


@dataclass(frozen=True)
class CalendarResult:
    status: str                    # ok | NO_EVENTS | UNVERIFIED_EVENT_RISK
    events: tuple[EconEvent, ...] = ()
    source: str | None = None      # the source that answered; None when nothing was read
    reasons: tuple[str, ...] = ()  # stable codes of what failed (feed_*, finnhub_*, range_not_covered)

    @property
    def verified(self) -> bool:
        return self.status != "UNVERIFIED_EVENT_RISK"


# ---------------------------------------------------------------------------------------------- fetching

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def default_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
    """GET over `tls.outbound_context`: https only, only the two hosts of this module, no redirects, a timeout and a size cap.
    Returns (status, text); raises OSError on a network failure or a refused URL."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in HOSTS or parts.username or parts.password:
        raise OSError("url not allowed")
    from hokma.core import tls

    opener = tls.opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Hokma)", "Accept": "application/json", **headers})
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read(MAX_BODY + 1).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, ""
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise OSError(str(error)) from None


# ---------------------------------------------------------------------------------------------- parsing

def _clean(value: Any) -> str:
    text = " ".join(_CONTROL.sub(" ", str(value or "")).split())
    return text if len(text) <= MAX_NAME else text[:MAX_NAME - 1].rstrip() + "…"


def _number(value: Any) -> float | None:
    """A number of the feeds: 0.3, "0.3%", "198K", "-0.90T". Anything else (ranges, words, NaN) is None, never zero."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    match = _NUMBER.match(str(value).strip().upper())
    if not match:
        return None
    try:
        scaled = Decimal(match.group(1)) * _SUFFIX.get(match.group(2), Decimal(1))
    except InvalidOperation:
        return None
    return float(scaled)


def _event_id(source: str, country: str, name: str, at: datetime) -> str:
    digest = hashlib.sha256(f"{source}|{country}|{name}|{at:%Y-%m-%dT%H:%M}".encode()).hexdigest()[:20]
    return f"{'ff' if source == 'forexfactory' else 'fh'}:{digest}"


def _aware(text: Any, *, assume_utc: bool = False) -> datetime | None:
    """A complete timestamp as UTC: date, time and (an offset or `assume_utc`). Anything less is None."""
    if not isinstance(text, str) or len(text) < 16:
        return None
    raw = text.strip().replace("Z", "+00:00")
    if "T" not in raw and " " in raw:
        raw = raw.replace(" ", "T", 1)
    if "T" not in raw:
        return None                                   # a date alone
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not assume_utc:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_forex_factory(body: str, received_at: datetime) -> list[EconEvent] | None:
    """The events of the weekly feed, or None when the body is not a calendar (not JSON, not a list, or rows nobody can read)."""
    try:
        rows = json.loads(body)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    out: list[EconEvent] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        at = _aware(row.get("date"))
        name = _clean(row.get("title"))
        impact = FF_IMPACT.get(str(row.get("impact", "")).lower())
        currency = str(row.get("country") or "").upper()
        if at is None or not name or impact is None or not currency:
            continue                                  # no complete timestamp: dropped
        country = FF_COUNTRY.get(currency, currency[:2])
        out.append(EconEvent(_event_id("forexfactory", country, name, at), "forexfactory", name, country, impact, at, None,
                             _number(row.get("forecast")), _number(row.get("previous")), None, received_at))
    if rows and not out:
        return None
    return out


def parse_finnhub(body: str, received_at: datetime) -> list[EconEvent] | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    rows = data.get("economicCalendar") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    out: list[EconEvent] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        at = _aware(row.get("time"), assume_utc=True)    # Finnhub times are UTC
        name = _clean(row.get("event"))
        impact = str(row.get("impact", "")).lower()
        country = str(row.get("country") or "").upper()[:2]
        if at is None or not name or impact not in IMPACT or not country:
            continue
        out.append(EconEvent(_event_id("finnhub", country, name, at), "finnhub", name, country, impact, at, _number(row.get("actual")),
                             _number(row.get("estimate")), _number(row.get("prev")), None, received_at))
    if rows and not out:
        return None
    return out


def feed_coverage(at: datetime) -> tuple[datetime, datetime]:
    """The span a weekly feed that holds `at` covers: Sunday 00:00 to the next Sunday 00:00, New York time, as UTC."""
    local = at.astimezone(FEED_ZONE).date()
    sunday = local - timedelta(days=(local.weekday() + 1) % 7)
    first = datetime.combine(sunday, dtime(0, 0), tzinfo=FEED_ZONE)
    return first.astimezone(timezone.utc), datetime.combine(sunday + timedelta(days=7), dtime(0, 0), tzinfo=FEED_ZONE).astimezone(timezone.utc)


# ---------------------------------------------------------------------------------------------- the store

def _store(db: Any):
    """The database to use: the one given, else the process one when it has a schema, else None (False: never)."""
    if db is False:
        return None
    if db is not None:
        return db
    try:
        from hokma.db import current_version, get_database

        return get_database() if current_version() > 0 else None
    except Exception:                                  # noqa: BLE001  (no store is not a failed read)
        return None


def _save(db: Any, events: Iterable[EconEvent]) -> None:
    from hokma.db import to_db_time

    rows = [{"i": e.event_id, "s": e.source, "n": e.name, "c": e.country, "p": e.impact, "t": to_db_time(e.scheduled_at),
             "a": e.actual, "k": e.consensus, "v": e.previous, "r": e.revised, "x": to_db_time(e.received_at)} for e in events]
    if not rows:
        return
    try:
        with db.write() as u:
            for row in rows:
                u.session.execute(
                    "INSERT INTO econ_events (event_id, source, name, country, impact, scheduled_at, actual, consensus, previous, "
                    "revised, received_at) VALUES (:i, :s, :n, :c, :p, :t, :a, :k, :v, :r, :x) "
                    "ON CONFLICT (event_id) DO UPDATE SET name = EXCLUDED.name, impact = EXCLUDED.impact, "
                    "scheduled_at = EXCLUDED.scheduled_at, actual = COALESCE(EXCLUDED.actual, econ_events.actual), "
                    "consensus = EXCLUDED.consensus, previous = EXCLUDED.previous, "
                    "revised = COALESCE(EXCLUDED.revised, econ_events.revised), received_at = EXCLUDED.received_at", row)
    except Exception:                                  # noqa: BLE001  (the read worked; only the copy failed)
        log.exception("could not save the calendar events")


def _saved(db: Any, source: str, now: datetime, ttl: float) -> list[EconEvent] | None:
    """The rows of `source` read within the TTL, or None when there are none (then the source is asked)."""
    from hokma.db import from_db_time, to_db_time

    try:
        with db.read() as u:
            rows = u.session.query("SELECT * FROM econ_events WHERE source = :s AND received_at >= :t",
                                   {"s": source, "t": to_db_time(now - timedelta(seconds=ttl))})
    except Exception:                                  # noqa: BLE001
        return None
    if not rows:
        return None
    return [EconEvent(r["event_id"], r["source"], r["name"], r["country"], r["impact"], from_db_time(r["scheduled_at"]), r["actual"],
                      r["consensus"], r["previous"], r["revised"], from_db_time(r["received_at"])) for r in rows]


# ---------------------------------------------------------------------------------------------- the query

_MEMO: dict[tuple, tuple[float, Any]] = {}


def forget() -> None:
    """Forget what is remembered in this process (tests)."""
    _MEMO.clear()


def _filter(events: Iterable[EconEvent], start: datetime, end: datetime, countries: Sequence[str], names: Sequence[str],
            min_impact: str) -> tuple[EconEvent, ...]:
    wanted = [n.casefold() for n in names]
    keep = [e for e in events if start <= e.scheduled_at <= end and (not countries or e.country in countries)
            and IMPACT.get(e.impact, 0) >= IMPACT[min_impact] and (not wanted or any(w in e.name.casefold() for w in wanted))]
    return tuple(sorted(keep, key=lambda e: (e.scheduled_at, e.name)))


def _checked(start: datetime, end: datetime, countries: Any, names: Any, min_impact: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(start, datetime) or not isinstance(end, datetime) or start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end are aware datetimes")
    if end < start or end - start > MAX_SPAN:
        raise ValueError(f"the range is at most {MAX_SPAN.days} days, in order")
    if min_impact not in IMPACT:
        raise ValueError("min_impact is low, medium or high")
    for label, value, size in (("countries", countries, 2), ("names", names, None)):
        if value is None:
            continue
        if not isinstance(value, (list, tuple)) or len(value) > MAX_FILTER or not all(
                isinstance(x, str) and (len(x.strip()) == size if size else 1 <= len(x.strip()) <= 40) for x in value):
            raise ValueError(f"{label} is a short list of short texts")
    return (tuple(sorted({c.strip().upper() for c in countries or ()})), tuple(dict.fromkeys(n.strip() for n in names or ())))


def _call(fetch: Fetch, url: str, headers: dict[str, str]) -> tuple[int, str] | None:
    try:
        status, text = fetch(url, headers)
    except Exception:                                  # noqa: BLE001  (a network failure of any kind is a failed read)
        return None
    return (status, text) if isinstance(text, str) and len(text) <= MAX_BODY else (status, "")


def events(*, start: datetime, end: datetime, countries: Sequence[str] | None = None, names: Sequence[str] | None = None,
           db: Any = None, fetch: Fetch | None = None, finnhub_key: KeyHook | None = None, min_impact: str = "low",
           clock: Callable[[], float] | None = None, memo: MutableMapping | None = None,
           allow: Callable[[], bool] | None = None) -> CalendarResult:
    """The events between `start` and `end` (both included, aware), for `countries` (alpha 2; empty: all) whose name holds one
    of `names` (any case; empty: all) and that have at least `min_impact`.

    `db`: the database that keeps the events (None: the process one when it has a schema; False: none). `fetch`: replaces the HTTP
    GET `(url, headers) -> (status, text)`. `finnhub_key`: returns the owner's Finnhub key, None or raises `KeyUnavailable`.
    `allow`: a rate limiter asked before each Finnhub call. `clock`/`memo`: seconds and the in-process memory (tests).
    Never raises for a failed read: that is `UNVERIFIED_EVENT_RISK`. Raises ValueError for arguments that are not valid."""
    countries_, names_ = _checked(start, end, countries, names, min_impact)
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    fetch = fetch or default_fetch
    memo = _MEMO if memo is None else memo
    seconds = (clock or time.time)()
    now = datetime.fromtimestamp(seconds, timezone.utc)
    store = _store(db)
    reasons: list[str] = []

    # 1. Forex Factory: the week of the feed.
    feed = _feed(fetch, memo, seconds, now, store, reasons)
    if feed is not None:
        rows, covered_from, covered_to = feed
        found = _filter(rows, start, end, countries_, names_, min_impact)
        if found:
            return CalendarResult("ok", found, "forexfactory")     # real events are real even when the range runs past the week
        if covered_from <= start and end < covered_to:
            # The feed answered and has nothing: Finnhub may know more; if it cannot, the empty answer of a read stays.
            second = _finnhub(fetch, finnhub_key, memo, seconds, now, store, start, end, [], allow)
            if isinstance(second, list):
                again = _filter(second, start, end, countries_, names_, min_impact)
                if again:
                    return CalendarResult("ok", again, "finnhub")
            return CalendarResult("NO_EVENTS", (), "forexfactory")
        reasons.append("range_not_covered")                   # nothing found AND part of the range is not in the feed

    # 2. Finnhub: any range.
    second = _finnhub(fetch, finnhub_key, memo, seconds, now, store, start, end, reasons, allow)
    if isinstance(second, list):
        found = _filter(second, start, end, countries_, names_, min_impact)
        return CalendarResult("ok", found, "finnhub") if found else CalendarResult("NO_EVENTS", (), "finnhub")
    return CalendarResult("UNVERIFIED_EVENT_RISK", (), None, tuple(dict.fromkeys(reasons)))


def _feed(fetch: Fetch, memo: MutableMapping, seconds: float, now: datetime, store: Any, reasons: list[str]):
    """(events, covered_from, covered_to) of the weekly feed, or None (the reason is appended)."""
    key = ("econ_calendar", "forexfactory")
    hit = memo.get(key)
    if hit and hit[0] > seconds:
        value = hit[1]
        if value is None:
            reasons.append("feed_unreadable")
        return value
    if store is not None:
        saved = _saved(store, "forexfactory", now, FEED_TTL)
        if saved:
            lo, hi = feed_coverage(max(saved, key=lambda e: e.received_at).scheduled_at)
            memo[key] = (seconds + FEED_TTL, (saved, lo, hi))
            return saved, lo, hi
    answer = _call(fetch, FOREX_FACTORY_URL, {})
    parsed = parse_forex_factory(answer[1], now) if answer and answer[0] == 200 and answer[1] else None
    if parsed is None:
        memo[key] = (seconds + 60.0, None)             # a failure is remembered for a minute, not asked again at once
        reasons.append("feed_unreadable")
        return None
    if parsed:
        lo, hi = feed_coverage(parsed[len(parsed) // 2].scheduled_at)
    else:
        lo, hi = feed_coverage(now)
    value = (parsed, lo, hi)
    memo[key] = (seconds + FEED_TTL, value)
    if store is not None:
        _save(store, parsed)
    return value


def _finnhub(fetch: Fetch, key_hook: KeyHook | None, memo: MutableMapping, seconds: float, now: datetime, store: Any,
             start: datetime, end: datetime, reasons: list[str], allow: Callable[[], bool] | None):
    """The events Finnhub has for the days of the range, or None (the reason is appended). Rows are kept in `econ_events`."""
    if key_hook is None:
        reasons.append("finnhub_not_connected")
        return None
    try:
        secret = key_hook()
    except KeyUnavailable as exc:
        reasons.append(exc.code)
        return None
    except Exception:                                  # noqa: BLE001
        reasons.append("finnhub_not_connected")
        return None
    if not secret or not isinstance(secret, str):
        reasons.append("finnhub_not_connected")
        return None
    first, last = start.date(), end.date()
    key = ("econ_calendar", "finnhub", first.isoformat(), last.isoformat())
    hit = memo.get(key)
    if hit and hit[0] > seconds:
        return hit[1]
    denied = memo.get(("econ_calendar", "finnhub", "denied"))
    if denied and denied[0] > seconds:
        reasons.append("finnhub_denied")
        return None
    if allow is not None and not allow():
        reasons.append("finnhub_rate")
        return None
    url = FINNHUB_URL + "?" + urllib.parse.urlencode({"from": first.isoformat(), "to": last.isoformat()})
    answer = _call(fetch, url, {"X-Finnhub-Token": secret})
    if answer is None:
        reasons.append("finnhub_network")
        return None
    status, text = answer
    if status == 401:
        reasons.append("finnhub_bad_key")
        return None
    if status in (402, 403):
        memo[("econ_calendar", "finnhub", "denied")] = (seconds + DENIED_MEMORY, True)
        reasons.append("finnhub_denied")
        return None
    if status == 429:
        reasons.append("finnhub_rate")
        return None
    parsed = parse_finnhub(text, now) if status == 200 and text else None
    if parsed is None:
        reasons.append("finnhub_unreadable" if status == 200 else "finnhub_upstream")
        return None
    memo[key] = (seconds + FINNHUB_TTL, parsed)
    if store is not None:
        _save(store, parsed)
    return parsed
