"""What every outside broker connector shares (SPEC-018 B5: OA-55): how an account of an outside broker is known without its identifier, the
neutral order every connector reads, the errors a connector raises, and the way the service opens a connector by `brokers.kind`.

An account of an outside broker is a local row of `broker_accounts` plus a row of `broker_external_accounts`: a fingerprint (HMAC-SHA256 of the
identifier with a 32 byte random key kept in the system credential store, one entry per kind) and a mask ("\u2022\u2022\u2022\u20221234"). The identifier
itself is never stored, logged, returned or put in an error. Outside a connector an account is its local id, its `account_ref` ("A1") and its mask.

The neutral order is what `hokma.exec.submit.to_native` builds for any kind other than schwab. `parse_order` reads it again from the real
contract ids: nothing the caller wrote (the provider symbol, the asset type) is believed, it is derived from the id.
Blocking: call it from a thread.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import logging
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hokma.broker.contract import BaseBroker, NotSupported
from hokma.contracts import symbols
from hokma.contracts.identity import NoContract, canonical_id, parse_canonical
from hokma.db.repos.broker import MAX_NAME, BrokerRepo, BrokerRow
from hokma.db.repos.desks import MAX_CAPITAL, MIN_CAPITAL
from hokma.exec.money import ZERO, MoneyError, dec

log = logging.getLogger("hokma.broker")

KEY_BYTES = 32
MASK_DOTS = "\u2022" * 4
CONNECTORS = {"alpaca": ("hokma.broker.alpaca", "AlpacaBroker"), "ibkr": ("hokma.broker.ibkr", "IbkrBroker"),
              "crypto": ("hokma.broker.crypto", "CryptoBroker")}
BROKER_NAMES = {"alpaca": "Alpaca", "ibkr": "IBKR", "crypto": "Crypto"}

# Codes of ConnectorError (what `hokma.exec` stores as the code of a refusal): stable words, never a provider text.
UNAVAILABLE = "unavailable"
NOT_SIGNED_IN = "not_signed_in"
NO_KEYS = "no_keys"
BAD_ORDER = "bad_order"
NO_INSTRUMENT = "no_instrument"
UNSUPPORTED = "unsupported"
NOT_LISTED = "account_not_listed"


class ConnectorError(Exception):
    """A connector could not do what it was asked. `code` is stable; `detail` is a short English sentence with no identifier,
    key or provider text. `hokma.exec.approvals` stores both as the reason of a refused order."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code, self.detail = code, detail or code.replace("_", " ")


def connector_class(kind: str) -> type[BaseBroker]:
    """The class of a connector kind, imported on first use (a library of a connector loads only when that connector is opened)."""
    module, name = CONNECTORS[kind]
    return getattr(importlib.import_module(module), name)


# ------------------------------------------------------------------------------------------------ fingerprint and mask


def _store():
    from hokma.connections import runtime

    return runtime.get_service().store


def load_key(entry: str, create: bool = False) -> bytes | None:
    """The local key of the fingerprints, from the credential store (never a file). None when there is none and `create` is false, or the
    store is not available. With `create`, a missing key is generated and saved; no store raises `StoreUnavailable`."""
    from hokma.connections.store import StoreUnavailable

    store = _store()
    saved = store.get(entry)
    if saved:
        try:
            key = bytes.fromhex(saved)
            if len(key) == KEY_BYTES:
                return key
        except ValueError:
            pass
        log.warning("a saved fingerprint key is malformed")
    if not create:
        return None
    if not store.available():
        raise StoreUnavailable()
    fresh = secrets.token_hex(KEY_BYTES)
    store.set(entry, fresh)
    return bytes.fromhex(fresh)


def fingerprint(key: bytes, identifier: str) -> str:
    """HMAC-SHA256 of an account identifier with the local key, in hex. Stable: the same account gives the same value."""
    return hmac.new(key, str(identifier).encode("utf-8"), hashlib.sha256).hexdigest()


def mask(identifier: str) -> str:
    """Four dots and the last four characters of the identifier."""
    return MASK_DOTS + str(identifier)[-4:]


