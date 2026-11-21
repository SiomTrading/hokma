"""The rules of an order of a desk (SPEC-015 18.3 and 18.5): a pure function that says whether an order leaves on its own, needs
the owner or is refused. It reads no database and calls no broker: the service gathers the facts (the switch of the broker, the
state and the capital of the desk, the preview of Schwab, the positions, the open orders, the optional limits) and this module
only decides. The same facts give the same answer.

Result of `evaluate`: {"result": "auto" | "needs_owner" | "refused", "why": text in the language of the student,
"code": a stable short id, "flags": [ids of every soft condition found], "numbers": {...}}.

- refused: the switch of the broker is off, the desk is paused or archived, the desk has no account of a broker that trades,
  or Schwab says no (`rejects` or a negative `projectedBuyingPower`). Closing orders and cancellations are refused for these too.
- needs_owner: Schwab asks for a review (`reviews`, `warns`, `alerts`), the order would take what the desk has committed past
  `desks.capital`, an optional limit of 18.5 is on and exceeded, or the order opens a position whose value cannot be worked
  out (a leg without price: `unpriced`, SPEC-015 bug 1223).
- auto: nothing above. A closing order (SELL of a long, BUY of a short, BUY_TO_COVER, `*_TO_CLOSE`, positionEffect CLOSING) and a
  cancellation go out on their own even when the desk is over its capital, so risk can always be reduced.

Design decision (reported): the soft conditions give `needs_owner` in BOTH permission modes. In the "Ask" mode the owner has
already approved the call in the chat, but this extra confirmation is never less safe, and it shows the owner the reason that
Schwab or the desk raised. A closing order is also exempt from every optional limit of 18.5 (a limit must never stop a position
from being reduced); only what Schwab itself raises (reviews, warns, alerts) still asks for the owner.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from hokma.i18n import t
from hokma.schwab.accounts import OPEN_ORDER_STATUSES

ACTIONS = ("preview", "place", "replace", "cancel")
LIMIT_KEYS = ("max_order_pct", "max_daily_loss_pct", "assets_only", "options_allowed", "short_allowed",
              "extended_hours_allowed", "max_orders_per_day")
MAX_ORDER_PCT = 1000.0
MAX_ORDERS_PER_DAY = 10000
CLOSE_INSTRUCTIONS = ("BUY_TO_COVER", "BUY_TO_CLOSE", "SELL_TO_CLOSE")
OPEN_INSTRUCTIONS = ("BUY_TO_OPEN", "SELL_TO_OPEN", "SELL_SHORT", "SELL_SHORT_EXEMPT")
SHORT_INSTRUCTIONS = ("SELL_SHORT", "SELL_SHORT_EXEMPT", "SELL_TO_OPEN")      # a sold option to open is a short leg
MESSAGE_LIMIT = 300                                                           # text that comes from Schwab, cut


# ---------------------------------------------------------------------------------------------------------- limits

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def validate_limits(changes: Any, current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The limits of a desk after `changes` (18.5). Only the given keys change; a `None` removes a limit (off). Everything is
    checked before anything is saved: raises `ValueError(detail)` for an unknown key or a value out of range.
    Off is the absence of the key. For the three `*_allowed` keys the limit is ON when the value is `False`; `True` is the
    same as off."""
    if not isinstance(changes, Mapping):
        raise ValueError("limits must be an object")
    merged = dict(current or {})
    for key, value in changes.items():
        if key not in LIMIT_KEYS:
            raise ValueError(f"unknown limit: {str(key)[:40]}")
        if value is None:
            merged.pop(key, None)
        elif key == "max_order_pct":
            if not _is_number(value) or not 0 < value <= MAX_ORDER_PCT:
                raise ValueError(f"max_order_pct must be a number above 0 and up to {MAX_ORDER_PCT:g}")
            merged[key] = float(value)
        elif key == "max_daily_loss_pct":
            if not _is_number(value) or not 0 < value <= 100:
                raise ValueError("max_daily_loss_pct must be a number above 0 and up to 100")
            merged[key] = float(value)
        elif key == "max_orders_per_day":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ORDERS_PER_DAY:
                raise ValueError(f"max_orders_per_day must be a whole number from 1 to {MAX_ORDERS_PER_DAY}")
            merged[key] = value
        else:                                                  # assets_only and the three *_allowed
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
            merged[key] = value
    return merged


