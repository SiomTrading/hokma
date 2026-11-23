"""The crypto connector (SPEC-018 B5: OA-55): spot holdings and orders of one account at a crypto exchange, for the line that holds crypto.

This is the ONLY file of Hokma that imports the crypto exchange library. An order reaches the exchange only through `place_order`,
`replace_order` and `cancel_order` of this class, which `hokma.exec.submit.dispatch` alone calls. The connector never turns the orders switch on
and does not decide whether an order may go out.

The exchange is one of `EXCHANGES` (an allow list: an agent never names one). Its keys live in the credential store (entry `crypto.exchange`,
JSON `{"exchange", "key", "secret", "password"?}`). The exchange object comes from an injectable factory (`set_exchange_factory`), so a test
answers recorded replies. The account is the key: its identifier is the exchange id and the key, used only for the fingerprint; the mask is
the four dots and the exchange id. A spot account has no cost basis and no leverage: a position carries no average price (0) and no open
result, and an order with a bracket, or a day order that is not a market order, is refused with its reason instead of being changed.
Quantities and prices are Decimal in Hokma; they become a float only at the library call.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import ccxt

from hokma.broker import external
from hokma.broker.contract import NotSupported
from hokma.broker.external import ConnectorError, ExternalBroker, Neutral
from hokma.broker.types import Balance, Capabilities, Order, Position
from hokma.contracts import symbols
from hokma.db.timeutil import utcnow
from hokma.exec.money import ZERO, MoneyError, dec, dec_or_none

log = logging.getLogger("hokma.broker")

KIND = "crypto"
KEY_ENTRY = "crypto.exchange"
FINGERPRINT_ENTRY = "crypto.fingerprint_key"
EXCHANGES = ("kraken", "coinbase", "binanceus", "bitstamp", "gemini")
QUOTE = "USD"
_STATUS = {"open": "WORKING", "closed": "FILLED", "canceled": "CANCELED", "cancelled": "CANCELED", "expired": "EXPIRED", "rejected": "REJECTED"}

ExchangeFactory = Callable[[Any], Any]
_factory: ExchangeFactory | None = None


def set_exchange_factory(factory: ExchangeFactory | None) -> None:
    """Tests only: where the exchange object comes from (`factory(broker)`). None puts the default back."""
    global _factory
    _factory = factory


def _default_exchange(broker: "CryptoBroker") -> Any:
    from hokma.connections import runtime

    saved = runtime.get_service().store.get(KEY_ENTRY)
    try:
        keys = json.loads(saved) if saved else None
        name = keys["exchange"]
        if name not in EXCHANGES:
            raise KeyError(name)
        config = {"apiKey": keys["key"], "secret": keys["secret"], "enableRateLimit": True}
        if keys.get("password"):
            config["password"] = keys["password"]
        return getattr(ccxt, name)(config)
    except (ValueError, KeyError, TypeError):
        raise ConnectorError(external.NO_KEYS, "the crypto exchange keys are not saved") from None


def _number(value: Any) -> Decimal | None:
    try:
        return dec_or_none(value)
    except MoneyError:
        return None


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _guard(call: Callable[[], Any]) -> Any:
    """One library call. A network failure is `unavailable`; an answer of the exchange that says no is passed up as ccxt.BaseError."""
    try:
        return call()
    except ccxt.NetworkError:
        log.info("the crypto exchange is not reachable")
        raise ConnectorError(external.UNAVAILABLE, "the exchange did not answer") from None


def _reason(error: ccxt.BaseError) -> str:
    """The class of the refusal as a word of ours: never the text of the exchange."""
    return type(error).__name__.lower()[:48]


class CryptoBroker(ExternalBroker):
    capabilities = Capabilities(accounts=True, reset=False, orders=True, positions=True)
    kind_name = "Crypto"
    key_entry = FINGERPRINT_ENTRY

    _exchange: Any = None

    # ---- the exchange and who the account is ----

    def exchange(self) -> Any:
        if self._exchange is None:
            self._exchange = (_factory or _default_exchange)(self)
        return self._exchange

    def _identifier(self) -> str:
        ex = self.exchange()
        if getattr(ex, "id", None) not in EXCHANGES or not getattr(ex, "apiKey", None):
            raise ConnectorError(external.NO_KEYS, "the crypto exchange keys are not saved")
        return f"{ex.id}:{ex.apiKey}"

    def live_identifiers(self) -> list[str]:
        return [self._identifier()]

    def mask_for(self, identifier: str) -> str:
        return external.MASK_DOTS + identifier.split(":", 1)[0][:9]

    def scrubber(self, identifiers: list[str] | None = None) -> external.Scrubber:
        found = self.live_identifiers() if identifiers is None else identifiers
        items = {i: self.mask_for(i) for i in found}
        items.update({i.split(":", 1)[1]: self.mask_for(i) for i in found if ":" in i})        # the key alone, too
        return external.Scrubber(items)

    def _who(self, id: int) -> tuple[str, str | None, external.Scrubber]:
        identifiers = self.live_identifiers()
        position, _identifier = self._match(id, identifiers)
        return f"A{position + 1}", self.mask_of(id), self.scrubber(identifiers)

    # ---- reads ----

    def _holdings(self) -> dict[str, Decimal]:
        """The total of every currency the account holds, above zero."""
        raw = _guard(self.exchange().fetch_balance)
        total = raw.get("total") if isinstance(raw, dict) else None
        return {str(c): q for c, v in (total or {}).items() if (q := _number(v)) is not None and q > ZERO}

    def _last(self, currency: str) -> Decimal | None:
        try:
            tick = _guard(lambda: self.exchange().fetch_ticker(f"{currency}/{QUOTE}"))
        except ccxt.BaseError:
            return None
        return _number(tick.get("last")) if isinstance(tick, dict) else None

    def balance(self, id: int) -> Balance:
        """Cash is the USD the account holds; equity adds the other currencies at their last price. None (never a made-up 0) when it cannot be read
        or a currency has no price."""
        account = self.account(id)
        try:
            self._who(id)
            raw = _guard(self.exchange().fetch_balance)
        except (ConnectorError, ccxt.BaseError):
            log.info("crypto balance not available")
            return Balance(cash=None, equity=None, currency=account.currency, as_of=utcnow())
        free = raw.get("free") if isinstance(raw, dict) and isinstance(raw.get("free"), dict) else {}
        total = {str(c): q for c, v in ((raw.get("total") if isinstance(raw, dict) else None) or {}).items() if (q := _number(v)) is not None and q > ZERO}
        cash = _number(free.get(QUOTE)) or ZERO
        equity: Decimal | None = total.get(QUOTE, ZERO)
        for currency, qty in total.items():
            if currency == QUOTE:
                continue
            price = self._last(currency)
            equity = None if price is None or equity is None else equity + qty * price
        return Balance(cash=float(cash), equity=_float(equity), currency=account.currency, as_of=utcnow())

    def positions(self, account_id: int) -> list[Position]:
        ref, masked, scrub = self._who(account_id)
        out = []
        for currency, qty in sorted(self._holdings().items()):
            if currency == QUOTE:
                continue
            pair = f"{currency}/{QUOTE}"
            try:
                contract_id = symbols.from_provider("crypto", pair)
            except Exception:                              # noqa: BLE001
                contract_id = None
            price = self._last(currency)
            out.append(Position(account_id, pair, float(qty), 0.0, asset_type="CRYPTO", long_qty=float(qty), short_qty=0.0,
                                market_value=None if price is None else float(qty * price), multiplier=1.0, account_ref=ref, mask=masked,
                                contract_id=contract_id, data=scrub.public({"symbol": pair, "qty": str(qty), "avg_price_known": False})))
        return out

    def _order(self, account_id: int, ref: str, masked: str | None, scrub: external.Scrubber, o: dict) -> Order:
        raw_status = str(o.get("status") or "open").lower()
        filled, amount, remaining = _number(o.get("filled")), _number(o.get("amount")), _number(o.get("remaining"))
        word = _STATUS.get(raw_status, "WORKING")
        if word == "WORKING" and filled:
            word = "PARTIALLY_FILLED"
        symbol = str(o.get("symbol") or "")
        try:
            contract_id = symbols.from_provider("crypto", symbol)
        except Exception:                                  # noqa: BLE001
            contract_id = None
        kind = str(o.get("type") or "market")
        working = word in ("WORKING", "PARTIALLY_FILLED")
        return Order(account_id, symbol, str(o.get("side") or ""), float(amount or ZERO), type=kind, limit_price=_float(_number(o.get("price"))),
                     order_id=None if o.get("id") is None else str(o["id"]), status=word, duration=None if not o.get("timeInForce") else str(o["timeInForce"]),
                     stop_price=_float(_number(o.get("stopPrice") or o.get("triggerPrice"))), filled_qty=_float(filled), remaining_qty=_float(remaining),
                     entered_at=None if o.get("datetime") is None else str(o["datetime"]), cancelable=working, editable=working, account_ref=ref,
                     mask=masked, contract_id=contract_id,
                     data=scrub.public({"id": o.get("id"), "symbol": symbol, "side": o.get("side"), "type": kind, "status": raw_status,
                                        "amount": str(o.get("amount")), "filled": str(o.get("filled")), "price": None if o.get("price") is None else str(o["price"])}))

    def _listed(self, account_id: int, which: str = "all") -> list[Order]:
        ref, masked, scrub = self._who(account_id)
        ex = self.exchange()
        rows: list[dict] = []
        try:
            if which in ("all", "open"):
                rows += _guard(ex.fetch_open_orders)
            if which in ("all", "closed"):
                try:
                    rows += _guard(ex.fetch_closed_orders)
                except (ccxt.NotSupported, ccxt.ArgumentsRequired):
                    pass                                   # an exchange that does not list closed orders without a symbol
        except ccxt.BaseError as error:
            raise ConnectorError("crypto_" + _reason(error), "the exchange refused to list the orders") from None
        return [self._order(account_id, ref, masked, scrub, o) for o in rows if isinstance(o, dict)]

    def orders(self, account_id: int, status: str | None = None, **_ignored: Any) -> list[Order]:
        """The orders of the account. `status` is open, closed or all (default open)."""
        if status not in (None, "open", "closed", "all"):
            raise ConnectorError(external.BAD_ORDER, "status must be open, closed or all")
        return self._listed(account_id, status or "open")

    def get_order(self, account_id: int, order_id: Any) -> Order | None:
        if not isinstance(order_id, str) or not 1 <= len(order_id) <= 64:
            raise ConnectorError(external.BAD_ORDER, "an order id is a short text")
        return next((o for o in self._listed(account_id) if o.order_id == order_id), None)

    # ---- orders: only through these four, called only by hokma.exec.submit.dispatch ----

    def _plan(self, account_id: int, order: Neutral) -> tuple[str, str, Decimal, dict[str, Any], str, Decimal | None]:
        """(symbol, side, amount, params, ccxt type, price) of a one leg spot order. Refuses what the exchange or the account cannot do."""
        leg = external.single_leg(order, "the crypto connector")
        if leg.asset_type != "crypto":
            raise ConnectorError(external.NO_INSTRUMENT, "the crypto connector trades crypto pairs")
        if order.stop_loss is not None or order.take_profit is not None:
            raise ConnectorError(external.UNSUPPORTED, "a spot account takes no bracket")
        if order.type != "market" and order.tif != "gtc":
            raise ConnectorError(external.UNSUPPORTED, "crypto limit and stop orders are good till canceled (tif gtc)")
        ex = self.exchange()
        _guard(ex.load_markets)
        try:
            market = ex.market(leg.symbol)
        except ccxt.BadSymbol:
            raise ConnectorError(external.NO_INSTRUMENT, "the exchange does not list this pair") from None
        qty = leg.qty
        if order.reduce_only:
            held = {p.contract_id: Decimal(repr(p.qty)) for p in self.positions(account_id) if p.contract_id}
            qty = external.reduce_legs(order.legs, lambda cid: held.get(cid, ZERO))[0].qty
        minimum = _number(((market.get("limits") or {}).get("amount") or {}).get("min"))
        if minimum is not None and qty < minimum:
            raise ConnectorError("below_minimum", "the amount is below the minimum of the pair")
        if _number(ex.amount_to_precision(leg.symbol, float(qty))) != qty:
            raise ConnectorError("amount_precision", "the amount has more decimals than the pair allows")
        price = order.limit_price if order.type in ("limit", "stop_limit") else None
        if price is not None and _number(ex.price_to_precision(leg.symbol, float(price))) != price:
            raise ConnectorError("price_precision", "the price has more decimals than the pair allows")
        params: dict[str, Any] = {}
        if order.type in ("stop", "stop_limit"):
            params["triggerPrice"] = float(order.stop_price)
        if order.type != "market":
            params["timeInForce"] = "GTC"
        if order.fingerprint:
            params["clientOrderId"] = ("hk" + order.fingerprint)[:32]
        return leg.symbol, leg.side, qty, params, "limit" if order.type in ("limit", "stop_limit") else "market", price

    def _result(self, o: Any) -> dict[str, Any]:
        o = o if isinstance(o, dict) else {}
        word = _STATUS.get(str(o.get("status") or "open").lower(), "WORKING")
        filled = _number(o.get("filled"))
        if word == "WORKING" and filled:
            word = "PARTIALLY_FILLED"
        return external.answer(None if o.get("id") is None else str(o["id"]), word, filled=filled, avg=_number(o.get("average")))

    def place_order(self, account_id: int, order: dict) -> dict[str, Any]:
        self._who(account_id)
        try:
            symbol, side, qty, params, kind, price = self._plan(account_id, external.parse_order(order, "crypto"))
        except ConnectorError as error:
            if error.code in (external.UNAVAILABLE, external.NOT_LISTED, external.NO_KEYS):
                raise
            return external.refused(error.code)
        except ccxt.BaseError as error:
            return external.refused(_reason(error))
        try:
            return self._result(_guard(lambda: self.exchange().create_order(symbol, kind, side, float(qty), None if price is None else float(price), params)))
        except ccxt.BaseError as error:
            return external.refused(_reason(error))

    def _find(self, account_id: int, order_id: Any) -> Order:
        found = self.get_order(account_id, order_id)
        if found is None or not found.cancelable:
            raise NotSupported("the order is not working")
        return found

    def cancel_order(self, account_id: int, order_id: Any) -> dict[str, Any]:
        found = self._find(account_id, order_id)
        try:
            _guard(lambda: self.exchange().cancel_order(found.order_id, found.symbol))
        except ccxt.OrderNotFound:
            raise NotSupported("the order is not working") from None
        except ccxt.BaseError as error:
            raise ConnectorError("crypto_" + _reason(error), "the exchange refused to cancel the order") from None
        return {"order_id": found.order_id, "status": "CANCELED"}

    def replace_order(self, account_id: int, order_id: Any, order: dict) -> dict[str, Any]:
        found = self._find(account_id, order_id)
        try:                                                       # the same plan and the same refusals as place_order
            symbol, side, qty, params, kind, price = self._plan(account_id, external.parse_order(order, "crypto"))
        except ConnectorError as error:
            if error.code in (external.UNAVAILABLE, external.NOT_LISTED, external.NO_KEYS):
                raise
            return external.refused(error.code)
        ex = self.exchange()
        try:
            if ex.has.get("editOrder") is True:
                return self._result(_guard(lambda: ex.edit_order(found.order_id, symbol, kind, side, float(qty),
                                                                 None if price is None else float(price), params)))
            _guard(lambda: ex.cancel_order(found.order_id, found.symbol))
            return self._result(_guard(lambda: ex.create_order(symbol, kind, side, float(qty), None if price is None else float(price), params)))
        except ccxt.BaseError as error:
            return external.refused(_reason(error))

    def preview_order(self, account_id: int, order: dict) -> dict[str, Any]:
        """What the order is worth at the price it carries, or at the last price of the pair for a market order. Nothing is sent."""
        self._who(account_id)
        parsed = external.parse_order(order, "crypto")
        value, missing = ZERO, []
        for leg in parsed.legs:
            price = parsed.limit_price or parsed.stop_price
            if price is None:
                price = self._last(leg.symbol.split("/")[0])
            if price is None:
                missing.append(leg.contract_id)
                continue
            value += leg.qty * price * leg.multiplier
        return {"orderStrategy": {"orderBalance": {"orderValue": float(value) if not missing else 0.0}}, "unpriced": missing}


def sync_accounts(db=None) -> list[int]:
    """The keys are saved: make sure the broker row and the local account of this exchange key exist (see `external.sync_accounts`)."""
    from hokma.db import get_database
    from types import SimpleNamespace

    ex = (_factory or _default_exchange)(SimpleNamespace(row=SimpleNamespace(config={})))
    if getattr(ex, "id", None) not in EXCHANGES or not getattr(ex, "apiKey", None):
        raise ConnectorError(external.NO_KEYS, "the crypto exchange keys are not saved")
    listing = [{"identifier": f"{ex.id}:{ex.apiKey}", "nick": None, "equity": None, "mask": external.MASK_DOTS + ex.id[:9]}]
    return external.sync_accounts(KIND, listing, key_entry=FINGERPRINT_ENTRY, db=db or get_database())
