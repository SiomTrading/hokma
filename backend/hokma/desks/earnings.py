"""Data of the Calendar panel of a desk (SPEC-028, delivery 7): who reports each weekday of a week, from the data connection of the owner.

One source: `hokma.kit.company_tools.earnings_calendar`, the same function the agents use. It is asked one day at a time, because a provider
answer is cut at `MAX_CALENDAR` rows and a whole week would lose the rest; a day that is still cut says so (`truncated`). The calendar is reference
data: it is read when the panel opens or the week changes, never on a timer. The market holidays come from the market hours of the broker
connection when there is one; without it the answer says it does not know them, and nothing is guessed.
Not here: the candidate / discarded marks of the PEAD flow (paused by decision of the owner).
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

from hokma.desks import service
from hokma.desks.errors import DeskError
from hokma.kit import company_tools

FILTERS = ("all", "list", "liquid")
MAX_SPAN_DAYS = 6                       # Monday to Sunday at most; the weekend is never asked
LIST_SYMBOLS_MAX = 10                   # symbols asked one by one to fill a day that was cut
LIQUID_CHECKED_MAX = 20                 # tickers whose options are read for the liquid filter; the biggest companies first
LIQUID_MAX_RELATIVE_SPREAD = 0.10       # a contract is liquid with a quoted spread of at most 10 percent of its mid...
LIQUID_MIN_OPEN_INTEREST = 100          # ...and at least this many open contracts
LIQUID_MIN_SHARE = 0.5                  # a ticker is liquid when at least this share of its contracts pass both
LIQUID_STRIKES = 4

_failed: dict[str, float] = {}           # day -> monotonic time until which a failed answer is not asked again
FAILURE_PAUSE_SECONDS = 15 * 60
_hours: dict[str, bool] = {}            # day -> is a market holiday (a day that has been answered never changes)


def _day(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise DeskError("bad_range", 400) from exc


def weekdays(first: date, last: date) -> list[date]:
    if last < first or (last - first).days > MAX_SPAN_DAYS:
        raise DeskError("bad_range", 400)
    return [first + timedelta(days=n) for n in range((last - first).days + 1) if (first + timedelta(days=n)).weekday() < 5]


def surprise_pct(estimate: float | None, actual: float | None) -> float | None:
    """The surprise of a published report, in percent of the estimate. None when either number is missing or the estimate is zero."""
    if estimate is None or actual is None or estimate == 0:
        return None
    return round((actual - estimate) / abs(estimate) * 100, 2)


def _row(item: dict) -> dict[str, Any]:
    estimate, actual = item.get("eps_estimate"), item.get("eps_actual")
    return {"symbol": item.get("symbol"), "date": item.get("date"), "hour": item.get("hour"), "eps_estimate": estimate, "eps_actual": actual,
            "surprise_pct": surprise_pct(estimate, actual), "revenue_estimate": item.get("revenue_estimate")}


def _ask_day(day: date, desk_id: int, symbol: str | None = None) -> tuple[list[dict], bool, str | None]:
    """The reports of one day (rows, cut, provider). A day with no report is an empty list. A missing connection goes up as `NotConnected`."""
    try:
        out = company_tools.earnings_calendar(day.isoformat(), day.isoformat(), symbol, desk_id)
    except company_tools.NoRows:
        return [], False, None
    rows = [_row(r) for r in out.get("earnings", []) if r.get("symbol") and str(r.get("date")) == day.isoformat()]
    return rows, bool(out.get("truncated")), out.get("source")


def _holiday(day: date) -> bool | None:
    """True when the market is closed all day on a weekday. None when the broker connection cannot say: the answer is then unknown, never a guess."""
    key = day.isoformat()
    if key in _hours:
        return _hours[key]
    if time.monotonic() < _failed.get(key, 0.0):          # the broker failed for this day a moment ago: it is not asked again until the pause ends
        return None
    from hokma.schwab import endpoints, models

    try:
        payload = models.dump(endpoints.market_hours("equity", date=key, past_ok=True))
    except Exception:                                   # noqa: BLE001  (a label: whatever fails, the holidays are "unknown")
        _failed[key] = time.monotonic() + FAILURE_PAUSE_SECONDS
        return None
    entry = (payload.get("equity") or {}).get("EQ") if isinstance(payload, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("isOpen"), bool):
        return None
    sessions = (entry.get("sessionHours") or {}).get("regularMarket") if isinstance(entry.get("sessionHours"), dict) else None
    _hours[key] = entry["isOpen"] is False and not sessions
    return _hours[key]


def _liquid(symbol: str, desk_id: int) -> bool | None:
    """Whether the options of the ticker are liquid, read on the chain of its nearest expiration (the contracts around the price). None when the
    options of this ticker cannot be read: it is not counted as liquid or as illiquid."""
    from hokma.market import options
    from hokma.quant.options import liquidity

    try:
        listed = options.expirations(symbol, desk_id).get("expirations") or []
        pick = next((e for e in listed if (e.get("days") or 0) >= 1), None)
        if pick is None:
            return None
        groups = options.chain(symbol, [pick["date"]], LIQUID_STRIKES, desk_id).get("expirations") or []
        contracts = [{"symbol": cell.get("symbol"), "strike": row.get("strike"), "expiration": pick["date"], "bid": cell.get("bid"), "ask": cell.get("ask"),
                      "open_interest": cell.get("open_interest")}
                     for group in groups for row in group.get("rows", []) for cell in (row.get("call"), row.get("put")) if cell]
        result = liquidity.chain_liquidity(contracts, LIQUID_MAX_RELATIVE_SPREAD, LIQUID_MIN_OPEN_INTEREST)
    except (LookupError, ValueError):                   # OptionsError is both
        return None
    value = result.value if isinstance(result.value, dict) else None
    if not value or not value.get("n_valid"):
        return None
    return (value["n_valid"] - value["n_failing"]) / value["n_valid"] >= LIQUID_MIN_SHARE


def _mark_liquid(days: list[dict], desk_id: int) -> dict[str, int]:
    """Marks `liquid` on the rows. The tickers with the biggest revenue estimate are read first; the rest are left unchecked (None) and counted."""
    rows = [r for d in days for r in d["rows"]]
    seen: dict[str, bool | None] = {}
    for row in sorted(rows, key=lambda r: -(r.get("revenue_estimate") or 0)):
        symbol = row["symbol"]
        if symbol not in seen and len(seen) < LIQUID_CHECKED_MAX:
            seen[symbol] = _liquid(symbol, desk_id)
    for row in rows:
        row["liquid"] = seen.get(row["symbol"])
    unchecked = len({r["symbol"] for r in rows} - {s for s, v in seen.items() if v is not None})
    return {"checked": sum(1 for v in seen.values() if v is not None), "unchecked": unchecked}


def calendar(desk_id: int, first: Any, last: Any, filter_: Any = "all", today: date | None = None) -> dict[str, Any]:
    """The week of reports for a desk. `connected` false when there is no data connection: the panel then asks for it in Connections."""
    mode = filter_ if filter_ in FILTERS else None
    if mode is None:
        raise DeskError("bad_filter", 400)
    days = weekdays(_day(first), _day(last))
    desk = service.get_desk(desk_id)                    # 404 when the desk is not there
    mine = [a["symbol"].upper() for a in desk.get("assets", [])]
    out: dict[str, Any] = {"from": days[0].isoformat() if days else str(first), "to": days[-1].isoformat() if days else str(last), "filter": mode,
                           "today": (today or date.today()).isoformat(), "connected": True, "source": None, "list": mine, "days": [],
                           "holidays_known": False, "liquid": None}
    hours = []
    for day in days:
        try:
            rows, cut, source = _ask_day(day, desk_id)
        except company_tools.NotConnected:
            return {**out, "connected": False}
        except LookupError as exc:                      # a refusal of the provider for this day: it is shown on the day, not as an empty day
            rows, cut, source, error = [], False, None, str(exc)
        else:
            error = None
        out["source"] = out["source"] or source
        hours.append(_holiday(day))
        out["days"].append({"date": day.isoformat(), "holiday": hours[-1], "truncated": cut, "error": error, "rows": rows})
    out["holidays_known"] = any(h is not None for h in hours)
    if mode == "list":
        _fill_list(out["days"], mine, desk_id)
    for entry in out["days"]:
        for row in entry["rows"]:
            row["in_list"] = str(row["symbol"]).upper() in mine
    if mode == "list":
        for entry in out["days"]:
            entry["rows"] = [r for r in entry["rows"] if r["in_list"]]
    elif mode == "liquid":
        out["liquid"] = _mark_liquid(out["days"], desk_id)
        for entry in out["days"]:
            entry["rows"] = [r for r in entry["rows"] if r["liquid"]]
    return out


def _fill_list(days: list[dict], mine: list[str], desk_id: int) -> None:
    """A day the provider cut may have lost a ticker of the list: those are asked one by one, so "my list" is not missing any."""
    for entry in days:
        if not entry["truncated"]:
            continue
        have = {str(r["symbol"]).upper() for r in entry["rows"]}
        for symbol in [s for s in mine if s not in have][:LIST_SYMBOLS_MAX]:
            try:
                rows, _, _source = _ask_day(date.fromisoformat(entry["date"]), desk_id, symbol)
            except LookupError:
                break
            entry["rows"] += rows
        entry["truncated"] = False