class Scrubber:
    """Makes an object safe to leave the connector: a key that names an account is dropped and every known identifier inside a text becomes
    its mask. `public` returns a new object; the argument is not changed."""

    DROP = frozenset({"accountnumber", "account_number", "accountid", "account_id", "acctid", "acct", "account", "accountvan", "accounttitle",
                      "accountalias", "hashvalue", "apikey", "api_key", "secret", "password", "uid", "owner"})

    def __init__(self, identifiers: Mapping[str, str] | None = None) -> None:
        """`identifiers` maps each known identifier to the text that replaces it (its mask)."""
        self._items = sorted((identifiers or {}).items(), key=lambda item: -len(item[0]))

    def text(self, value: str) -> str:
        for identifier, masked in self._items:
            if identifier and identifier in value:
                value = value.replace(identifier, masked)
        return value

    def public(self, obj: Any) -> Any:
        if isinstance(obj, Mapping):
            return {k: self.public(v) for k, v in obj.items() if str(k).lower() not in self.DROP}
        if isinstance(obj, (list, tuple)):
            return [self.public(v) for v in obj]
        if isinstance(obj, str):
            return self.text(obj)
        return obj


# ----------------------------------------------------------------------------------------------- broker rows and accounts


def ensure_broker(repo: BrokerRepo, kind: str) -> BrokerRow:
    """The broker row of a connector kind; created once, with the orders switch OFF. Idempotent. A name taken by another broker is not touched:
    the new row takes the next free name. The switch is never turned on here: only the owner does that."""
    for row in repo.list_brokers():
        if row.kind == kind:
            if "orders_enabled" not in row.config:
                return repo.set_broker_config(row.id, {**row.config, "orders_enabled": False})
            return row
    base = BROKER_NAMES[kind]
    name, n = base, 1
    while repo.find_broker(name) is not None:
        n += 1
        name = f"{base} {n}"
    return repo.create_broker(kind, name, {"orders_enabled": False})


def _account_name(repo: BrokerRepo, broker_id: int, base_name: str, nick: str | None, masked: str) -> str:
    base = (nick or "").strip() or f"{base_name} {masked}"
    for candidate in (base[:MAX_NAME], f"{base[:MAX_NAME - len(masked) - 1]} {masked}"):
        if repo.find_account_by_name(broker_id, candidate) is None:
            return candidate
    n = 2
    while True:
        suffix = f" {masked} {n}"
        candidate = f"{base[:MAX_NAME - len(suffix)]}{suffix}"
        if repo.find_account_by_name(broker_id, candidate) is None:
            return candidate
        n += 1


def initial_capital(value: Any) -> int:
    """The value at the moment of linking, as the whole number the CHECK of `broker_accounts` accepts."""
    if value is None:
        return MIN_CAPITAL
    return int(max(MIN_CAPITAL, min(MAX_CAPITAL, round(float(value)))))


def sync_accounts(kind: str, listing: list[dict[str, Any]], *, key_entry: str, db=None) -> list[int]:
    """The connector listed its accounts (`[{identifier, nick, equity, mask?}]`, read BEFORE the database is opened): make sure the broker row and
    one local account per listed account exist and are active. An account seen before (same fingerprint) is brought back, not duplicated; one
    the provider no longer lists is archived. Idempotent. Returns the local ids that are active now. The identifier is used only to compute
    the fingerprint and the mask."""
    from hokma.db import get_database

    key = load_key(key_entry, create=True)
    found = [{"fingerprint": fingerprint(key, str(item["identifier"])), "mask": item.get("mask") or mask(str(item["identifier"])),
              "nick": item.get("nick"), "equity": item.get("equity")} for item in listing]
    with (db or get_database()).write() as u:
        repo = u.broker
        broker = ensure_broker(repo, kind)
        seen: list[int] = []
        for item in found:
            existing = repo.find_by_fingerprint(item["fingerprint"])
            if existing is not None:
                if existing.status != "active":
                    repo.set_account_status(existing.id, "active")
                repo.update_external_mask(existing.id, item["mask"])
                seen.append(existing.id)
                continue
            name = _account_name(repo, broker.id, BROKER_NAMES[kind], item["nick"], item["mask"])
            account = repo.create_account(broker.id, name, initial_capital(item["equity"]))
            repo.add_external(account.id, item["fingerprint"], item["mask"])
            seen.append(account.id)
        for account in repo.list_accounts(broker.id):
            if account.id not in seen:
                repo.archive_account(account.id)
        return seen


