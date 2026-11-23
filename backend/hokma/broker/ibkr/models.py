"""Typed readers of the answers of the IBKR web API (SPEC-018 B5: OA-72). Nothing here calls the network.

Tolerant reading: a field the reader does not know goes to `extra` (it does not break the parse), a field that is missing is None, and a number
is read through its shortest text into a Decimal (never a binary float calculation). An account identifier never comes out of a reader: every
model carries `account_ref` ("A1") and `mask` (four dots and the last four characters), given by a `Masker` that knows the identifiers the
gateway listed. `public(obj, masker)` makes any raw object safe to keep: the keys that name an account are dropped and the identifiers inside a
text become their mask.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from hokma.broker.external import MASK_DOTS, Scrubber, mask as mask_of
from hokma.exec.money import MoneyError, dec_or_none

ACCOUNT_FIELDS = frozenset({"acct", "acctid", "accountid", "account", "accountvan", "accounttitle", "accountalias", "aliasacct", "displayname", "desc"})
STATUS = {"presubmitted": "WORKING", "pendingsubmit": "WORKING", "submitted": "WORKING", "pendingcancel": "WORKING", "apipending": "WORKING",
          "filled": "FILLED", "cancelled": "CANCELED", "canceled": "CANCELED", "apicancelled": "CANCELED", "inactive": "REJECTED",
          "rejected": "REJECTED", "expired": "EXPIRED"}
ORDER_TYPES = {"mkt": "market", "market": "market", "lmt": "limit", "limit": "limit", "stp": "stop", "stop": "stop", "stp lmt": "stop_limit",
               "stop limit": "stop_limit"}
_OCC = re.compile(r"([A-Z][A-Z.]{0,5})\s+(\d{6}[CP]\d{8})")


def number(value: Any) -> Decimal | None:
    """A number of an answer as Decimal; None when it is missing, not a number, or the text has a thousands comma and a currency
    ("1,001.00 USD" is read as 1001.00)."""
    if isinstance(value, str):
        cleaned = value.replace(",", "").split(" ")[0].strip()
        value = cleaned or None
    try:
        return dec_or_none(value)
    except MoneyError:
        return None


class Masker:
    """Who an account is outside the connector: `ref` and `mask` of an identifier the gateway listed (the order of the list gives the ref)."""

    def __init__(self, identifiers: list[str], masks: Mapping[str, str] | None = None) -> None:
        self._index = {identifier: n + 1 for n, identifier in enumerate(identifiers)}
        self._masks = dict(masks or {})

    def ref(self, identifier: Any) -> str | None:
        n = self._index.get(str(identifier))
        return None if n is None else f"A{n}"

    def mask(self, identifier: Any) -> str | None:
        if identifier is None or str(identifier) == "":
            return None
        return self._masks.get(str(identifier)) or mask_of(str(identifier))

    def scrubber(self) -> Scrubber:
        return Scrubber({i: self.mask(i) or MASK_DOTS for i in self._index})


def public(obj: Any, masker: Masker) -> Any:
    """`obj` made safe to keep: keys that name an account are dropped, and every listed identifier inside a text becomes its mask."""
    scrub = masker.scrubber()

    def walk(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {k: walk(v) for k, v in value.items() if str(k).lower() not in ACCOUNT_FIELDS and str(k).lower() not in Scrubber.DROP}
        if isinstance(value, (list, tuple)):
            return [walk(v) for v in value]
        return scrub.text(value) if isinstance(value, str) else value

    return walk(obj)


def _known(data: Mapping[str, Any], names: set[str]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in names and k.lower() not in ACCOUNT_FIELDS}


@dataclass(frozen=True, slots=True)
class AuthStatus:
    authenticated: bool
    connected: bool
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.authenticated and self.connected


def parse_auth(data: Any) -> AuthStatus:
    data = data if isinstance(data, Mapping) else {}
    return AuthStatus(data.get("authenticated") is True, data.get("connected") is True, _known(data, {"authenticated", "connected"}))


def parse_account_ids(data: Any) -> list[str]:
    """The identifiers of `GET /portfolio/accounts` (a list of objects with `id`) or of `GET /iserver/accounts` (`{"accounts": [...]}`).
    They are for the connector's own use (fingerprint, mask): they never go into a model."""
    items = data.get("accounts") if isinstance(data, Mapping) else data
    out: list[str] = []
    for item in items if isinstance(items, list) else []:
        value = item.get("id") or item.get("accountId") if isinstance(item, Mapping) else item
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out


@dataclass(frozen=True, slots=True)
class Summary:
    cash: Decimal | None
    equity: Decimal | None
    buying_power: Decimal | None
    currency: str | None
    account_ref: str | None
    mask: str | None
    extra: dict[str, Any] = field(default_factory=dict)


def _amount(data: Mapping[str, Any], key: str) -> tuple[Decimal | None, str | None]:
    item = data.get(key)
    if isinstance(item, Mapping) and item.get("isNull") is not True:
        return number(item.get("amount")), item.get("currency") if isinstance(item.get("currency"), str) else None
    return None, None


