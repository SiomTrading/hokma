"""The simulated broker of Hokma (SPEC-018: AH-39, AH-40; decision 1215): a real account of Hokma with a real market, a commission and a
slippage, comparable with a real broker, and a switch to a real account that changes only the account assigned to the desk.

It takes orders in the neutral shape that `hokma.exec.submit.to_native` builds (every amount as text), fills them with the engine of
`sim_engine.py` and writes each fill in `sim_fills`, in `book_entries` (the one book of every broker) and in the answer. Costs live in the broker
config and are editable. The cash is the book's: the last `open` or `reset` plus the entries after it."""
from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from hokma.backoffice import book as book_module
from hokma.backoffice import valuation
from hokma.broker import sim_engine
from hokma.broker.contract import BaseBroker, NotSupported
from hokma.broker.costs import DEFAULT_COMMISSION, DEFAULT_SLIPPAGE_BPS, MAX_BPS, _number, validate_commission
from hokma.broker.sim_engine import Refused
from hokma.broker.types import Balance, Capabilities, Fill, Order, Position
from hokma.db.timeutil import utcnow
from hokma.exec.money import ZERO, MoneyError, dec, dec_or_none, text

KIND = "sim"
ORDER_KEYS = {"fingerprint", "type", "tif", "limit_price", "stop_price", "stop_loss", "take_profit", "reduce_only", "legs"}
LEG_KEYS = {"contract_id", "symbol", "side", "effect", "qty", "multiplier", "tick_size", "asset_type"}


class _NotEnoughCash(Exception):
    """Raised inside the fill transaction to roll it back."""


