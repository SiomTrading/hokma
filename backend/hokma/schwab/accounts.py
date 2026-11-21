"""Account references, masks and the personal data that never leaves the backend (SPEC-015 5.9) and the data of the
"Ver cuenta" panel (5.7).

'account_ref' ("A1", "A2"...) is the only name an account has outside this module: the table maps it to the hash Schwab
wants in paths and to a mask ("••••" and the last four digits of the account number). The table lives in memory, is
built on first use from GET /accounts/accountNumbers, in the order of the answer, and is refreshed after an hour; a
refresh keeps the 'account_ref' of every account it already knew, so a reference does not change meaning while Hokma
runs. The hash and the plain number are never logged, returned or put in an error.

'public(obj)' is the one way out for anything read from the accounts endpoints: every accountNumber of any schema
becomes 'account_ref' and 'mask' next to where it was, no hash survives, UserDetails keeps only type, firstName and
lastName, and StreamerInfo only streamerSocketUrl. Blocking: call it from a thread.
"""
import copy
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from hokma.schwab import endpoints, models
from hokma.schwab.http import INVALID_PARAM, SchwabError

log = logging.getLogger("hokma.schwab")

MASK_DOTS = "•" * 4
REFRESH_SECONDS = 3600.0                              # the table is read again every hour (5.9)
RETRY_SECONDS = 60.0                                  # after a failed refresh the old table serves for a minute more
MISS_SECONDS = 30.0                                   # a number that is not in the table forces at most one refresh per 30 s
PANEL_CACHE_SECONDS = 15.0                            # 5.7
USER_KEEP = ("type", "firstName", "lastName")         # UserDetails: cdDomainId, login, userId, systemUserName... go
STREAMER_KEEP = ("streamerSocketUrl",)                # StreamerInfo: the customer, correl, channel and function ids stay
# The 15 open states of the 'status' enum (5.7); the server filters by one status only, so Hokma filters.
OPEN_ORDER_STATUSES = (
    "AWAITING_PARENT_ORDER", "AWAITING_CONDITION", "AWAITING_STOP_CONDITION", "AWAITING_MANUAL_REVIEW", "ACCEPTED",
    "AWAITING_UR_OUT", "PENDING_ACTIVATION", "QUEUED", "WORKING", "NEW", "AWAITING_RELEASE_TIME",
    "PENDING_ACKNOWLEDGEMENT", "PENDING_CANCEL", "PENDING_REPLACE", "PENDING_RECALL")
_MARGIN_BALANCES = ("equity", "buyingPower", "availableFunds")
_CASH_BALANCES = ("totalCash", "cashAvailableForTrading")
_POSITION_FIELDS = ("longQuantity", "shortQuantity", "averagePrice", "marketValue", "currentDayProfitLoss",
                    "currentDayProfitLossPercentage")
_ORDER_FIELDS = ("orderType", "price", "stopPrice", "duration", "status", "filledQuantity", "remainingQuantity",
                 "enteredTime")

_lock = threading.RLock()
_clock: Callable[[], float] = time.monotonic
_entries: dict[str, tuple[str, str, str]] = {}        # account_ref -> (account number, hash, mask)
_refs: dict[str, str] = {}                            # account number -> account_ref
_built_at: float | None = None
_next = 1                                             # the next 'account_ref' number: a reference is never reused
_last_miss = float("-inf")
_panel: tuple[float, dict] | None = None


def set_clock(clock: Callable[[], float] | None = None) -> None:
    """Tests only: a fake monotonic clock (seconds) for the hourly refresh and the 15 s panel cache."""
    global _clock
    _clock = clock or time.monotonic


def reset() -> None:
    """Forget the table and the panel cache (a sign out, and tests)."""
    global _built_at, _last_miss, _panel, _next
    with _lock:
        _entries.clear()
        _refs.clear()
        _built_at, _last_miss, _panel, _next = None, float("-inf"), None, 1


def mask_of(number: str) -> str:
    return MASK_DOTS + str(number)[-4:]