def parse_summary(data: Any, identifier: str, masker: Masker) -> Summary:
    """`GET /portfolio/{account}/summary`: cash is the available funds, equity the net liquidation value."""
    data = data if isinstance(data, Mapping) else {}
    cash, currency = _amount(data, "availablefunds")
    equity, _ = _amount(data, "netliquidation")
    power, _ = _amount(data, "buyingpower")
    return Summary(cash, equity, power, currency, masker.ref(identifier), masker.mask(identifier), {})


@dataclass(frozen=True, slots=True)
class IbkrPosition:
    conid: int | None
    symbol: str
    asset_class: str | None
    qty: Decimal
    avg_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    multiplier: Decimal | None
    put_call: str | None
    contract_symbol: str | None           # the provider symbol of an option (OCC), when the description carries it
    account_ref: str | None
    mask: str | None
    extra: dict[str, Any] = field(default_factory=dict)


_POSITION_KEYS = {"conid", "ticker", "contractDesc", "assetClass", "position", "avgPrice", "mktValue", "unrealizedPnl", "multiplier", "putOrCall"}


def parse_position(data: Any, masker: Masker) -> IbkrPosition | None:
    if not isinstance(data, Mapping):
        return None
    desc = data.get("contractDesc") if isinstance(data.get("contractDesc"), str) else ""
    occ = _OCC.search(desc)
    conid = data.get("conid")
    return IbkrPosition(int(conid) if isinstance(conid, (int, str)) and str(conid).isdigit() else None,
                        str(data.get("ticker") or desc.split(" ")[0] or ""), data.get("assetClass") if isinstance(data.get("assetClass"), str) else None,
                        number(data.get("position")) or Decimal(0), number(data.get("avgPrice")), number(data.get("mktValue")),
                        number(data.get("unrealizedPnl")), number(data.get("multiplier")),
                        data.get("putOrCall") if isinstance(data.get("putOrCall"), str) else None,
                        f"{occ.group(1)}{occ.group(2)}" if occ else None, masker.ref(data.get("acctId")), masker.mask(data.get("acctId")),
                        public(_known(data, _POSITION_KEYS | {"acctId"}), masker))


@dataclass(frozen=True, slots=True)
class IbkrOrder:
    order_id: str | None
    conid: int | None
    symbol: str
    side: str
    qty: Decimal | None
    filled: Decimal | None
    remaining: Decimal | None
    order_type: str
    price: Decimal | None
    stop_price: Decimal | None
    status: str                           # WORKING, FILLED, CANCELED, REJECTED, EXPIRED (the words of the sim broker)
    raw_status: str | None
    tif: str | None
    entered_at: str | None
    account_ref: str | None
    mask: str | None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def partially_filled(self) -> bool:
        return self.status == "WORKING" and bool(self.filled) and self.filled > 0


_ORDER_KEYS = {"orderId", "conid", "ticker", "side", "totalSize", "filledQuantity", "remainingQuantity", "origOrderType", "orderType", "price",
               "auxPrice", "status", "timeInForce", "lastExecutionTime_r"}


def parse_order(data: Any, masker: Masker) -> IbkrOrder | None:
    if not isinstance(data, Mapping):
        return None
    raw = data.get("status") if isinstance(data.get("status"), str) else None
    kind = str(data.get("origOrderType") or data.get("orderType") or "").lower()
    order_id, conid = data.get("orderId"), data.get("conid")
    stamp = data.get("lastExecutionTime_r")
    return IbkrOrder(None if order_id is None else str(order_id), int(conid) if isinstance(conid, (int, str)) and str(conid).isdigit() else None,
                     str(data.get("ticker") or ""), str(data.get("side") or "").lower(), number(data.get("totalSize")), number(data.get("filledQuantity")),
                     number(data.get("remainingQuantity")), ORDER_TYPES.get(kind, kind or "market"), number(data.get("price")),
                     number(data.get("auxPrice")), STATUS.get((raw or "").lower(), "WORKING"), raw,
                     data.get("timeInForce") if isinstance(data.get("timeInForce"), str) else None, None if stamp is None else str(stamp),
                     masker.ref(data.get("acct")), masker.mask(data.get("acct")), public(_known(data, _ORDER_KEYS | {"acct"}), masker))


@dataclass(frozen=True, slots=True)
class OrderReply:
    """The answer to a placed order: an accepted order (`order_id`), or a question of the gateway that nobody confirmed (`question`, its messages)."""
    order_id: str | None
    status: str | None
    question: bool
    messages: tuple[str, ...]
    extra: dict[str, Any] = field(default_factory=dict)


def parse_reply(data: Any) -> OrderReply:
    items = data if isinstance(data, list) else [data]
    first = next((i for i in items if isinstance(i, Mapping)), {})
    if "order_id" in first:
        return OrderReply(str(first["order_id"]), str(first.get("order_status") or "") or None, False, (), {})
    messages = tuple(str(m)[:200] for m in first.get("message", []) if isinstance(m, str)) if isinstance(first.get("message"), list) else ()
    return OrderReply(None, None, "id" in first or bool(messages), messages, {})
