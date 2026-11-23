"""The Alpaca connector (SPEC-018 B5: DTS-08b, OA-55): stocks, ETFs and options of one Alpaca account, read and traded through the official SDK.

This is the ONLY file of Hokma that imports the Alpaca SDK. An order reaches Alpaca only through `place_order`, `replace_order` and
`cancel_order` of this class, and those are called only by `hokma.exec.submit.dispatch`. The connector never turns the orders switch of its broker
on and never decides whether an order may go out: the rules of `hokma.broker.rules` and the levels of `hokma.exec` do.

The client comes from an injectable factory (`set_client_factory`): the default builds a `TradingClient` with the keys of the credential store
(entry `alpaca.trading`, JSON `{"key_id", "secret"}`) and the PAPER endpoint unless the broker config says `"paper": false`; a test gives a client
whose HTTP session answers recorded bodies. Quantities and prices are Decimal everywhere in Hokma; they become a float only at the SDK call,
because its request models take floats.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetOrdersRequest, LimitOrderRequest, MarketOrderRequest, ReplaceOrderRequest, StopLimitOrderRequest,
                                     StopLossRequest, StopOrderRequest, TakeProfitRequest)

from hokma.broker import external
from hokma.broker.contract import NotSupported
from hokma.broker.external import ConnectorError, ExternalBroker, Neutral
from hokma.broker.types import Balance, Capabilities, Order, Position
from hokma.contracts import symbols
from hokma.db.timeutil import utcnow
from hokma.exec.money import ZERO, MoneyError, dec, dec_or_none

log = logging.getLogger("hokma.broker")

KIND = "alpaca"
KEY_ENTRY = "alpaca.trading"                # the order keys; the data keys of the market connection are another entry
FINGERPRINT_ENTRY = "alpaca.fingerprint_key"
MAX_ORDERS = 500
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_STATUS = {"new": "WORKING", "accepted": "WORKING", "pending_new": "WORKING", "accepted_for_bidding": "WORKING", "held": "WORKING",
           "pending_review": "WORKING", "pending_replace": "WORKING", "pending_cancel": "WORKING", "calculated": "WORKING", "stopped": "WORKING",
           "replaced": "CANCELED", "partially_filled": "PARTIALLY_FILLED", "filled": "FILLED", "canceled": "CANCELED", "done_for_day": "EXPIRED",
           "expired": "EXPIRED", "rejected": "REJECTED", "suspended": "REJECTED"}
_INTENT = {("buy", "open"): PositionIntent.BUY_TO_OPEN, ("buy", "close"): PositionIntent.BUY_TO_CLOSE,
           ("sell", "open"): PositionIntent.SELL_TO_OPEN, ("sell", "close"): PositionIntent.SELL_TO_CLOSE}

ClientFactory = Callable[[Any], TradingClient]
_factory: ClientFactory | None = None


def set_client_factory(factory: ClientFactory | None) -> None:
    """Tests only: where the client comes from (`factory(broker)` returns a TradingClient). None puts the default back."""
    global _factory
    _factory = factory


def _default_client(broker: "AlpacaBroker") -> TradingClient:
    from hokma.connections import runtime

    saved = runtime.get_service().store.get(KEY_ENTRY)
    try:
        keys = json.loads(saved) if saved else None
        client = TradingClient(api_key=keys["key_id"], secret_key=keys["secret"], paper=broker.row.config.get("paper") is not False)
    except (ValueError, KeyError, TypeError):
        raise ConnectorError(external.NO_KEYS, "the Alpaca order keys are not saved") from None
    return client


def _number(value: Any) -> Decimal | None:
    try:
        return dec_or_none(value)
    except MoneyError:
        return None


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _api_code(error: APIError) -> str:
    """The Alpaca error code as a word of ours: never the text of the answer."""
    try:
        return f"alpaca_{int(error.code)}"
    except Exception:                                  # noqa: BLE001  (a body that is not JSON)
        return f"alpaca_http_{error.status_code or 0}"


def _guard(call: Callable[[], Any]) -> Any:
    """Run one SDK call. A network failure is `unavailable` and nothing else is said about it; an error of Alpaca is passed up as APIError."""
    try:
        return call()
    except OSError:
        log.info("alpaca is not reachable")
        raise ConnectorError(external.UNAVAILABLE, "Alpaca did not answer") from None


class AlpacaBroker(ExternalBroker):
    capabilities = Capabilities(accounts=True, reset=False, orders=True, positions=True)
    kind_name = "Alpaca"
    key_entry = FINGERPRINT_ENTRY

    _client_cache: TradingClient | None = None

    # ---- the client and who the account is ----

    def client(self) -> TradingClient:
        if self._client_cache is None:
            self._client_cache = (_factory or _default_client)(self)
        return self._client_cache

    def _account_raw(self):
        return _guard(self.client().get_account)

    def live_identifiers(self) -> list[str]:
        number = getattr(self._account_raw(), "account_number", None)
        return [str(number)] if number else []

    def _who(self, id: int) -> tuple[str, str | None, external.Scrubber]:
        """(account_ref, mask, scrubber) of a local account, after proving it is the account these keys belong to (one call to Alpaca)."""
        identifiers = self.live_identifiers()
        position, _identifier = self._match(id, identifiers)
        return f"A{position + 1}", self.mask_of(id), self.scrubber(identifiers)

    def _check(self, id: int) -> None:
        self._who(id)

    # ---- reads ----

    def balance(self, id: int) -> Balance:
        """Cash, equity and buying power of the real account; None (never a made-up 0) while Alpaca cannot be read."""
        account = self.account(id)
        try:
            self._check(id)
            raw = self._account_raw()
        except (ConnectorError, APIError):
            log.info("alpaca balance not available")
            return Balance(cash=None, equity=None, currency=account.currency, as_of=utcnow())
        cash, equity, power = _number(raw.cash), _number(raw.equity), _number(raw.buying_power)
        return Balance(cash=_float(cash), equity=_float(equity), currency=account.currency, as_of=utcnow(), buying_power=_float(power))

    def positions(self, account_id: int) -> list[Position]:
        ref, masked, scrub = self._who(account_id)
        try:
            found = _guard(self.client().get_all_positions)
        except APIError as error:
            raise ConnectorError(_api_code(error), "Alpaca refused to list the positions") from None
        return [self._position(account_id, ref, masked, scrub, p) for p in found]

    @staticmethod
    def _position(account_id: int, ref: str, masked: str | None, scrub: external.Scrubber, raw: Any) -> Position:
        qty = _number(raw.qty) or ZERO
        short = getattr(raw.side, "value", raw.side) == "short"
        signed = -abs(qty) if short else abs(qty)
        asset_class = getattr(raw.asset_class, "value", raw.asset_class)
        try:
            contract_id = symbols.from_provider("alpaca", str(raw.symbol))
        except Exception:                              # noqa: BLE001  (a symbol Hokma has no id for)
            contract_id = None
        is_option = asset_class == "us_option"
        return Position(account_id, str(raw.symbol), float(signed), float(_number(raw.avg_entry_price) or ZERO),
                        asset_type="OPTION" if is_option else str(asset_class or "").upper() or None,
                        long_qty=float(max(signed, ZERO)), short_qty=float(max(-signed, ZERO)), market_value=_float(_number(raw.market_value)),
                        open_pl=_float(_number(raw.unrealized_pl)), multiplier=100.0 if is_option else 1.0, account_ref=ref, mask=masked,
                        data=scrub.public({"symbol": raw.symbol, "asset_class": asset_class, "qty": str(raw.qty), "side": str(getattr(raw.side, "value", raw.side)),
                                           "avg_entry_price": str(raw.avg_entry_price), "market_value": str(raw.market_value),
                                           "current_price": str(raw.current_price)}), contract_id=contract_id)

    def _order(self, account_id: int, ref: str, masked: str | None, scrub: external.Scrubber, raw: Any) -> Order:
        status = getattr(raw.status, "value", raw.status)
        qty, filled = _number(raw.qty), _number(raw.filled_qty)
        side = str(getattr(raw.side, "value", raw.side))
        working = _STATUS.get(str(status), "WORKING") in ("WORKING", "PARTIALLY_FILLED")
        try:
            contract_id = symbols.from_provider("alpaca", str(raw.symbol))
        except Exception:                              # noqa: BLE001
            contract_id = None
        otype = str(getattr(raw.type, "value", raw.type) or "market")
        return Order(account_id, str(raw.symbol), side, float(qty or ZERO), type=otype, limit_price=_float(_number(raw.limit_price)), order_id=str(raw.id),
                     status=_STATUS.get(str(status), "WORKING"), duration=str(getattr(raw.time_in_force, "value", raw.time_in_force)),
                     stop_price=_float(_number(raw.stop_price)), filled_qty=_float(filled),
                     remaining_qty=None if qty is None else float(qty - (filled or ZERO)), entered_at=str(raw.submitted_at or raw.created_at or ""),
                     cancelable=working, editable=working, account_ref=ref, mask=masked, contract_id=contract_id,
                     data=scrub.public({"id": str(raw.id), "client_order_id": str(raw.client_order_id), "symbol": raw.symbol, "side": side,
                                        "status": str(status), "type": otype, "qty": str(raw.qty), "filled_qty": str(raw.filled_qty),
                                        "filled_avg_price": None if raw.filled_avg_price is None else str(raw.filled_avg_price),
                                        "limit_price": None if raw.limit_price is None else str(raw.limit_price),
                                        "stop_price": None if raw.stop_price is None else str(raw.stop_price)}))

    def orders(self, account_id: int, status: str | None = None, limit: int | None = None, **_ignored: Any) -> list[Order]:
        """The orders of the account. `status` is open, closed or all (default open); `limit` is at most 500."""
        wanted = {None: QueryOrderStatus.OPEN, "open": QueryOrderStatus.OPEN, "closed": QueryOrderStatus.CLOSED, "all": QueryOrderStatus.ALL}
        if status not in wanted or (limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ORDERS)):
            raise ConnectorError(external.BAD_ORDER, "status must be open, closed or all and limit between 1 and 500")
        ref, masked, scrub = self._who(account_id)
        try:
            found = _guard(lambda: self.client().get_orders(GetOrdersRequest(status=wanted[status], limit=limit or 100)))
        except APIError as error:
            raise ConnectorError(_api_code(error), "Alpaca refused to list the orders") from None
        return [self._order(account_id, ref, masked, scrub, o) for o in found]

    def get_order(self, account_id: int, order_id: Any) -> Order | None:
        key = self._order_key(order_id)
        ref, masked, scrub = self._who(account_id)
        try:
            found = _guard(lambda: self.client().get_order_by_id(key))
        except APIError as error:
            if error.status_code == 404:
                return None
            raise ConnectorError(_api_code(error), "Alpaca refused to read the order") from None
        return self._order(account_id, ref, masked, scrub, found)

    @staticmethod
    def _order_key(order_id: Any) -> str:
        if not isinstance(order_id, str) or not _UUID.fullmatch(order_id):
            raise ConnectorError(external.BAD_ORDER, "an Alpaca order id is a UUID")
        return order_id

    # ---- orders: only through these four, called only by hokma.exec.submit.dispatch ----

    def _request(self, account_id: int, order: Neutral):
        """The SDK request of a one leg order. Refuses, with its reason, what Alpaca cannot do (it never cuts an order)."""
        leg = external.single_leg(order, "Alpaca")
        if leg.asset_type not in ("equity", "etf", "option"):
            raise ConnectorError(external.NO_INSTRUMENT, "Alpaca trades shares, ETFs and options")
        is_option = leg.asset_type == "option"
        if is_option and (order.tif != "day" or leg.qty != leg.qty.to_integral_value()):
            raise ConnectorError(external.UNSUPPORTED, "Alpaca options are day orders of whole contracts")
        if is_option and (order.stop_loss is not None or order.take_profit is not None or order.type in ("stop", "stop_limit")):
            raise ConnectorError(external.UNSUPPORTED, "Alpaca options take market and limit orders without a bracket")
        qty = leg.qty
        if order.reduce_only:
            held = {p.contract_id: Decimal(repr(p.qty)) for p in self.positions(account_id) if p.contract_id}
            qty = external.reduce_legs(order.legs, lambda cid: held.get(cid, ZERO))[0].qty
        common: dict[str, Any] = {"symbol": leg.symbol, "qty": float(qty), "side": OrderSide(leg.side),
                                  "time_in_force": TimeInForce.DAY if order.tif == "day" else TimeInForce.GTC}
        if order.fingerprint:
            common["client_order_id"] = ("hk-" + order.fingerprint)[:48]        # Alpaca refuses a second order with the same id: a repeat is not sent twice
        if is_option:
            common["position_intent"] = _INTENT[(leg.side, leg.effect)]
        exits = [order.stop_loss, order.take_profit]
        if any(price is not None for price in exits):
            if order.type not in ("market", "limit"):
                raise ConnectorError(external.UNSUPPORTED, "a bracket needs a market or limit entry")
            common["order_class"] = OrderClass.BRACKET if all(price is not None for price in exits) else OrderClass.OTO
            if order.take_profit is not None:
                common["take_profit"] = TakeProfitRequest(limit_price=float(order.take_profit))
            if order.stop_loss is not None:
                common["stop_loss"] = StopLossRequest(stop_price=float(order.stop_loss))
        if order.type == "market":
            return MarketOrderRequest(**common)
        if order.type == "limit":
            return LimitOrderRequest(limit_price=float(order.limit_price), **common)
        if order.type == "stop":
            return StopOrderRequest(stop_price=float(order.stop_price), **common)
        return StopLimitOrderRequest(limit_price=float(order.limit_price), stop_price=float(order.stop_price), **common)

    def _result(self, raw: Any) -> dict[str, Any]:
        status = _STATUS.get(str(getattr(raw.status, "value", raw.status)), "WORKING")
        return external.answer(str(raw.id), status, filled=_number(raw.filled_qty), avg=_number(raw.filled_avg_price))

    def place_order(self, account_id: int, order: dict) -> dict[str, Any]:
        self._check(account_id)
        try:
            request = self._request(account_id, external.parse_order(order, "alpaca"))
        except ConnectorError as error:
            if error.code in (external.UNAVAILABLE, external.NOT_LISTED, external.NO_KEYS):
                raise
            return external.refused(error.code)
        try:
            return self._result(_guard(lambda: self.client().submit_order(request)))
        except APIError as error:
            return external.refused(_api_code(error))

    def replace_order(self, account_id: int, order_id: Any, order: dict) -> dict[str, Any]:
        """Replacing an order is placing one: the new order is built by `_request`, the same function as `place_order` (the option restrictions,
        the cut of a reduce-only order to the live position, one leg), and the change carries the quantity and prices of THAT request, never the
        ones of the order as it arrived."""
        self._check(account_id)
        key = self._order_key(order_id)
        try:
            request = self._request(account_id, external.parse_order(order, "alpaca"))
        except ConnectorError as error:
            if error.code in (external.UNAVAILABLE, external.NOT_LISTED, external.NO_KEYS):
                raise
            return external.refused(error.code)
        qty = Decimal(repr(request.qty))
        change = ReplaceOrderRequest(qty=int(qty) if qty == qty.to_integral_value() else float(qty), time_in_force=request.time_in_force,
                                     limit_price=getattr(request, "limit_price", None), stop_price=getattr(request, "stop_price", None))
        try:
            return self._result(_guard(lambda: self.client().replace_order_by_id(key, change)))
        except APIError as error:
            return external.refused(_api_code(error))

    def cancel_order(self, account_id: int, order_id: Any) -> dict[str, Any]:
        self._check(account_id)
        key = self._order_key(order_id)
        try:
            _guard(lambda: self.client().cancel_order_by_id(key))
        except APIError as error:
            if error.status_code in (404, 422):
                raise NotSupported("the order is not working") from None
            raise ConnectorError(_api_code(error), "Alpaca refused to cancel the order") from None
        return {"order_id": key, "status": "CANCELED"}

    def preview_order(self, account_id: int, order: dict) -> dict[str, Any]:
        """What the order is worth at the prices it carries. Alpaca has no preview call: an order with no price of its own (a market order)
        has no value here and is listed in `unpriced`. Nothing is sent."""
        self._check(account_id)
        parsed = external.parse_order(order, "alpaca")
        value, missing = ZERO, []
        for leg in parsed.legs:
            price = parsed.limit_price or parsed.stop_price
            if price is None:
                missing.append(leg.contract_id)
                continue
            value += leg.qty * price * leg.multiplier
        return {"orderStrategy": {"orderBalance": {"orderValue": float(value) if not missing else 0.0}}, "unpriced": missing}


def sync_accounts(db=None, paper: bool = True) -> list[int]:
    """The keys are saved: make sure the broker row and the local account of this Alpaca account exist (see `external.sync_accounts`).
    `paper` says which endpoint the keys belong to; it defaults to the paper one."""
    from hokma.db import get_database

    client = (_factory or _default_client)(SimpleNamespace(row=SimpleNamespace(config={"paper": paper})))
    raw = _guard(client.get_account)
    number = getattr(raw, "account_number", None)
    listing = [{"identifier": str(number), "nick": None, "equity": _number(raw.equity)}] if number else []
    return external.sync_accounts(KIND, listing, key_entry=FINGERPRINT_ENTRY, db=db or get_database())