class ExternalBroker(BaseBroker):
    """The part of a connector that is the same for every outside broker: who an account is, and what a real account cannot do.
    A connector gives `kind`, `key_entry` and `live_identifiers()` (the identifiers the provider lists now, read from the provider)."""

    kind_name: str = ""
    key_entry: str = ""

    def live_identifiers(self) -> list[str]:
        raise NotImplementedError

    def mask_for(self, identifier: str) -> str:
        return mask(identifier)

    def mask_of(self, id: int) -> str | None:
        link = self.repo.external_of(self.account(id).id)
        return link.mask if link else None

    def _match(self, id: int, identifiers: list[str]) -> tuple[int, str]:
        """(position, identifier) of the listed account whose fingerprint is the saved one of the local account `id`."""
        link = self.repo.external_of(self.account(id).id)
        key = load_key(self.key_entry)
        if link is None or key is None:
            raise ConnectorError(NOT_LISTED, "the account is not linked to this broker")
        for n, identifier in enumerate(identifiers):
            if hmac.compare_digest(fingerprint(key, identifier), link.fingerprint):
                return n, identifier
        raise ConnectorError(NOT_LISTED, "the broker does not list this account")

    def account_ref(self, id: int) -> str:
        """The `account_ref` ("A1") of a local account in this run: its place in what the provider lists now."""
        return f"A{self._match(id, self.live_identifiers())[0] + 1}"

    def scrubber(self, identifiers: list[str] | None = None) -> Scrubber:
        found = self.live_identifiers() if identifiers is None else identifiers
        return Scrubber({i: self.mask_for(i) for i in found})

    def reset(self, id: int):
        raise NotSupported("an account of an outside broker cannot be reset: its money is in the broker")

    def create_account(self, name: str, capital: int):
        raise NotSupported("accounts of an outside broker come from its keys, they are not created in Hokma")


# ---------------------------------------------------------------------------------------------------------- the neutral order

ORDER_KEYS = {"fingerprint", "type", "tif", "limit_price", "stop_price", "stop_loss", "take_profit", "reduce_only", "legs"}
LEG_KEYS = {"contract_id", "symbol", "side", "effect", "qty", "multiplier", "tick_size", "asset_type"}
TYPES = ("market", "limit", "stop", "stop_limit")
TIFS = ("day", "gtc")
MAX_LEGS = 8
_ID = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")


@dataclass(frozen=True, slots=True)
class Leg:
    contract_id: str
    symbol: str                  # the provider's, written from the contract id
    side: str
    effect: str
    qty: Decimal
    multiplier: Decimal
    asset_type: str              # of the contract id, not of what the caller said


@dataclass(frozen=True, slots=True)
class Neutral:
    fingerprint: str
    type: str
    tif: str
    limit_price: Decimal | None
    stop_price: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    reduce_only: bool
    legs: tuple[Leg, ...]


def _price(value: Any, what: str) -> Decimal | None:
    if value is None:
        return None
    try:
        out = dec(value, what)
    except MoneyError as exc:
        raise ConnectorError(BAD_ORDER, f"{what} is not a valid number") from exc
    if out <= ZERO:
        raise ConnectorError(BAD_ORDER, f"{what} must be above zero")
    return out


