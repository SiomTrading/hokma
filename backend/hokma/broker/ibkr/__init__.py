"""The IBKR connector (SPEC-018 B5: OA-55, OA-72): shares, ETFs, options and futures of an IBKR account, through the official web API and the
gateway that runs on the student's computer (`http.py`; typed answers in `models.py`). No package is added.

The connector reports `unavailable` while the gateway does not answer and `not_signed_in` while it answers without a login session; in both cases no
order is sent (`ConnectorError`) and the balance is None, never a made-up 0. An order reaches IBKR only through `place_order`,
`replace_order` and `cancel_order` of this class, which `hokma.exec.submit.dispatch` alone calls. The question that the gateway sometimes
asks before it takes an order is NOT answered here: an order that needs a confirmation is refused with the reason `needs_confirmation`.
The connector never turns the orders switch on and does not decide whether an order may go out.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from hokma.broker import external
from hokma.broker.contract import NotSupported
from hokma.broker.external import ConnectorError, ExternalBroker, Neutral
from hokma.broker.ibkr import http, models
from hokma.broker.ibkr.http import Gateway, IbkrError
from hokma.broker.types import Balance, Capabilities, Order, Position
from hokma.contracts import symbols
from hokma.contracts.identity import parse_canonical
from hokma.db.timeutil import utcnow
from hokma.exec.money import ZERO

log = logging.getLogger("hokma.broker")

KIND = "ibkr"
FINGERPRINT_ENTRY = "ibkr.fingerprint_key"
_SEC_TYPES = {"equity": "STK", "etf": "STK", "option": "OPT", "future": "FUT"}
_ORDER_TYPES = {"market": "MKT", "limit": "LMT", "stop": "STP", "stop_limit": "STP LMT"}
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_ORDER_ID_DIGITS = 19

_transport: http.Transport | None = None


def set_transport(transport: http.Transport | None) -> None:
    """Tests only: the function that answers the calls of the gateway (`transport(method, url, headers, body) -> (status, text)`)."""
    global _transport
    _transport = transport


def _json_number(value: Decimal) -> int | float:
    """A quantity or price in a JSON body: a whole number as an int, anything else as the float of its exact text (the edge of the API)."""
    return int(value) if value == value.to_integral_value() else float(value)


def _order_id(value: Any) -> str:
    text = str(value)
    if not text.isdigit() or not 1 <= len(text) <= _ORDER_ID_DIGITS:
        raise ConnectorError(external.BAD_ORDER, "an IBKR order id is a number")
    return text


class IbkrBroker(ExternalBroker):
    capabilities = Capabilities(accounts=True, reset=False, orders=True, positions=True)
    kind_name = "IBKR"
    key_entry = FINGERPRINT_ENTRY

    _gateway: Gateway | None = None
    _conids: dict[str, tuple[str, Decimal | None]] | None = None

    # ---- the gateway and who the account is ----

    def gateway(self) -> Gateway:
        if self._gateway is None:
            try:
                self._gateway = Gateway(self.row.config.get("gateway_base") or http.DEFAULT_BASE, transport=_transport)
            except IbkrError:
                raise ConnectorError(external.UNAVAILABLE, "the gateway address must be a local one") from None
        return self._gateway

    def status(self) -> dict[str, str]:
        """`ok`, `unavailable` (the gateway does not answer) or `not_signed_in` (it answers, nobody is logged in). Nothing is sent."""
        try:
            auth = models.parse_auth(self.gateway().get("/iserver/auth/status"))
        except IbkrError as error:
            return {"status": "not_signed_in" if error.code == http.NOT_SIGNED_IN else "unavailable"}
        return {"status": "ok" if auth.ready else "not_signed_in"}

    def _ready(self) -> None:
        state = self.status()["status"]
        if state != "ok":
            raise ConnectorError(external.UNAVAILABLE if state == "unavailable" else external.NOT_SIGNED_IN,
                                 "the IBKR gateway does not answer" if state == "unavailable" else "nobody is signed in to the IBKR gateway")

    def _call(self, method: str, path: str, *, query: dict | None = None, body: Any = None) -> Any:
        try:
            return self.gateway().request(method, path, query, body=body)
        except IbkrError as error:
            if error.code == http.UNAVAILABLE:
                raise ConnectorError(external.UNAVAILABLE, "the IBKR gateway does not answer") from None
            if error.code == http.NOT_SIGNED_IN:
                raise ConnectorError(external.NOT_SIGNED_IN, "nobody is signed in to the IBKR gateway") from None
            raise ConnectorError("ibkr_" + error.code.split(".")[-1], "the IBKR gateway refused the call") from None

    def live_identifiers(self) -> list[str]:
        return models.parse_account_ids(self._call("GET", "/portfolio/accounts"))

    def _who(self, id: int) -> tuple[str, str, models.Masker, str]:
        """(account_ref, mask, masker, identifier) of a local account, after proving it is one the gateway lists now."""
        identifiers = self.live_identifiers()
        position, identifier = self._match(id, identifiers)
        masker = models.Masker(identifiers)
        return f"A{position + 1}", self.mask_of(id) or models.mask_of(identifier), masker, identifier

    # ---- reads ----

    def balance(self, id: int) -> Balance:
        account = self.account(id)
        try:
            _ref, _mask, masker, identifier = self._who(id)
            found = models.parse_summary(self._call("GET", f"/portfolio/{identifier}/summary"), identifier, masker)
        except ConnectorError:
            log.info("ibkr balance not available")
            return Balance(cash=None, equity=None, currency=account.currency, as_of=utcnow())
        return Balance(cash=None if found.cash is None else float(found.cash), equity=None if found.equity is None else float(found.equity),
                       currency=account.currency, as_of=utcnow(), buying_power=None if found.buying_power is None else float(found.buying_power))

    def positions(self, account_id: int) -> list[Position]:
        ref, masked, masker, identifier = self._who(account_id)
        raw = self._call("GET", f"/portfolio/{identifier}/positions/0")
        out = []
        for item in raw if isinstance(raw, list) else []:
            p = models.parse_position(item, masker)
            if p is None or p.qty == 0:
                continue
            out.append(self._position(account_id, ref, masked, p))
        return out

    @staticmethod
    def _position(account_id: int, ref: str, masked: str, p: models.IbkrPosition) -> Position:
        option = p.asset_class == "OPT"
        try:
            contract_id = symbols.from_provider("ibkr", p.contract_symbol if option and p.contract_symbol else p.symbol)
        except Exception:                                    # noqa: BLE001  (a symbol Hokma has no id for)
            contract_id = None
        multiplier = p.multiplier if p.multiplier else (Decimal(100) if option else Decimal(1))
        return Position(account_id, p.symbol, float(p.qty), float(p.avg_price or ZERO), asset_type=p.asset_class,
                        long_qty=float(max(p.qty, ZERO)), short_qty=float(max(-p.qty, ZERO)),
                        market_value=None if p.market_value is None else float(p.market_value),
                        open_pl=None if p.unrealized_pnl is None else float(p.unrealized_pnl), multiplier=float(multiplier), put_call=p.put_call,
                        account_ref=ref, mask=masked, contract_id=contract_id,
                        data={"conid": p.conid, "symbol": p.symbol, "asset_class": p.asset_class, "qty": str(p.qty), **p.extra})

    @staticmethod
    def _order(account_id: int, ref: str, masked: str, o: models.IbkrOrder) -> Order:
        working = o.status == "WORKING"
        status = "PARTIALLY_FILLED" if o.partially_filled else o.status
        return Order(account_id, o.symbol, o.side, float(o.qty or ZERO), type=o.order_type, limit_price=None if o.price is None else float(o.price),
                     order_id=o.order_id, status=status, duration=o.tif, stop_price=None if o.stop_price is None else float(o.stop_price),
                     filled_qty=None if o.filled is None else float(o.filled), remaining_qty=None if o.remaining is None else float(o.remaining),
                     entered_at=o.entered_at, cancelable=working, editable=working, account_ref=ref, mask=masked,
                     data={"order_id": o.order_id, "conid": o.conid, "symbol": o.symbol, "side": o.side, "status": o.raw_status, **o.extra})

    def _listed(self, account_id: int) -> list[Order]:
        ref, masked, masker, identifier = self._who(account_id)
        raw = self._call("GET", "/iserver/account/orders")
        rows = raw.get("orders") if isinstance(raw, dict) else None
        out = []
        for item in rows if isinstance(rows, list) else []:
            o = models.parse_order(item, masker)
            if o is not None and isinstance(item, dict) and str(item.get("acct")) == identifier:
                out.append(self._order(account_id, ref, masked, o))
        return out

    def orders(self, account_id: int, status: str | None = None, **_ignored: Any) -> list[Order]:
        """The orders the gateway lists for the account (the open ones and the ones of the day). `status` is a filter on the words WORKING,
        FILLED, CANCELED, REJECTED, EXPIRED."""
        found = self._listed(account_id)
        if status is None:
            return found
        if status not in ("WORKING", "FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            raise ConnectorError(external.BAD_ORDER, "status is not valid")
        return [o for o in found if o.status == status or (status == "WORKING" and o.status == "PARTIALLY_FILLED")]

    def get_order(self, account_id: int, order_id: Any) -> Order | None:
        key = _order_id(order_id)
        return next((o for o in self._listed(account_id) if o.order_id == key), None)

    # ---- the contract of a leg ----

    def _conid(self, leg: external.Leg) -> tuple[str, Decimal | None]:
        """(conid, multiplier) of the instrument of a leg, asked of the gateway. Raises ConnectorError(NO_INSTRUMENT) when it finds none or many."""
        if self._conids is None:
            self._conids = {}
        if leg.contract_id in self._conids:
            return self._conids[leg.contract_id]
        parsed = parse_canonical(leg.contract_id)
        found: tuple[str, Decimal | None] | None = None
        if leg.asset_type in ("equity", "etf"):
            stock = self._stock(leg.symbol)
            found = None if stock is None else (stock, None)
        elif leg.asset_type == "option":
            found = self._option(parsed)
        elif leg.asset_type == "future":
            found = self._future(parsed)
        if found is None:
            raise ConnectorError(external.NO_INSTRUMENT, f"IBKR has no contract for {leg.contract_id}")
        self._conids[leg.contract_id] = found
        return found

    def _stock(self, symbol: str) -> str | None:
        rows = self._call("POST", "/iserver/secdef/search", body={"symbol": symbol, "secType": "STK"})
        matches = [r for r in rows if isinstance(r, dict) and str(r.get("symbol", "")).upper() == symbol.upper() and str(r.get("conid", "")).isdigit()] \
            if isinstance(rows, list) else []
        return str(matches[0]["conid"]) if len(matches) == 1 else None

    def _option(self, parsed) -> tuple[str, Decimal | None] | None:
        underlying = self._stock((parsed.underlying or "").replace(".", " "))
        if underlying is None or parsed.expiration is None:
            return None
        month = f"{_MONTHS[parsed.expiration.month - 1]}{parsed.expiration.year % 100:02d}"
        rows = self._call("GET", "/iserver/secdef/info", query={"conid": underlying, "sectype": "OPT", "month": month,
                                                                "strike": format(parsed.strike.normalize(), "f"), "right": parsed.put_call})
        day = f"{parsed.expiration:%Y%m%d}"
        matches = [r for r in rows if isinstance(r, dict) and str(r.get("maturityDate")) == day and str(r.get("right")) == parsed.put_call
                   and models.number(r.get("strike")) == parsed.strike and str(r.get("conid", "")).isdigit()] if isinstance(rows, list) else []
        return (str(matches[0]["conid"]), models.number(matches[0].get("multiplier"))) if len(matches) == 1 else None

    def _future(self, parsed) -> tuple[str, Decimal | None] | None:
        rows = self._call("GET", "/trsrv/futures", query={"symbols": parsed.root})
        month = f"{parsed.expiration:%Y%m}" if parsed.expiration else ""
        listed = rows.get(parsed.root) if isinstance(rows, dict) else None
        matches = sorted((r for r in listed or [] if isinstance(r, dict) and str(r.get("expirationDate", "")).startswith(month)
                          and str(r.get("conid", "")).isdigit()), key=lambda r: str(r.get("expirationDate")))
        return (str(matches[0]["conid"]), None) if matches else None

    # ---- orders: only through these four, called only by hokma.exec.submit.dispatch ----

    def _body(self, account_id: int, identifier: str, order: Neutral, *, with_id: bool) -> dict[str, Any]:
        """The request of a one leg order. Refuses, with its reason, what is not supported (it never cuts an order)."""
        leg = external.single_leg(order, "IBKR")
        if order.stop_loss is not None or order.take_profit is not None:
            raise ConnectorError(external.UNSUPPORTED, "IBKR brackets are not supported yet")
        if leg.asset_type not in _SEC_TYPES:
            raise ConnectorError(external.NO_INSTRUMENT, "IBKR trades shares, ETFs, options and futures")
        qty = leg.qty
        if order.reduce_only:
            held = {p.contract_id: Decimal(repr(p.qty)) for p in self.positions(account_id) if p.contract_id}
            qty = external.reduce_legs(order.legs, lambda cid: held.get(cid, ZERO))[0].qty
        conid, _multiplier = self._conid(leg)
        body: dict[str, Any] = {"acctId": identifier, "conid": int(conid), "secType": f"{conid}:{_SEC_TYPES[leg.asset_type]}",
                                "orderType": _ORDER_TYPES[order.type], "side": leg.side.upper(), "quantity": _json_number(qty),
                                "tif": "DAY" if order.tif == "day" else "GTC", "outsideRTH": False}
        if order.type == "limit":
            body["price"] = _json_number(order.limit_price)
        elif order.type == "stop":
            body["price"] = _json_number(order.stop_price)
        elif order.type == "stop_limit":
            body["price"] = _json_number(order.limit_price)
            body["auxPrice"] = _json_number(order.stop_price)
        if with_id and order.fingerprint:
            body["cOID"] = ("hk-" + order.fingerprint)[:40]          # a second order with the same id is refused by the gateway
        return body

    def _taken(self, reply: Any) -> dict[str, Any]:
        parsed = models.parse_reply(reply)
        if parsed.order_id is None:
            return external.refused("needs_confirmation" if parsed.question else "ibkr_no_order")
        word = models.STATUS.get((parsed.status or "").lower(), "WORKING")
        return external.answer(parsed.order_id, word)

    def place_order(self, account_id: int, order: dict) -> dict[str, Any]:
        self._ready()
        try:
            neutral = external.parse_order(order, "ibkr")
            _ref, _masked, _masker, identifier = self._who(account_id)
            body = self._body(account_id, identifier, neutral, with_id=True)
        except ConnectorError as error:
            if error.code in (external.UNAVAILABLE, external.NOT_SIGNED_IN, external.NOT_LISTED):
                raise
            return external.refused(error.code)
        try:
            return self._taken(self._call("POST", f"/iserver/account/{identifier}/orders", body={"orders": [body]}))
        except ConnectorError as error:
            if error.code in (external.UNAVAILABLE, external.NOT_SIGNED_IN):
                raise
            return external.refused(error.code)

    def replace_order(self, account_id: int, order_id: Any, order: dict) -> dict[str, Any]:
        self._ready()
        key = _order_id(order_id)
        try:                                                       # the same build and the same refusals as place_order
            neutral = external.parse_order(order, "ibkr")
            _ref, _masked, _masker, identifier = self._who(account_id)
            body = self._body(account_id, identifier, neutral, with_id=False)
        except ConnectorError as error:
            if error.code in (external.UNAVAILABLE, external.NOT_SIGNED_IN, external.NOT_LISTED):
                raise
            return external.refused(error.code)
        try:
            return self._taken(self._call("POST", f"/iserver/account/{identifier}/order/{key}", body=body))
        except ConnectorError as error:
            if error.code in (external.UNAVAILABLE, external.NOT_SIGNED_IN):
                raise
            return external.refused(error.code)

    def cancel_order(self, account_id: int, order_id: Any) -> dict[str, Any]:
        self._ready()
        key = _order_id(order_id)
        _ref, _masked, _masker, identifier = self._who(account_id)
        try:
            reply = self._call("DELETE", f"/iserver/account/{identifier}/order/{key}")
        except ConnectorError as error:
            if error.code in (external.UNAVAILABLE, external.NOT_SIGNED_IN):
                raise
            raise NotSupported("the order is not working") from None
        if isinstance(reply, dict) and reply.get("error"):
            raise NotSupported("the order is not working")
        return {"order_id": key, "status": "CANCELED"}

    def preview_order(self, account_id: int, order: dict) -> dict[str, Any]:
        """What the gateway says the order would cost (`whatif`). Nothing is sent as an order."""
        self._ready()
        neutral = external.parse_order(order, "ibkr")
        _ref, _masked, _masker, identifier = self._who(account_id)
        body = self._body(account_id, identifier, neutral, with_id=False)
        reply = self._call("POST", f"/iserver/account/{identifier}/orders/whatif", body={"orders": [body]})
        amount = reply.get("amount") if isinstance(reply, dict) and isinstance(reply.get("amount"), dict) else {}
        value, commission = models.number(amount.get("amount")), models.number(amount.get("commission"))
        return {"orderStrategy": {"orderBalance": {"orderValue": None if value is None else float(value),
                                                   "projectedCommission": None if commission is None else float(commission)}},
                "unpriced": [] if value is not None else [leg.contract_id for leg in neutral.legs]}


def sync_accounts(db=None) -> list[int]:
    """The gateway is signed in: make sure the broker row and one local account per IBKR account exist (see `external.sync_accounts`)."""
    from hokma.db import get_database

    try:
        identifiers = models.parse_account_ids(Gateway(http.DEFAULT_BASE, transport=_transport).get("/portfolio/accounts"))
    except IbkrError:
        raise ConnectorError(external.UNAVAILABLE, "the IBKR gateway does not answer or nobody is signed in") from None
    listing = [{"identifier": i, "nick": None, "equity": None} for i in identifiers]
    return external.sync_accounts(KIND, listing, key_entry=FINGERPRINT_ENTRY, db=db or get_database())