def refresh(force: bool = False) -> None:
    """Build the table, or read it again when it is older than an hour (or 'force'). A failed refresh of a table that
    exists keeps the old one for a minute more; the first build raises the SchwabError."""
    global _built_at, _next
    with _lock:
        now = _clock()
        if not force and _built_at is not None and now - _built_at < REFRESH_SECONDS:
            return
        try:
            pairs = [(str(p.accountNumber), p.hashValue) for p in endpoints.account_numbers()
                     if p is not None and p.accountNumber and p.hashValue]
        except SchwabError as error:
            if _built_at is None:
                raise
            log.warning("schwab account table not refreshed: %s", error.code)
            _built_at = now - REFRESH_SECONDS + RETRY_SECONDS
            return
        current = {number for number, _ in pairs}
        known = {number: ref for number, ref in _refs.items() if number in current}
        entries: dict[str, tuple[str, str, str]] = {}
        for number, hash_value in pairs:
            ref = known.get(number)
            if ref is None:                                   # a new account: the next number, never one that was used
                ref = f"A{_next}"
                _next += 1
                known[number] = ref
            entries[ref] = (number, hash_value, mask_of(number))
        _entries.clear()
        _entries.update(dict(sorted(entries.items(), key=lambda kv: int(kv[0][1:]))))
        _refs.clear()
        _refs.update(known)
        _built_at = now


def sensitive_values() -> list[str]:
    """Internal use only (the redaction of reader.py): every account number and hash of the table in memory. It reads the
    table as it is, without a refresh and without a call. Never return it from a route or a tool, never log it."""
    with _lock:
        return [value for number, hash_value, _ in _entries.values() for value in (number, hash_value) if value]


def hash_for(account_ref: Any) -> str:
    """The hash of Schwab paths for an 'account_ref'. SchwabError INVALID_PARAM for a reference that does not exist."""
    refresh()
    entry = _entries.get(account_ref) if isinstance(account_ref, str) else None
    if entry is None:
        raise SchwabError(INVALID_PARAM, 422, detail="unknown account_ref")
    return entry[1]


def listing() -> list[dict]:
    """[{account_ref, mask}] in table order: what the session card and the agent tools show."""
    refresh()
    return [{"account_ref": ref, "mask": entry[2]} for ref, entry in _entries.items()]


def identity(number: Any) -> dict:
    """{'account_ref', 'mask'} of an account number. A number the table does not know forces one refresh (at most one
    per 30 s); if it is still unknown, 'account_ref' is None and the mask is all that shows."""
    global _last_miss
    text = str(number)
    with _lock:
        if text not in _refs and _clock() - _last_miss >= MISS_SECONDS:
            _last_miss = _clock()
            try:
                refresh(force=True)
            except SchwabError:
                pass
        return {"account_ref": _refs.get(text), "mask": mask_of(text)}


# ------------------------------------------------------------------------------------------------ public


def public(obj: Any) -> Any:
    """The JSON of any object of 'models' (or a list, a dict) made safe to leave the backend: accountNumber becomes
    'account_ref' and 'mask' in the same object, 'hashValue' is dropped, UserDetails and StreamerInfo are reduced (5.9).
    Everything else, extra included, is kept."""
    if isinstance(obj, models.UserDetails):
        return _only(models.dump(obj), USER_KEEP)
    if isinstance(obj, models.StreamerInfo):
        return _only(models.dump(obj), STREAMER_KEEP)
    if isinstance(obj, models.Model):
        out: dict = {}
        for json_name, (attr, _) in type(obj)._by_json.items():
            value = getattr(obj, attr)
            if value is not None:
                _put(out, json_name, value)
        for key, value in obj.extra.items():
            _put(out, key, value)
        return out
    if isinstance(obj, models.QuoteResponse):
        return public(obj.to_json())
    if isinstance(obj, (list, tuple)):
        return [public(item) for item in obj]
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            _put(out, key, value)
        return out
    return obj


def _only(data: dict, keep: tuple[str, ...]) -> dict:
    return {key: data[key] for key in keep if key in data}