NUMERIC_LIMITS = ("max_order_pct", "max_daily_loss_pct", "max_orders_per_day")      # smaller is stricter; absent is off
FLAG_LIMITS = {"assets_only": True, "options_allowed": False, "short_allowed": False, "extended_hours_allowed": False}
# for a flag limit the value that turns it ON (assets_only is on when True; the three *_allowed are on when False)


def loosened_limits(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> list[str]:
    """The keys of the limits of a desk that `after` loosens against `before` (SPEC-015 bug 1220): a limit turned off, or a
    number raised, or a flag that stops being on. Adding a limit or making a number smaller is stricter and is not listed.
    Compared value against value, so splitting a change into steps or giving it through another field cannot hide a loosening."""
    old, new = before or {}, after or {}
    out = []
    for key in NUMERIC_LIMITS:
        was, now = old.get(key), new.get(key)
        if was is not None and (now is None or now > was):
            out.append(key)
    for key, on in FLAG_LIMITS.items():
        if old.get(key) is on and new.get(key) is not on:
            out.append(key)
    return out


# ------------------------------------------------------------------------------------------------------------ facts

def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _sym(symbol: Any) -> str:
    return str(symbol or "").replace(" ", "").upper()


def _nodes(order: Any) -> Iterable[Mapping]:
    """The order and every nested `childOrderStrategies`, parents first."""
    if isinstance(order, Mapping):
        yield order
        for child in order.get("childOrderStrategies") or []:
            yield from _nodes(child)


def _legs(node: Mapping) -> list[dict[str, Any]]:
    out = []
    for leg in node.get("orderLegCollection") or []:
        if not isinstance(leg, Mapping):
            continue
        instrument = leg.get("instrument") if isinstance(leg.get("instrument"), Mapping) else {}
        asset = instrument.get("assetType")
        symbol = str(instrument.get("symbol") or "")
        underlying = instrument.get("underlyingSymbol") or (symbol[:6].strip() if asset == "OPTION" else symbol)
        out.append({"symbol": symbol, "asset": asset, "underlying": str(underlying), "instruction": leg.get("instruction"),
                    "effect": leg.get("positionEffect"), "qty": _num(leg.get("quantity")),
                    "all_shares": leg.get("quantityType") == "ALL_SHARES",
                    "mult": _num(instrument.get("optionMultiplier")) or (100.0 if asset == "OPTION" else 1.0)})
    return out


def _position_map(positions: Iterable[Any]) -> dict[str, tuple[float, float]]:
    """symbol -> (long quantity, short quantity)."""
    out: dict[str, tuple[float, float]] = {}
    for p in positions:
        long_q, short_q = _num(_get(p, "long_qty")), _num(_get(p, "short_qty"))
        if long_q is None and short_q is None:
            net = _num(_get(p, "qty")) or 0.0
            long_q, short_q = max(net, 0.0), max(-net, 0.0)
        key = _sym(_get(p, "symbol"))
        old = out.get(key, (0.0, 0.0))
        out[key] = (old[0] + (long_q or 0.0), old[1] + (short_q or 0.0))
    return out


def _closes(leg: Mapping, held: Mapping[str, tuple[float, float]]) -> bool:
    """Whether one leg reduces what the account holds (True) or opens or increases exposure (False)."""
    instruction, effect = leg["instruction"], leg["effect"]
    if instruction in CLOSE_INSTRUCTIONS or effect == "CLOSING":
        return True
    if instruction in OPEN_INSTRUCTIONS or effect == "OPENING":
        return False
    long_q, short_q = held.get(_sym(leg["symbol"]), (0.0, 0.0))
    if instruction == "SELL":                                  # a sale closes a long; what is left over opens a short
        return long_q > 0 and (leg["all_shares"] or (leg["qty"] is not None and leg["qty"] <= long_q))
    if instruction == "BUY":                                   # a purchase covers a short
        return short_q > 0 and (leg["all_shares"] or (leg["qty"] is not None and leg["qty"] <= short_q))
    return False


def _opens_short(leg: Mapping, held: Mapping[str, tuple[float, float]]) -> bool:
    if leg["instruction"] in SHORT_INSTRUCTIONS:
        return True
    if leg["instruction"] == "SELL" and leg["effect"] != "CLOSING" and not _closes(leg, held):
        long_q = held.get(_sym(leg["symbol"]), (0.0, 0.0))[0]
        return leg["qty"] is None or leg["qty"] > long_q       # sells more than it holds
    return False


def order_facts(order: Any, positions: Iterable[Any] = ()) -> dict[str, Any]:
    """What a request does: every leg of every node, split in closing and opening, and its symbols, underlyings, sessions,
    options and short legs. `closing` is true only when it has legs and every one reduces the account."""
    held = _position_map(positions)
    opening: list[dict] = []
    closing: list[dict] = []
    sessions: set[str] = set()
    for node in _nodes(order):
        if node.get("session"):
            sessions.add(str(node["session"]))
        for leg in _legs(node):
            (closing if _closes(leg, held) else opening).append(leg)
    legs = opening + closing
    return {"opening": opening, "closing_legs": closing, "closing": bool(legs) and not opening,
            "symbols": sorted({leg["symbol"] for leg in legs if leg["symbol"]}),
            "underlyings": sorted({leg["underlying"].upper() for leg in legs if leg["underlying"]}),
            "has_option": any(leg["asset"] == "OPTION" for leg in legs),
            "has_short": any(_opens_short(leg, held) for leg in legs),
            "sessions": sorted(sessions)}


def derive_marks(positions: Iterable[Any], preview: Any = None) -> dict[str, float]:
    """Prices for the orders that have none: the mark of each position (market value over quantity and multiplier) and the
    mark, last or ask of each leg of the preview."""
    marks: dict[str, float] = {}
    for p in positions:
        held = abs(_num(_get(p, "qty")) or 0.0) * (_num(_get(p, "multiplier")) or 1.0)
        value = _num(_get(p, "market_value"))
        if value is not None and held:
            marks[_sym(_get(p, "symbol"))] = abs(value) / held
    for leg in (_get(_get(preview, "orderStrategy") or {}, "orderLegs") or []):
        price = next((_num(_get(leg, k)) for k in ("markPrice", "lastPrice", "askPrice") if _num(_get(leg, k)) is not None), None)
        symbol = _get(leg, "finalSymbol")
        if price is not None and isinstance(symbol, str):
            marks.setdefault(_sym(symbol), price)
    return marks


def _nodes_value(order: Any, held: Mapping[str, tuple[float, float]], marks: Mapping[str, float]) -> tuple[float, list[str]]:
    """The value of what an order OPENS (limit price, else stop, else mark, times quantity and multiplier), and the symbols
    that had no price. A net price of a multi leg order (vertical, condor...) is per spread, so it counts once."""
    total, unpriced = 0.0, []
    for node in _nodes(order):
        every = _legs(node)
        legs = [leg for leg in every if not _closes(leg, held)]
        if not legs:
            continue
        price = _num(node.get("price"))
        if price is None:
            price = _num(node.get("stopPrice"))
        if len(every) > 1 and price is not None:
            spreads = min((leg["qty"] for leg in every if leg["qty"]), default=1.0)
            total += abs(price) * spreads * (100.0 if any(leg["asset"] == "OPTION" for leg in every) else 1.0)
            continue
        for leg in legs:
            each = price if price is not None else _num(marks.get(_sym(leg["symbol"])))
            if each is None or leg["qty"] is None:
                unpriced.append(leg["symbol"])
                continue
            total += abs(each) * leg["qty"] * leg["mult"]
    return total, unpriced


# ----------------------------------------------------------------------------------------------------------- results

def _result(result: str, code: str, why: str, flags: Sequence[str] = (), **numbers: Any) -> dict[str, Any]:
    return {"result": result, "why": why, "code": code, "flags": list(flags), "numbers": numbers}


def _messages(details: Any) -> str:
    out = []
    for item in details or []:
        if isinstance(item, Mapping):
            text = item.get("message") or item.get("activityMessage") or item.get("validationRuleName")
            if text:
                out.append(str(text)[:MESSAGE_LIMIT])
    return "; ".join(out)


def _money(value: float) -> str:
    return f"{value:,.2f}"

# --------------------------------------------------------------------------------------------------------- evaluate

def gate(orders_enabled: bool, desk_status: str, account_ok: bool) -> dict[str, Any] | None:
    """The first three rows of 18.3: no account of a broker that trades, the switch of the broker off, the desk paused or
    archived. They need no preview, so the service can refuse before it calls the broker. None when none of them applies."""
    if not account_ok:
        return _result("refused", "no_trading_account", t("broker.err.no_trading_account"))
    if not orders_enabled:
        return _result("refused", "orders_off", t("broker.err.orders_off"))
    if desk_status in ("paused", "archived"):
        return _result("refused", "desk_inactive", t("broker.err.desk_inactive"))
    return None


def evaluate(*, action: str, orders_enabled: bool, desk_status: str, account_ok: bool, order: Any = None,
             preview: Mapping[str, Any] | None = None, capital: float = 0.0, positions: Sequence[Any] = (),
             open_orders: Sequence[Any] = (), limits: Mapping[str, Any] | None = None, assets: Iterable[str] = (),
             orders_today: int = 0, marks: Mapping[str, float] | None = None,
             replaces_order_id: Any = None) -> dict[str, Any]:
    """The table of 18.3 and the limits of 18.5.

    action: "preview", "place", "replace" or "cancel". orders_enabled: the switch of the broker of the account. desk_status:
    "active", "paused" or "archived". account_ok: the desk has an active account of a broker that takes orders. order: the
    request (REF B.1). preview: the `PreviewOrder` as JSON (REF B.2). capital: `desks.capital`. positions: `Position` objects
    of the account. open_orders: `Order` objects of the account (those that are not open are ignored). limits: the desk's
    optional limits (empty means none). assets: the symbols of `desk_assets`. orders_today: orders sent today by the desk.
    marks: symbol -> price for orders without a price, over the ones derived from the positions and the preview.
    replaces_order_id: that open order is not counted (it is being replaced).
    """
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    refusal = gate(orders_enabled, desk_status, account_ok)
    if refusal is not None:
        return refusal
    if action == "cancel":
        return _result("auto", "ok", "")

    validation = _get(preview, "orderValidationResult") or {}
    balance = _get(_get(preview, "orderStrategy") or {}, "orderBalance") or {}
    if _get(validation, "rejects"):
        reason = _messages(_get(validation, "rejects")) or t("broker.reason.no_detail")
        return _result("refused", "rejected", t("broker.err.rejected", reason=reason))
    power = _num(_get(balance, "projectedBuyingPower"))
    if power is not None and power < 0:
        return _result("refused", "rejected", t("broker.err.rejected", reason=t("broker.reason.buying_power", value=_money(power))))

    numbers: dict[str, Any] = {}
    held = _position_map(positions)
    facts = order_facts(order, positions)
    limits = limits or {}
    marks = {**derive_marks(positions, preview), **{_sym(k): v for k, v in (marks or {}).items()}}
    flags: list[tuple[str, str]] = []

    if any(_get(validation, key) for key in ("reviews", "warns", "alerts")):
        review = "; ".join(m for m in (_messages(_get(validation, key)) for key in ("reviews", "warns", "alerts")) if m)
        flags.append(("review", t("broker.why.review", detail=review or t("broker.reason.no_detail"))))

    if not facts["closing"]:                                   # a closing order is exempt from capital and from the limits
        order_value = abs(_num(_get(balance, "orderValue")) or 0.0)
        if order_value == 0.0:
            order_value, unpriced = _nodes_value(order, held, marks)
            numbers["unpriced"] = unpriced
        numbers["order_value"] = order_value
        flags += _soft_flags(facts, limits, assets, capital, positions, open_orders, held, marks, order_value,
                             orders_today, replaces_order_id, numbers)
        if numbers.get("unpriced"):                            # a value that cannot be worked out cannot be held to the capital
            symbols = ", ".join(sorted({_sym(s) for s in numbers["unpriced"] if s}) or ["?"])
            flags.append(("unpriced", t("broker.why.unpriced", symbols=symbols)))

    if flags:
        return _result("needs_owner", flags[0][0], "; ".join(text for _code, text in flags),
                       [code for code, _text in flags], **numbers)
    return _result("auto", "ok", "", **numbers)


def _soft_flags(facts: Mapping[str, Any], limits: Mapping[str, Any], assets: Iterable[str], capital: float,
                positions: Sequence[Any], open_orders: Sequence[Any], held: Mapping[str, tuple[float, float]],
                marks: Mapping[str, float], order_value: float, orders_today: int, replaces_order_id: Any,
                numbers: dict[str, Any]) -> list[tuple[str, str]]:
    flags: list[tuple[str, str]] = []
    in_positions = sum(abs(_num(_get(p, "market_value")) or 0.0) for p in positions)
    in_orders = 0.0
    for open_order in open_orders:
        if str(_get(open_order, "status") or "") not in OPEN_ORDER_STATUSES:
            continue
        if replaces_order_id is not None and str(_get(open_order, "order_id")) == str(replaces_order_id):
            continue
        value, _missing = _nodes_value(_get(open_order, "data") or {}, held, marks)
        in_orders += value
    committed = in_positions + in_orders + order_value
    numbers.update(committed=committed, capital=capital, in_positions=in_positions, in_open_orders=in_orders)
    if committed > capital:
        flags.append(("capital", t("broker.why.capital", committed=_money(committed), capital=_money(capital))))

    pct = limits.get("max_order_pct")
    if pct is not None and capital > 0 and order_value > capital * pct / 100.0:
        flags.append(("max_order_pct", t("broker.why.max_order_pct", value=_money(order_value), pct=f"{pct:g}")))

    loss_pct = limits.get("max_daily_loss_pct")
    if loss_pct is not None:
        day = sum(_num(_get(p, "day_pl")) or 0.0 for p in positions)
        numbers["day_pl"] = day
        if capital > 0 and day < -capital * loss_pct / 100.0:
            flags.append(("max_daily_loss_pct", t("broker.why.max_daily_loss", loss=_money(-day), pct=f"{loss_pct:g}")))

    if limits.get("assets_only") is True:
        allowed = {_sym(a) for a in assets}
        outside = [s for s in facts["underlyings"] if _sym(s) not in allowed]
        if outside:
            flags.append(("assets_only", t("broker.why.assets_only", symbol=", ".join(outside))))
    if limits.get("options_allowed") is False and facts["has_option"]:
        flags.append(("options_allowed", t("broker.why.options_off")))
    if limits.get("short_allowed") is False and facts["has_short"]:
        flags.append(("short_allowed", t("broker.why.short_off")))
    if limits.get("extended_hours_allowed") is False and any(s != "NORMAL" for s in facts["sessions"]):
        flags.append(("extended_hours_allowed", t("broker.why.extended_off")))
    per_day = limits.get("max_orders_per_day")
    if per_day is not None and orders_today >= per_day:
        flags.append(("max_orders_per_day", t("broker.why.max_orders", count=str(orders_today), limit=str(per_day))))
    return flags


def summary(order: Any) -> str:
    """One line for a journal title and a decision: 'BUY 10 AAPL LIMIT 210.50', plus '+N legs' for a multi leg order and
    '+N nested' for the orders of a TRIGGER or an OCO. An order carries no account number, so neither does this."""
    if not isinstance(order, Mapping):
        return "order"
    legs = _legs(order)
    first = legs[0] if legs else {}
    parts = [str(first.get("instruction") or order.get("orderStrategyType") or ""),
             "" if first.get("qty") is None else f"{first['qty']:g}", str(first.get("symbol") or "").strip(),
             str(order.get("orderType") or ""), "" if order.get("price") is None else str(order["price"])]
    text = " ".join(p for p in parts if p)
    if len(legs) > 1:
        text += f" +{len(legs) - 1} legs"
    kids = len(order.get("childOrderStrategies") or [])
    if kids:
        text += f" +{kids} nested"
    return text[:120]
