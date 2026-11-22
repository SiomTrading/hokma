"""The hours of the stock market as ALPACA says them (SPEC-016 3.4, A3), verified by Iris against Alpaca's own documentation (21-nov):

* `GET /v2/clock` has no pre or post (only `is_open` of the regular session): it is not used.
* `GET /v3/calendar/{market}` (`start`, `end`, `timezone`: UTC only) gives `date`, `pre_start`, `pre_end`, `core_start`, `core_end`, `post_start`, `post_end` (and
  lunch for markets that have one): the pieces of the day, which are the pieces Hokma shades.
* `GET /v3/clock?markets=` gives, per market, `phase` (closed, pre, core, lunch, post) and `phase_until`: the live "open or closed".
Both are on the TRADING API (the live host of `options.py`, or its paper twin for a demo key), not on the market data host.

What the documentation does not say, and is assumed here: the envelope of the answer (a list, or an object with a list inside) and the exact form of
the times (read as an ISO date-time; a bare `HH:MM` is read as UTC on that date). If anything is missing the function returns None and Hokma shows no
shading and "unknown" for open or closed, never a guess. These are the only reads of Alpaca's calendar and clock in Hokma.
"""
import json
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from hokma.kit import hardblock
from hokma.market.hours import ProviderHours, Segment
from hokma.market.options import LIVE_HOST, PAPER_HOST       # the one file that names the real host is options.py

CALENDAR_PATH = "/v3/calendar/{market}"
CLOCK_PATH = "/v3/clock"
MARKET = "NYSE"
PIECES = (("preMarket", "pre_start", "pre_end"), ("regularMarket", "core_start", "core_end"), ("postMarket", "post_start", "post_end"))


def trading_hosts(key_id: str) -> list[str]:
    """The trading hosts to try, by the prefix of the key id (PK demo, AK real); any other prefix tries both."""
    prefix = key_id[:2].upper()
    return [PAPER_HOST] if prefix == "PK" else [LIVE_HOST] if prefix == "AK" else [PAPER_HOST, LIVE_HOST]


def _moment(day: str, value) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = datetime.fromisoformat(f"{day}T{value}")
        except ValueError:
            return None
    return int((moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp())


def _rows(payload) -> list[dict]:
    if isinstance(payload, dict):
        payload = next((v for v in payload.values() if isinstance(v, list)), [])
    return [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []


def parse_calendar(payload, day: date) -> ProviderHours | None:
    """The pieces of `day`. A day the calendar does not list (holiday, weekend) is closed with no pieces; a listed day without its core times is unknown."""
    if not isinstance(payload, (list, dict)) or (isinstance(payload, dict) and not any(isinstance(v, list) for v in payload.values())):
        return None
    iso = day.isoformat()
    entry = next((r for r in _rows(payload) if r.get("date") == iso), None)
    if entry is None:
        return ProviderHours("equity", None, iso, False, ())
    segments = []
    for name, first, last in PIECES:
        start, end = _moment(iso, entry.get(first)), _moment(iso, entry.get(last))
        if start is not None and end is not None and end > start:
            segments.append(Segment(name, start, end))
    if not any(s.name == "regularMarket" for s in segments):
        return None
    return ProviderHours("equity", None, iso, None, tuple(sorted(segments, key=lambda s: s.start)))


def _get(fetch, headers: dict, hosts: list[str], path: str, query: str) -> object | None:
    """Blocking. The first host that answers 200 with JSON. A call to the real host is made only if the lock allows this consumer that exact read."""
    for host in hosts:
        netloc = urlsplit(host).netloc
        if host == LIVE_HOST and not hardblock.read_exception_allows(netloc, "GET", path, "hokma_backend"):
            continue
        try:
            status, text = fetch(f"{host}{path}?{query}", headers)
            if status == 200:
                return json.loads(text)
        except Exception:                                   # noqa: BLE001  (an answer that does not come is "unknown")
            continue
    return None


def fetch_day(fetch, headers: dict, day: date, hosts: list[str] | None = None) -> ProviderHours | None:
    """The pieces of `day` from Alpaca's calendar. `fetch(url, headers)` is `history.http_get`."""
    path = CALENDAR_PATH.format(market=MARKET)
    payload = _get(fetch, headers, hosts or [PAPER_HOST, LIVE_HOST], path, f"start={day.isoformat()}&end={day.isoformat()}&timezone=UTC")
    return parse_calendar(payload, day) if payload is not None else None


def phase(fetch, headers: dict, hosts: list[str] | None = None) -> str | None:
    """The live phase of the market (closed, pre, core, lunch, post), or None when Alpaca does not say."""
    payload = _get(fetch, headers, hosts or [PAPER_HOST, LIVE_HOST], CLOCK_PATH, f"markets={MARKET}")
    for row in _rows(payload) if payload is not None else []:
        if row.get("market") in (MARKET, None) and isinstance(row.get("phase"), str):
            return row["phase"]
    return None
