"""Commitment of Traders (SPEC-017: FT-098, CC-08).

The weekly positioning report is published by the CFTC but this module does NOT read it from the regulator: it reads it
from the paid provider that delivers it, and the result declares that provider as its source (`fmp`). Without that provider's
key the answer is `DATA_UNAVAILABLE` with the reason `provider_key_missing`. For positioning straight from the regulator
another source would have to be added.

A report is dated the Tuesday it describes and is published on the Friday after, so a report is used only from its Friday:
cutting by the Tuesday would put three days of the future into a run.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable

from hokma.data import sentinels
from hokma.data.providers import (KeyResolver, NoDataError, NotConfiguredError, RateLimitError, Transport, check_status,
                                  connections_key, http_transport)
from hokma.data.sentinels import DataResult, parse_time, utcnow

SOURCE = "fmp"
URL = "https://financialmodelingprep.com/stable/commitment-of-traders-report"
PUBLICATION_LAG_DAYS = 3        # Tuesday report, published Friday

FIELDS: dict[str, str] = {
    "name": "name", "sector": "sector", "openInterestAll": "open_interest",
    "noncommPositionsLongAll": "non_commercial_long", "noncommPositionsShortAll": "non_commercial_short",
    "noncommPositionsSpreadAll": "non_commercial_spread", "commPositionsLongAll": "commercial_long",
    "commPositionsShortAll": "commercial_short", "totReptPositionsLongAll": "total_reportable_long",
    "totReptPositionsShortAll": "total_reportable_short", "nonreptPositionsLongAll": "non_reportable_long",
    "nonreptPositionsShortAll": "non_reportable_short", "changeInOpenInterestAll": "change_in_open_interest",
    "changeInNoncommLongAll": "change_in_non_commercial_long", "changeInNoncommShortAll": "change_in_non_commercial_short",
    "changeInCommLongAll": "change_in_commercial_long", "changeInCommShortAll": "change_in_commercial_short",
    "pctOfOiNoncommLongAll": "pct_oi_non_commercial_long", "pctOfOiNoncommShortAll": "pct_oi_non_commercial_short",
    "pctOfOiCommLongAll": "pct_oi_commercial_long", "pctOfOiCommShortAll": "pct_oi_commercial_short",
}


def cot(market: str, *, as_of: datetime, lookback_days: int = 365, key: KeyResolver = connections_key,
        transport: Transport = http_transport, clock: Callable[[], datetime] = utcnow) -> DataResult:
    """Positioning reports of a futures market (for example `NG`) known on `as_of`, oldest first.

    Each report carries `date` (the Tuesday it describes) and `published_on` (the day it became public). Only reports
    published on or before `as_of` are returned.
    """
    cut, now = parse_time(as_of), clock()
    secret = key("fmp")
    if not secret:
        return sentinels.unavailable(SOURCE, sentinels.REASON_KEY_MISSING, as_of=cut, received_at=now)
    start = (cut - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    params = {"symbol": market, "from": start, "to": cut.strftime("%Y-%m-%d"), "apikey": secret}
    try:
        status, body = transport(URL, params, {"Accept": "application/json"})
        check_status(SOURCE, status, body, secret=secret)
        rows = json.loads(body)
    except NotConfiguredError as error:
        return sentinels.unavailable(SOURCE, sentinels.REASON_KEY_MISSING, as_of=cut, received_at=now, warnings=(str(error),))
    except RateLimitError as error:
        return sentinels.unavailable(SOURCE, error.reason, as_of=cut, received_at=now, warnings=(sentinels.scrub(str(error), secret),))
    except (NoDataError, json.JSONDecodeError) as error:
        return sentinels.unavailable(SOURCE, "bad_response", as_of=cut, received_at=now, warnings=(sentinels.scrub(str(error), secret),))
    if not isinstance(rows, list) or not rows:
        return sentinels.no_data(SOURCE, sentinels.REASON_EMPTY, as_of=cut, received_at=now,
                                 warnings=("the provider returns reports only for markets tracked by the regulator",))
    reports: list[dict[str, Any]] = []
    for row in rows:
        try:
            day = parse_time(str(row["date"])[:10])
        except (KeyError, ValueError):
            continue
        published = day + timedelta(days=PUBLICATION_LAG_DAYS)
        if published.date() > cut.date():
            continue
        report = {"date": day.strftime("%Y-%m-%d"), "published_on": published.strftime("%Y-%m-%d")}
        report.update({new: row.get(old) for old, new in FIELDS.items() if old in row})
        reports.append(report)
    if not reports:
        return sentinels.no_data(SOURCE, sentinels.REASON_NOT_PUBLISHED, as_of=cut, received_at=now)
    reports.sort(key=lambda r: r["date"])
    return sentinels.ok({"market": market, "reports": reports}, source=SOURCE, as_of=cut, received_at=now,
                        meta={"publisher_note": "regulator data delivered by a paid provider"})