def parse_order(order: Any, provider: str) -> Neutral:
    """The neutral order, validated. The provider symbol of each leg is written from its canonical contract id with `symbols.to_provider`; the
    contract id must round trip exactly. Raises ConnectorError (BAD_ORDER, NO_INSTRUMENT)."""
    if not isinstance(order, Mapping) or set(order) - ORDER_KEYS or not isinstance(order.get("legs"), (list, tuple)) or not order["legs"]:
        raise ConnectorError(BAD_ORDER, "the order is not in the neutral shape")
    if len(order["legs"]) > MAX_LEGS:
        raise ConnectorError(BAD_ORDER, "the order has too many legs")
    if order.get("type") not in TYPES or order.get("tif") not in TIFS:
        raise ConnectorError(BAD_ORDER, "type or tif is not valid")
    if not isinstance(order.get("reduce_only", False), bool):
        raise ConnectorError(BAD_ORDER, "reduce_only must be true or false")
    legs = []
    for item in order["legs"]:
        if not isinstance(item, Mapping) or set(item) - LEG_KEYS or not {"contract_id", "side", "qty"} <= set(item):
            raise ConnectorError(BAD_ORDER, "a leg is not in the neutral shape")
        cid = item["contract_id"]
        if not isinstance(cid, str) or not _ID.fullmatch(cid) or item["side"] not in ("buy", "sell") or item.get("effect", "open") not in ("open", "close"):
            raise ConnectorError(BAD_ORDER, "a leg has an invalid contract id, side or effect")
        try:
            parsed = parse_canonical(cid)
            if canonical_id(parsed) != cid:
                raise NoContract(cid, "the contract id does not round trip")
            provider_symbol = symbols.to_provider(cid, provider)
        except NoContract as exc:
            raise ConnectorError(NO_INSTRUMENT, f"{provider} does not carry {cid}") from exc
        except Exception as exc:                         # a malformed id of any kind is a bad order, not a crash
            raise ConnectorError(BAD_ORDER, "a leg has an invalid contract id") from exc
        qty = _price(item["qty"], "qty")
        multiplier = _price(item.get("multiplier", "1"), "multiplier")
        assert qty is not None and multiplier is not None
        legs.append(Leg(cid, provider_symbol, item["side"], item.get("effect", "open"), qty, multiplier, parsed.asset_type))
    limit, stop = _price(order.get("limit_price"), "limit_price"), _price(order.get("stop_price"), "stop_price")
    kind = order["type"]
    if (kind in ("limit", "stop_limit") and limit is None) or (kind in ("stop", "stop_limit") and stop is None):
        raise ConnectorError(BAD_ORDER, f"a {kind} order needs its price")
    return Neutral(str(order.get("fingerprint") or "")[:128], kind, order["tif"], limit, stop, _price(order.get("stop_loss"), "stop_loss"),
                   _price(order.get("take_profit"), "take_profit"), bool(order.get("reduce_only", False)), tuple(legs))


def reduce_legs(legs: tuple[Leg, ...], held: Callable[[str], Decimal]) -> tuple[Leg, ...]:
    """A reduce-only order is cut to the position the broker really holds (`held(contract_id)`, signed); with nothing to reduce it is refused.
    The flag is never believed: the position is read from the broker at this moment."""
    from hokma.exec.money import clip_reduce_only

    out = []
    for leg in legs:
        qty, _clipped = clip_reduce_only(leg.side, leg.qty, held(leg.contract_id))
        if qty <= ZERO:
            raise ConnectorError("reduce_only_nothing", f"there is no {leg.contract_id} position to reduce")
        out.append(Leg(leg.contract_id, leg.symbol, leg.side, leg.effect, qty, leg.multiplier, leg.asset_type))
    return tuple(out)


def single_leg(order: Neutral, what: str) -> Leg:
    """Connectors send one leg orders; a multi-leg order or a bracket is refused with its reason instead of being cut."""
    if len(order.legs) != 1:
        raise ConnectorError(UNSUPPORTED, f"{what} does not take multi-leg orders yet")
    return order.legs[0]


def answer(order_id: str | None, status: str, *, filled: Decimal | None = None, avg: Decimal | None = None, reason: str | None = None) -> dict[str, Any]:
    """What `place_order` and `replace_order` return: the status word of the sim broker (FILLED, WORKING, PARTIALLY_FILLED, REJECTED...)."""
    out: dict[str, Any] = {"order_id": order_id, "status": status}
    if filled is not None:
        out["filled_qty"] = format(filled, "f")
    if avg is not None:
        out["avg_price"] = format(avg, "f")
    if reason:
        out["reason"] = reason[:64]
    return out


def refused(reason: str) -> dict[str, Any]:
    return answer(None, "REJECTED", reason=reason)