def _put(out: dict, key: str, value: Any) -> None:
    if key == "accountNumber":
        out.update(identity(value))
    elif key == "hashValue":
        pass
    elif key == "user" and isinstance(value, dict):                               # UserDetails left as raw JSON
        out[key] = _only(value, USER_KEEP)
    elif key == "streamerInfo" and isinstance(value, dict):
        out[key] = _only(value, STREAMER_KEEP)
    elif key == "streamerInfo" and isinstance(value, list):
        out[key] = [_only(item, STREAMER_KEEP) if isinstance(item, dict) else public(item) for item in value]
    else:
        out[key] = public(value)


# ------------------------------------------------------------------------------------------------- panel


def _pick(obj: Any, names: tuple[str, ...]) -> dict:
    return {name: getattr(obj, name, None) if obj is not None else None for name in names}


def _position(position: models.Position) -> dict:
    instrument = position.instrument
    out = {"symbol": getattr(instrument, "symbol", None), "assetType": getattr(instrument, "assetType", None)}
    out.update(_pick(position, _POSITION_FIELDS))
    return out


def _open_order(order: models.Order) -> dict:
    legs = [{"symbol": getattr(leg.instrument, "symbol", None), "instruction": leg.instruction, "quantity": leg.quantity}
            for leg in order.orderLegCollection or [] if isinstance(leg, models.OrderLegCollection)]
    return {"legs": legs, **_pick(order, _ORDER_FIELDS)}


def panel_data() -> dict:
    """The data of the 'Ver cuenta' panel (5.7), cached 15 seconds: GET /accounts?fields=positions and GET /orders of the
    last 59 days 23 hours, with the orders in an open state kept by Hokma. Shape:
    {'as_of': ISO UTC, 'accounts': [{'account_ref', 'mask', 'type': 'MARGIN'|'CASH',
      'currentBalances': (margin: equity, buyingPower, availableFunds | cash: totalCash, cashAvailableForTrading),
      'initialBalances': {'liquidationValue'}, 'positions': [{symbol, assetType, longQuantity, shortQuantity,
      averagePrice, marketValue, currentDayProfitLoss, currentDayProfitLossPercentage}],
      'orders': [{legs: [{symbol, instruction, quantity}], orderType, price, stopPrice, duration, status,
      filledQuantity, remainingQuantity, enteredTime}]}]}. No number and no hash."""
    global _panel
    now = _clock()
    with _lock:
        if _panel is not None and now - _panel[0] < PANEL_CACHE_SECONDS:
            return copy.deepcopy(_panel[1])
    refresh()
    moment = endpoints._now()
    listed = endpoints.accounts(None, fields="positions")
    placed = endpoints.orders(None, fromEnteredTime=moment - endpoints.ORDERS_RECENT, toEnteredTime=moment)
    open_by_ref: dict[Any, list[dict]] = {}
    for order in placed:
        if order is not None and order.status in OPEN_ORDER_STATUSES:
            open_by_ref.setdefault(identity(order.accountNumber)["account_ref"], []).append(_open_order(order))
    accounts_out = []
    for account in listed:
        secured = account.securitiesAccount if account is not None else None
        if secured is None:
            continue
        who = identity(secured.accountNumber)
        cash = isinstance(secured, models.CashAccount) or (secured.type == "CASH")
        accounts_out.append({
            **who, "type": "CASH" if cash else "MARGIN",
            "currentBalances": _pick(getattr(secured, "currentBalances", None),
                                     _CASH_BALANCES if cash else _MARGIN_BALANCES),
            "initialBalances": _pick(getattr(secured, "initialBalances", None), ("liquidationValue",)),
            "positions": [_position(p) for p in secured.positions or [] if isinstance(p, models.Position)],
            "orders": open_by_ref.get(who["account_ref"], []) if who["account_ref"] else [],
        })
    result = {"as_of": endpoints._format_time(moment), "accounts": accounts_out}
    with _lock:
        _panel = (now, result)
    return copy.deepcopy(result)