class SimBroker(BaseBroker):
    capabilities = Capabilities(accounts=True, reset=True, orders=True, positions=True)

    @property
    def commission(self) -> dict[str, Any]:
        return self.row.config.get("commission", DEFAULT_COMMISSION)

    @property
    def slippage_bps(self) -> float:
        return self.row.config.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)

    @property
    def max_quote_age(self) -> float:
        return float(self.row.config.get("max_quote_age_seconds", sim_engine.DEFAULT_MAX_QUOTE_AGE))

    def set_costs(self, commission: Mapping[str, Any] | None = None, slippage_bps: float | None = None) -> None:
        """Change the costs; what is not given stays. Validated before it is saved."""
        config = {**self.row.config, "commission": validate_commission(commission) if commission is not None else self.commission,
                  "slippage_bps": _number(slippage_bps, "slippage", MAX_BPS) if slippage_bps is not None else self.slippage_bps}
        self.row = self.repo.set_broker_config(self.id, config)

    # ---- accounts and money ----

    def reset(self, id: int):
        self.account(id)
        if self._positions_raw(id) or self.repo.s.one("SELECT 1 AS x FROM sim_orders WHERE account_id = :a AND status = 'working'", {"a": id}) is not None:
            raise NotSupported("close the positions and cancel the open orders before a reset")      # an old position never survives a reset
        account = super().reset(id)
        self.repo.s.insert("book_entries", {"account_id": id, "kind": "adjustment", "amount": "0", "currency": "USD", "source": "owner",
                                            "source_ref": f"reset:{utcnow()}:{secrets.token_hex(4)}", "occurred_at": utcnow(), "recorded_at": utcnow(),
                                            "note": "reset"})
        return account

    def _cash(self, account_id: int) -> Decimal | None:
        self.account(account_id)
        cash = self._book_cash(account_id)
        return cash

    def _book_cash(self, account_id: int) -> Decimal | None:
        return book_module.cash_in(self.repo.s, account_id)

    def balance(self, id: int) -> Balance:
        account = self.account(id)
        cash = self._cash(id)
        equity: float | None = None
        if cash is not None:
            equity = float(cash) if not self._positions_raw(id) else self._equity(id, cash)
        return Balance(cash=None if cash is None else float(cash), equity=equity, currency=account.currency, as_of=utcnow())

    def _equity(self, id: int, cash: Decimal) -> float | None:
        values, errors = valuation.value_all(self._book(id).cost_positions().values(), sim_engine.mark_source(list(self._book(id).cost_positions())))
        return None if errors else float(cash + valuation.total(values))

    def _book(self, account_id: int) -> book_module.Book:
        return book_module.Book.load(account_id, session=self.repo.s)

    def _positions_raw(self, account_id: int):
        return self._book(account_id).cost_positions()

    # ---- reads ----

    def positions(self, account_id: int) -> list[Position]:
        self.account(account_id)
        marks = sim_engine.mark_source(list(self._positions_raw(account_id)))
        out = []
        for cid, p in self._positions_raw(account_id).items():
            mark = marks.get(cid)
            mv = None if mark is None else float(p.qty * mark * p.multiplier)
            open_pl = None if mark is None else float((mark - p.avg_price) * p.qty * p.multiplier)
            out.append(Position(account_id, cid, float(p.qty), float(p.avg_price), asset_type=None, long_qty=float(max(p.qty, 0)),
                                short_qty=float(max(-p.qty, 0)), market_value=mv, open_pl=open_pl, multiplier=float(p.multiplier), contract_id=cid))
        return out

    def _sim_rows(self, u, account_id: int, **where) -> list[dict]:
        return u.session.query("SELECT * FROM sim_orders WHERE account_id = :a ORDER BY id", {"a": account_id})

    def _order(self, row: dict) -> Order:
        request = json.loads(row["request_json"])
        first = request["legs"][0]
        working = row["status"] == "working"
        return Order(row["account_id"], first["contract_id"], first["side"], float(first["qty"]), type=request["type"],
                     limit_price=None if not request.get("limit_price") else float(request["limit_price"]), order_id=row["order_ref"],
                     status={"working": "WORKING", "filled": "FILLED", "partially_filled": "PARTIALLY_FILLED", "canceled": "CANCELED",
                             "rejected": "REJECTED", "expired": "EXPIRED"}[row["status"]], duration=request["tif"],
                     stop_price=None if not request.get("stop_price") else float(request["stop_price"]),
                     filled_qty=float(row["filled_qty"]), entered_at=row["created_at"], cancelable=working, editable=working,
                     contract_id=first["contract_id"], multiplier=dec_or_none(first.get("multiplier")), data={"reason": row.get("reason")},
                     legs=tuple({"contract_id": g["contract_id"], "symbol": g["contract_id"], "side": g["side"], "effect": g.get("effect"), "quantity": g["qty"]}
                                for g in request["legs"]))

    def orders(self, account_id: int, *args: Any, **filters: Any) -> list[Order]:
        from hokma.db import get_database

        self.account(account_id)
        with get_database().read() as u:
            return [self._order(r) for r in self._sim_rows(u, account_id)]

    def get_order(self, account_id: int, order_id: Any) -> Order | None:
        from hokma.db import get_database

        self.account(account_id)
        with get_database().read() as u:
            row = u.session.one("SELECT * FROM sim_orders WHERE account_id = :a AND order_ref = :r", {"a": account_id, "r": str(order_id)})
        return None if row is None else self._order(row)

    # ---- orders ----

    def _parse(self, order: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(order, Mapping) or set(order) - ORDER_KEYS or not order.get("legs"):
            raise Refused("bad_order", "the order is not in the neutral shape")
        for leg in order["legs"]:
            if set(leg) - LEG_KEYS or not {"contract_id", "side", "qty", "multiplier"} <= set(leg):
                raise Refused("bad_order", "a leg is not in the neutral shape")
        return dict(order)

    def preview_order(self, account_id: int, order: Mapping[str, Any]) -> dict[str, Any]:
        """What the order would cost with the quote of this moment. Nothing is saved."""
        self.account(account_id)
        parsed = self._parse(order)
        now, value, missing = time.time(), ZERO, []
        for leg in parsed["legs"]:
            quote = sim_engine.quote_source().quote(leg["contract_id"])
            if not sim_engine.fresh(quote, now, self.max_quote_age):
                missing.append(leg["contract_id"])
                continue
            value += dec(leg["qty"]) * quote.side_price(leg["side"]) * dec(leg["multiplier"])
        return {"orderStrategy": {"orderBalance": {"orderValue": float(value) if not missing else 0.0}}, "unpriced": missing}

    def place_order(self, account_id: int, order: Mapping[str, Any]) -> dict[str, Any]:
        from hokma.db import get_database

        self.account(account_id)
        parsed = self._parse(order)
        ref = "S" + secrets.token_hex(7)
        database = get_database()
        with database.write() as u:
            u.session.insert("sim_orders", {"account_id": account_id, "order_ref": ref, "request_json": json.dumps(parsed, sort_keys=True),
                                            "status": "working", "filled_qty": "0", "created_at": utcnow(), "updated_at": utcnow()})
        try:
            return self._try_fill(account_id, ref, parsed, initial=True)
        except Refused as refused:
            self._close(ref, "rejected", refused.why[:200])
            return {"order_id": ref, "status": "REJECTED", "reason": refused.code, "filled_qty": "0"}

    def _fresh_cash(self, account_id: int) -> Decimal | None:
        from hokma.db import get_database

        return book_module.cash(account_id, db=get_database())

    def _reduce_legs(self, account_id: int, legs: list[dict]) -> list[dict]:
        """A reduce-only order is cut to the position the book really holds; with nothing to reduce it is refused."""
        from hokma.db import get_database
        from hokma.exec.money import clip_reduce_only

        held = book_module.Book.load(account_id, db=get_database()).cost_positions()
        out = []
        for leg in legs:
            pos = held.get(leg["contract_id"])
            qty, _clipped = clip_reduce_only(leg["side"], dec(leg["qty"]), pos.qty if pos else ZERO)
            if qty <= 0:
                raise Refused("reduce_only_nothing", f"there is no {leg['contract_id']} position to reduce")
            out.append({**leg, "qty": text(qty)})
        return out

    def _try_fill(self, account_id: int, ref: str, parsed: dict[str, Any], *, initial: bool) -> dict[str, Any]:
        """Fill the order when the quotes allow it; otherwise leave it working. A market order with no fresh quote is refused."""
        otype = parsed["type"]
        limit, stop = dec_or_none(parsed.get("limit_price")), dec_or_none(parsed.get("stop_price"))
        now = time.time()
        fills: list[sim_engine.LegFill] = []
        legs = parsed["legs"]
        if parsed.get("reduce_only"):                       # against the REAL position at the moment of the fill, not the flag
            legs = self._reduce_legs(account_id, legs)
        for leg in legs:
            quote = sim_engine.quote_source().quote(leg["contract_id"])
            if not sim_engine.fresh(quote, now, self.max_quote_age):
                if otype == "market":
                    raise Refused("no_market_price", f"{leg['contract_id']} has no fresh quote")
                return {"order_id": ref, "status": "WORKING", "filled_qty": "0"}
            if not sim_engine.crosses(leg["side"], otype, limit, stop, quote):
                return {"order_id": ref, "status": "WORKING", "filled_qty": "0"}
            fills.append(sim_engine.fill_leg(leg, order_type=otype, limit=limit, stop=stop, quote=quote, slippage_bps=self.slippage_bps,
                                             commission_cfg=self.commission))
        assets = {leg["contract_id"]: leg.get("asset_type") or "equity" for leg in parsed["legs"]}
        cash = self._fresh_cash(account_id)
        need = sim_engine.cash_needed(fills, assets)
        if cash is not None and need is not None and need > cash:       # the cash check holds for every order, reduce_only included
            raise Refused("insufficient_cash", "the account does not have the cash for this order")
        return self._book_fills(account_id, ref, parsed, fills, assets)

    def _book_fills(self, account_id: int, ref: str, parsed: dict[str, Any], fills: list[sim_engine.LegFill],
                    assets: Mapping[str, str] | None = None) -> dict[str, Any]:
        """The claim of the order, its fills and its book entries are ONE transaction: an order that is no longer working (canceled by another
        thread) is not filled, and nothing of it is written."""
        from hokma.db import get_database

        total = sum((f.qty for f in fills), ZERO)
        avg = sum((f.qty * f.price for f in fills), ZERO) / total
        assets = assets or {leg["contract_id"]: leg.get("asset_type") or "equity" for leg in parsed["legs"]}
        try:
            return self._book_in_one_transaction(account_id, ref, parsed, fills, assets, total, avg)
        except _NotEnoughCash:
            self._close(ref, "rejected", "the account does not have the cash for this order")
            return {"order_id": ref, "status": "REJECTED", "filled_qty": "0", "reason": "insufficient_cash"}

    def _book_in_one_transaction(self, account_id, ref, parsed, fills, assets, total, avg) -> dict[str, Any]:
        """Claim, cash check, fills and book entries in ONE write transaction (the write lock serializes the fills of an account): two fills that
        together pass the cash cannot both go through; the second sees the cash after the first and the transaction rolls back."""
        from hokma.db import get_database

        with get_database().write() as u:
            row = u.session.one("SELECT id FROM sim_orders WHERE order_ref = :r", {"r": ref})
            claimed = u.session.execute("UPDATE sim_orders SET status = 'filled', filled_qty = :q, avg_price = :p, updated_at = :t "
                                        "WHERE id = :i AND status = 'working'", {"q": text(total), "p": text(avg), "t": utcnow(), "i": row["id"]})
            if claimed != 1:
                return {"order_id": ref, "status": "CANCELED", "filled_qty": "0", "reason": "not_working"}
            need, cash = sim_engine.cash_needed(fills, assets), book_module.cash_in(u.session, account_id)
            if cash is not None and need is not None and need > cash:
                raise _NotEnoughCash()                              # rolls the claim back with everything else
            desk_id = u.broker.desk_of_account(account_id)
            for n, f in enumerate(fills):
                u.session.insert("sim_fills", {"sim_order_id": row["id"], "contract_id": f.contract_id, "side": f.side, "qty": text(f.qty), "price": text(f.price),
                                               "commission": text(f.commission), "multiplier": text(f.multiplier),
                                               "quote_json": json.dumps({"bid": text(f.quote.bid), "ask": text(f.quote.ask), "last": text(f.quote.last), "ts": f.quote.ts}),
                                               "filled_at": utcnow()})
                book_module.record_fill_in(u.session, Fill(account_id, f.contract_id, f.side, float(f.qty), float(f.price), float(f.commission), utcnow(),
                                                           order_id=ref, multiplier=f.multiplier, contract_id=f.contract_id),
                                           desk_id=desk_id, source="sim", source_ref=f"{ref}:{n}")
        return {"order_id": ref, "status": "FILLED", "filled_qty": text(total), "avg_price": text(avg)}

    def _close(self, ref: str, status: str, reason: str | None = None) -> None:
        from hokma.db import get_database

        with get_database().write() as u:
            u.session.update("sim_orders", {"order_ref": ref}, {"status": status, "reason": reason, "updated_at": utcnow()})

    def cancel_order(self, account_id: int, order_id: Any) -> dict[str, Any]:
        from hokma.db import get_database

        self.account(account_id)
        with get_database().write() as u:
            n = u.session.execute("UPDATE sim_orders SET status = 'canceled', updated_at = :t WHERE account_id = :a AND order_ref = :r AND status = 'working'",
                                  {"t": utcnow(), "a": account_id, "r": str(order_id)})
        if n != 1:
            raise NotSupported("the order is not working")
        return {"order_id": str(order_id), "status": "CANCELED"}

    def replace_order(self, account_id: int, order_id: Any, order: Mapping[str, Any]) -> dict[str, Any]:
        """Replacing is placing: the new order is checked first with the same checks (shape, reduce-only against the real position), and only
        if it passes is the old one canceled, so a refused replacement leaves the working order where it was."""
        parsed = self._parse(order)
        if parsed.get("reduce_only"):
            try:
                self._reduce_legs(account_id, parsed["legs"])
            except Refused as refused:
                return {"order_id": str(order_id), "status": "REJECTED", "reason": refused.code, "filled_qty": "0"}
        self.cancel_order(account_id, order_id)
        return self.place_order(account_id, order)

    # ---- the stream: fill what waits ----

    def process(self, contract_id: str) -> int:
        """Called when the quote of a contract changes: fill the working orders of this broker that the new quote crosses. Returns how many filled."""
        from hokma.db import get_database

        filled = 0
        if self.row.config.get("orders_enabled") is not True:        # the off switch also holds for orders that are already working
            return 0
        with get_database().read() as u:
            rows = u.session.query(
                "SELECT s.* FROM sim_orders s JOIN broker_accounts a ON a.id = s.account_id "
                "LEFT JOIN desk_accounts da ON da.account_id = a.id LEFT JOIN desks d ON d.id = da.desk_id "
                "WHERE s.status = 'working' AND a.broker_id = :b AND a.status = 'active' AND (d.id IS NULL OR d.status = 'active')", {"b": self.id})
        for row in rows:
            parsed = json.loads(row["request_json"])
            if contract_id not in {leg["contract_id"] for leg in parsed["legs"]}:
                continue
            try:
                result = self._try_fill(row["account_id"], row["order_ref"], parsed, initial=False)
            except Refused as refused:
                self._close(row["order_ref"], "rejected", refused.why[:200])
                continue
            filled += 1 if result["status"] == "FILLED" else 0
        return filled


def on_quote(contract_id: str, quote: Any) -> int:
    """Listener of the quote board: fill the waiting orders of every simulated broker that this quote crosses."""
    from hokma.db import get_database

    filled = 0
    with get_database().read() as u:
        for row in u.broker.list_brokers():
            if row.kind == KIND:
                filled += SimBroker(u.broker, row).process(contract_id)
    return filled


def attach() -> None:
    """Subscribe the simulated brokers to the quote board (once)."""
    listeners = sim_engine.board().listeners
    if on_quote not in listeners:
        listeners.append(on_quote)


__all__ = ["KIND", "MoneyError", "Refused", "SimBroker", "attach", "on_quote"]
