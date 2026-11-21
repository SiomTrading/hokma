"""The Schwab broker (task 3299, SPEC-015 18.2): the real accounts of the student, read through `hokma.schwab`.

Accounts live in the tables of 0018 like any broker's; the link with the real account is a fingerprint (HMAC-SHA256 of the
hash Schwab uses in paths, with a 32 byte random key kept in the system credential store) and the mask, in
`broker_external_accounts`. The plain number and the hash are never stored, logged, returned or put in an error: outside
this module an account is its local id, its `account_ref` ("A1") and its mask ("••••1234").

The broker reads (balance, positions, orders) and DELEGATES the four writes (place, replace, cancel, preview) to
`hokma.schwab.orders`. The rules of 18.3 (switch, desk state, capital, limits, owner) are not decided here.
Blocking: call it from a thread.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Any

from hokma.broker.contract import BaseBroker, NotSupported
from hokma.broker.types import Balance, Capabilities, Order, Position
from hokma.db.repos.broker import MAX_NAME, BrokerAccount, BrokerRepo, BrokerRow
from hokma.db.repos.desks import MAX_CAPITAL, MIN_CAPITAL
from hokma.db.timeutil import utcnow

log = logging.getLogger("hokma.broker")

KIND = "schwab"
BROKER_NAME = "Schwab"
KEY_ENTRY = "schwab.fingerprint_key"          # entry of the system credential store (KeyStore): never a file
KEY_BYTES = 32


# ----------------------------------------------------------------------------------------------- fingerprint key


def _store():
    from hokma.connections import runtime

    return runtime.get_service().store


def load_key(create: bool = False) -> bytes | None:
    """The local key of the fingerprints, from the credential store. None when there is none (and `create` is false) or
    the store is not available. With `create`, a missing key is generated and saved; no store raises
    `StoreUnavailable` (the service answers `connections.store_unavailable`)."""
    from hokma.connections.store import StoreUnavailable

    store = _store()
    saved = store.get(KEY_ENTRY)
    if saved:
        try:
            key = bytes.fromhex(saved)
            if len(key) == KEY_BYTES:
                return key
        except ValueError:
            pass
        log.warning("the saved fingerprint key is malformed")
        if not create:
            return None
    if not create:
        return None
    if not store.available():
        raise StoreUnavailable()
    fresh = secrets.token_hex(KEY_BYTES)
    store.set(KEY_ENTRY, fresh)                   # raises StoreUnavailable when the store refuses
    return bytes.fromhex(fresh)


def fingerprint(key: bytes, hash_value: str) -> str:
    """HMAC-SHA256 of the hash of an account with the local key, in hex. Stable: the same account gives the same value."""
    return hmac.new(key, str(hash_value).encode("utf-8"), hashlib.sha256).hexdigest()


# ------------------------------------------------------------------------------------------------ balance values


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def balance_values(securities: dict) -> tuple[float | None, float | None, float | None]:
    """(cash, equity, buying_power) of a public `securitiesAccount` (5.7).
    Margin: cash is `currentBalances.availableFunds`, equity `currentBalances.equity`, buying power `buyingPower`.
    Cash account: cash is `cashAvailableForTrading`; equity is `totalCash` plus `longNonMarginableMarketValue` (what the
    account holds in cash and in securities), or the cash alone when Schwab does not give the securities."""
    current = securities.get("currentBalances")
    current = current if isinstance(current, dict) else {}
    is_cash = securities.get("type") == "CASH" or (securities.get("type") is None and "totalCash" in current)
    if is_cash:
        total, held = _num(current.get("totalCash")), _num(current.get("longNonMarginableMarketValue"))
        equity = None if total is None else total + (held or 0.0)
        return _num(current.get("cashAvailableForTrading")), equity, _num(current.get("cashAvailableForTrading"))
    return _num(current.get("availableFunds")), _num(current.get("equity")), _num(current.get("buyingPower"))


def _securities(public_account: Any) -> dict:
    inner = public_account.get("securitiesAccount") if isinstance(public_account, dict) else None
    return inner if isinstance(inner, dict) else {}


def _orders_module():
    import importlib

    return importlib.import_module("hokma.schwab.orders")


# ----------------------------------------------------------------------------------------------------- the broker


class SchwabBroker(BaseBroker):
    capabilities = Capabilities(accounts=True, reset=False, orders=True, positions=True)

    # ---- who an account is ----

    def mask_of(self, id: int) -> str | None:
        link = self.repo.external_of(self.account(id).id)
        return link.mask if link else None

    def account_ref(self, id: int) -> str:
        """The `account_ref` ("A1") of a local account in this run: the one whose hash gives the saved fingerprint.
        SchwabError NOT_CONNECTED while there is no sign-in; NOT_FOUND when Schwab no longer lists the account."""
        from hokma.schwab import accounts
        from hokma.schwab.http import NOT_FOUND, SchwabError

        link = self.repo.external_of(self.account(id).id)
        key = load_key()
        if link is None or key is None:
            raise SchwabError(NOT_FOUND, 404, detail="account is not linked to Schwab")
        for entry in accounts.listing():
            ref = entry["account_ref"]
            if hmac.compare_digest(fingerprint(key, accounts.hash_for(ref)), link.fingerprint):
                return ref
        raise SchwabError(NOT_FOUND, 404, detail="account is not listed by Schwab")

    # ---- reads ----

    def balance(self, id: int) -> Balance:
        """The cash and equity of the real account. `None` (never a made-up 0) while Schwab cannot be read."""
        from hokma.schwab import accounts, endpoints
        from hokma.schwab.http import SchwabError

        account = self.account(id)
        try:
            data = accounts.public(endpoints.accounts(self.account_ref(id)))
        except SchwabError as error:
            log.info("schwab balance not available: %s", error.code)
            return Balance(cash=None, equity=None, currency=account.currency, as_of=utcnow())
        cash, equity, power = balance_values(_securities(data))
        return Balance(cash=cash, equity=equity, currency=account.currency, as_of=utcnow(), buying_power=power)

    def positions(self, account_id: int) -> list[Position]:
        from hokma.schwab import accounts, endpoints

        ref, mask = self.account_ref(account_id), self.mask_of(account_id)
        data = accounts.public(endpoints.accounts(ref, fields="positions"))
        found = _securities(data).get("positions")
        return [_position(account_id, ref, mask, p) for p in (found or []) if isinstance(p, dict)]

    def orders(self, account_id: int, status: str | None = None, fromEnteredTime: Any = None,  # noqa: N803
               toEnteredTime: Any = None, maxResults: int | None = None) -> list[Order]:
        """The orders of the account (REF 3.2), with the filters of the endpoint."""
        from hokma.schwab import accounts, endpoints

        ref, mask = self.account_ref(account_id), self.mask_of(account_id)
        listed = endpoints.orders(ref, fromEnteredTime=fromEnteredTime, toEnteredTime=toEnteredTime, status=status,
                                  maxResults=maxResults)
        return [_order(account_id, ref, mask, accounts.public(o)) for o in listed if o is not None]

    def get_order(self, account_id: int, order_id: Any) -> Order | None:
        from hokma.schwab import accounts, endpoints

        ref, mask = self.account_ref(account_id), self.mask_of(account_id)
        found = endpoints.order(ref, order_id)
        return None if found is None else _order(account_id, ref, mask, accounts.public(found))

    # ---- writes: only delegated (the rules are not decided here) ----

    def place_order(self, account_id: int, order: dict) -> dict:
        return _orders_module().place_order(self.account_ref(account_id), order)

    def replace_order(self, account_id: int, order_id: Any, order: dict) -> dict:
        return _orders_module().replace_order(self.account_ref(account_id), order_id, order)

    def cancel_order(self, account_id: int, order_id: Any) -> dict:
        return _orders_module().cancel_order(self.account_ref(account_id), order_id)

    def preview_order(self, account_id: int, order: dict) -> Any:
        return _orders_module().preview_order(self.account_ref(account_id), order)

    # ---- what a real account cannot do ----

    def reset(self, id: int) -> BrokerAccount:
        raise NotSupported("a Schwab account cannot be reset: its money is in Schwab")

    def create_account(self, name: str, capital: int) -> BrokerAccount:
        raise NotSupported("Schwab accounts come from the sign-in, they are not created in Hokma")


def _leg(leg: dict) -> dict[str, Any]:
    instrument = leg.get("instrument") if isinstance(leg.get("instrument"), dict) else {}
    return {"symbol": instrument.get("symbol"), "asset_type": instrument.get("assetType"),
            "instruction": leg.get("instruction"), "position_effect": leg.get("positionEffect"),
            "quantity": leg.get("quantity")}


def _order(account_id: int, ref: str, mask: str | None, data: dict) -> Order:
    legs = tuple(_leg(leg) for leg in data.get("orderLegCollection") or [] if isinstance(leg, dict))
    first = legs[0] if legs else {}
    order_id = data.get("orderId")
    qty = _num(data.get("quantity"))
    return Order(
        account_id=account_id, symbol=first.get("symbol") or "", side=first.get("instruction") or "",
        qty=qty if qty is not None else (_num(first.get("quantity")) or 0.0),
        type=data.get("orderType") or "", limit_price=_num(data.get("price")), order_id=None if order_id is None else str(order_id),
        status=data.get("status"), duration=data.get("duration"), stop_price=_num(data.get("stopPrice")),
        filled_qty=_num(data.get("filledQuantity")), remaining_qty=_num(data.get("remainingQuantity")),
        entered_at=data.get("enteredTime"), cancelable=data.get("cancelable"), editable=data.get("editable"),
        account_ref=ref, mask=mask, legs=legs, data=data)


def _position(account_id: int, ref: str, mask: str | None, data: dict) -> Position:
    instrument = data.get("instrument") if isinstance(data.get("instrument"), dict) else {}
    long_qty, short_qty = _num(data.get("longQuantity")) or 0.0, _num(data.get("shortQuantity")) or 0.0
    opens = [v for v in (_num(data.get("longOpenProfitLoss")), _num(data.get("shortOpenProfitLoss"))) if v is not None]
    return Position(
        account_id=account_id, symbol=instrument.get("symbol") or "", qty=long_qty - short_qty,
        avg_price=_num(data.get("averagePrice")) or 0.0, asset_type=instrument.get("assetType"),
        long_qty=long_qty, short_qty=short_qty, market_value=_num(data.get("marketValue")),
        day_pl=_num(data.get("currentDayProfitLoss")), day_pl_pct=_num(data.get("currentDayProfitLossPercentage")),
        open_pl=sum(opens) if opens else None, multiplier=_num(instrument.get("optionMultiplier")),
        put_call=instrument.get("putCall"), underlying=instrument.get("underlyingSymbol"),
        account_ref=ref, mask=mask, data=data)


# ------------------------------------------------------------------------- connect and remove (SPEC-015 18.2 points 2 and 3)


def ensure_broker(repo: BrokerRepo) -> BrokerRow:
    """The broker row of Schwab; created once, with the orders switch off. Idempotent. A name already taken by another
    broker ("Schwab" typed by hand) is not touched: the new row takes the next free name."""
    for row in repo.list_brokers():
        if row.kind == KIND:
            if "orders_enabled" not in row.config:
                return repo.set_broker_config(row.id, {**row.config, "orders_enabled": False})
            return row
    name, n = BROKER_NAME, 1
    while repo.find_broker(name) is not None:
        n += 1
        name = f"{BROKER_NAME} {n}"
    return repo.create_broker(KIND, name, {"orders_enabled": False})


def _account_name(repo: BrokerRepo, broker_id: int, nick: str | None, mask: str) -> str:
    base = (nick or "").strip() or f"{BROKER_NAME} {mask}"
    for candidate in (base[:MAX_NAME], f"{base[:MAX_NAME - len(mask) - 1]} {mask}"):
        if repo.find_account_by_name(broker_id, candidate) is None:
            return candidate
    n = 2
    while True:
        suffix = f" {mask} {n}"
        candidate = f"{base[:MAX_NAME - len(suffix)]}{suffix}"
        if repo.find_account_by_name(broker_id, candidate) is None:
            return candidate
        n += 1


def _initial_capital(value: float | None) -> int:
    """The value at the moment of linking, as the whole number the CHECK of `broker_accounts` accepts (100 to 1,000,000,000)."""
    if value is None:
        return MIN_CAPITAL
    return int(max(MIN_CAPITAL, min(MAX_CAPITAL, round(value))))


def _read_schwab() -> list[dict[str, Any]]:
    """[{fingerprint, mask, nick, equity}] for every account Schwab lists, read BEFORE the database is opened."""
    from hokma.schwab import accounts, endpoints
    from hokma.schwab.http import SchwabError

    key = load_key(create=True)
    listing = accounts.listing()
    nicks: dict[str, str] = {}
    try:
        preference = endpoints.user_preference()
        for item in (preference.accounts or []) if preference is not None else []:
            who = accounts.identity(item.accountNumber)
            if who["account_ref"] and item.nickName:
                nicks[who["account_ref"]] = str(item.nickName)
    except SchwabError as error:
        log.info("schwab nicknames not read: %s", error.code)
    equity: dict[str, float | None] = {}
    try:
        for account in endpoints.accounts():
            data = _securities(accounts.public(account))
            _, value, _ = balance_values(data)
            if data.get("account_ref"):
                equity[data["account_ref"]] = value
    except SchwabError as error:
        log.info("schwab balances at link time not read: %s", error.code)
    return [{"fingerprint": fingerprint(key, accounts.hash_for(e["account_ref"])), "mask": e["mask"],
             "nick": nicks.get(e["account_ref"]), "equity": equity.get(e["account_ref"])} for e in listing]


def sync_accounts(db=None) -> list[int]:
    """Schwab was connected: make sure the broker and one local account per Schwab account exist, and that the ones Schwab
    lists are active. An account seen before (same fingerprint) is brought back, not duplicated; one Schwab no longer
    lists is archived. Idempotent. Returns the local ids of the accounts that are active now. Raises `StoreUnavailable`
    when there is no credential store for the key, `SchwabError` when Schwab cannot be read."""
    from hokma.db import get_database

    found = _read_schwab()
    with (db or get_database()).write() as u:
        repo = u.broker
        broker = ensure_broker(repo)
        seen: list[int] = []
        for item in found:
            existing = repo.find_by_fingerprint(item["fingerprint"])
            if existing is not None:
                if existing.status != "active":
                    repo.set_account_status(existing.id, "active")
                repo.update_external_mask(existing.id, item["mask"])
                seen.append(existing.id)
                continue
            name = _account_name(repo, broker.id, item["nick"], item["mask"])
            account = repo.create_account(broker.id, name, _initial_capital(item["equity"]))
            repo.add_external(account.id, item["fingerprint"], item["mask"])
            seen.append(account.id)
        for account in repo.list_accounts(broker.id):
            if account.id not in seen:
                repo.archive_account(account.id)
        return seen


def archive_accounts(db=None) -> int:
    """Schwab was removed: its accounts become `archived` (nothing is deleted) and the orders switch goes off. Returns how
    many accounts changed. Reconnecting brings them back by fingerprint."""
    from hokma.db import get_database

    with (db or get_database()).write() as u:
        rows = [r for r in u.broker.list_brokers() if r.kind == KIND]
        changed = 0
        for row in rows:
            changed += u.broker.archive_broker_accounts(row.id)
            if row.config.get("orders_enabled"):
                u.broker.set_broker_config(row.id, {**row.config, "orders_enabled": False})
        return changed
