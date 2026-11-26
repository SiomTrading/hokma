"""Data of the Option detail panel of a desk (SPEC-028, delivery 6, piece 3339-A): the table of profit and loss by date and price of one strategy on
the real chain, the curves, the figures of the header and the events that fall before expiration.

The numbers are made by hokma.quant.options.detail (Black-Scholes before expiration, the payoff of the library at expiration, the same rate, dividend
and volatility convention as the Strategies panel). This module reads the chain (hokma.market.options: the price of the underlying comes from the stream of
the market hub, the chain is the snapshot of this request with its time), resolves each leg to a premium and a volatility, and asks the events
(next earnings report, next ex-dividend date). Nothing here writes to the database, sends an order or asks a model.

The panel calls it when the owner releases a slider and when the price of the underlying moves more than RECALC_PRICE_MOVE_PCT percent since the answer
it holds (an event of the stream, never a clock); the numbers of both rules travel in the answer under "recalc" so the screen does not keep its own.
A source that cannot answer is {"ok": False, "source", "reason"} ("Sin dato de {source}"), never a zero.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta
from typing import Any

from hokma.desks import company as company_panel
from hokma.desks.errors import DeskError
from hokma.i18n import t
from hokma.market.tz import NEW_YORK
from hokma.quant.options import detail as quant
from hokma.quant.options import payoff as pay
from hokma.quant.options.structures import CATALOG, build_legs

RECALC_PRICE_MOVE_PCT = 0.1           # the screen asks again when the live price moves more than this percent from the price of the answer it holds
RECALC_DEBOUNCE_MS = 150              # and this long after a slider is released
CHAIN_STRIKES = 50                    # strikes of the window around the price that the chain read asks for
MAX_LEGS = 8
MAX_QUANTITY = 1000.0
MAX_SHARES = MAX_QUANTITY * 100.0       # shares of a stock leg: the same ceiling as the contracts (100 shares each)
log = logging.getLogger("hokma.desks")
EARNINGS_DAYS = company_panel.CALENDAR_DAYS
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SYMBOL = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")
LIMITS = {"range_pct": {"min": quant.RANGE_MIN, "max": quant.RANGE_MAX, "default": quant.DEFAULT_RANGE},
          "iv_mult": {"min": quant.IV_MULT_MIN, "max": quant.IV_MULT_MAX, "default": 1.0},
          "rows": {"min": quant.MIN_ROWS, "max": quant.MAX_ROWS, "default": quant.DEFAULT_ROWS, "odd": True},
          "columns": {"min": 2, "max": quant.MAX_COLUMNS, "default": quant.DEFAULT_COLUMNS}, "legs": {"max": MAX_LEGS}, "quantity": {"max": MAX_QUANTITY}, "shares": {"max": MAX_SHARES}}


def _bad() -> DeskError:
    return DeskError("bad_option_detail_args", 422)


def _number(value: Any, low: float | None = None, high: float | None = None, default: float | None = None, *, positive: bool = False) -> float | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _bad()
    try:
        number = float(value)
    except ValueError:
        raise _bad() from None
    if not math.isfinite(number) or (positive and number <= 0) or (low is not None and number < low) or (high is not None and number > high):
        raise _bad()
    return number


def _whole(value: Any, low: int, high: int, default: int) -> int:
    number = _number(value, low, high, default)
    if number != int(number):
        raise _bad()
    return int(number)


def _leg(raw: Any, default_quantity: float) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _bad()
    instrument = raw.get("instrument", "option")
    position = raw.get("position")
    if instrument not in ("option", "stock") or position not in ("long", "short"):
        raise _bad()
    out: dict[str, Any] = {"instrument": instrument, "position": position, "premium": _number(raw.get("premium"), 0.0),
                           "quantity": _number(raw.get("quantity"), None, MAX_QUANTITY if instrument == "option" else MAX_SHARES, default_quantity if instrument == "option" else None, positive=True)}
    if instrument == "option":
        kind = raw.get("kind")
        if kind not in ("call", "put"):
            raise _bad()
        out.update(kind=kind, strike=_number(raw.get("strike"), None, None, None, positive=True))
        if out["strike"] is None:
            raise _bad()
    return out


def check_arguments(symbol: Any, expiration: Any, legs: Any = None, structure: Any = None, strikes: Any = None, quantity: Any = None, range_pct: Any = None,
                    iv_mult: Any = None, rows: Any = None, columns: Any = None) -> dict[str, Any]:
    """The request in its clean form, or DeskError bad_option_detail_args (422). Either the legs, or a structure of the catalog with its strikes low to high."""
    if not isinstance(symbol, str) or not SYMBOL.match(symbol.strip()):
        raise _bad()
    if not isinstance(expiration, str) or not ISO_DAY.match(expiration):
        raise _bad()
    try:
        day = date.fromisoformat(expiration)
    except ValueError:
        raise _bad() from None
    qty = _number(quantity, None, MAX_QUANTITY, 1.0, positive=True)
    if (legs is None) == (structure is None):
        raise _bad()
    clean_legs = clean_strikes = None
    if legs is not None:
        if not isinstance(legs, list) or not 1 <= len(legs) <= MAX_LEGS:
            raise _bad()
        clean_legs = [_leg(x, qty) for x in legs]
        if not any(x["instrument"] == "option" for x in clean_legs):
            raise _bad()
    else:
        if not isinstance(structure, str) or structure not in CATALOG or not isinstance(strikes, list) or len(strikes) != CATALOG[structure]["strikes"]:
            raise _bad()
        clean_strikes = [_number(x, None, None, None, positive=True) for x in strikes]
        if None in clean_strikes or any(a >= b for a, b in zip(clean_strikes, clean_strikes[1:])):
            raise _bad()
    n_rows = _whole(rows, quant.MIN_ROWS, quant.MAX_ROWS, quant.DEFAULT_ROWS)
    if n_rows % 2 == 0:
        raise _bad()                                    # the middle row is the current price: an odd number of rows
    return {"symbol": symbol.strip().upper(), "expiration": day, "legs": clean_legs, "structure": structure, "strikes": clean_strikes, "quantity": qty,
            "range_pct": _number(range_pct, quant.RANGE_MIN, quant.RANGE_MAX, quant.DEFAULT_RANGE),
            "iv_mult": _number(iv_mult, quant.IV_MULT_MIN, quant.IV_MULT_MAX, 1.0),
            "rows": n_rows, "columns": _whole(columns, 2, quant.MAX_COLUMNS, quant.DEFAULT_COLUMNS)}


# ---- seams: what the tests replace -------------------------------------------------------------------------------------------------------

def _today() -> date:
    return datetime.now(NEW_YORK).date()


def _load_chain(desk_id: int, symbol: str, day: date) -> dict[str, Any]:
    """The chain of the expiration with its time, or {"ok": False, ...} when the source cannot answer."""
    from hokma.market import options

    try:
        data = options.chain(symbol, [day.isoformat()], CHAIN_STRIKES, desk_id)
        group = (data.get("expirations") or [None])[0]
        spot = data.get("underlying_price")
        if group is None or not group.get("rows") or spot is None:
            return {"ok": False, "source": data.get("source") or t("desks.option_detail.source_chain"), "code": "empty", "reason": t("desks.option_detail.empty_chain")}
        return {"ok": True, "spot": float(spot), "rows": group["rows"], "multiplier": float(group.get("multiplier") or options.DEFAULT_MULTIPLIER),
                "years": options.years_to_expiry(day), "rate": options.PROB_ITM_RATE, "source": data.get("source"), "price_feed": data.get("price_feed"),
                "greeks": data.get("greeks"), "as_of": data.get("as_of"), "market_open": data.get("market_open"), "delayed": data.get("delayed"),
                "notes": data.get("notes") or []}
    except options.OptionsError as exc:
        return {"ok": False, "source": t("desks.option_detail.source_chain"), "code": exc.code, "reason": str(exc)}


def _mark(cell: Any) -> float | None:
    from hokma.market import options

    if not isinstance(cell, dict):
        return None
    return options.mark_price(cell.get("bid"), cell.get("ask"))


def _iv_from_mark(kind: str, spot: float, strike: float, mark: float | None, years: float, rate: float) -> float | None:
    from hokma.market import options

    return options.implied_vol_from_mark(kind, spot, strike, mark, years, rate)


def _earnings_block(desk_id: int, symbol: str, today: date) -> dict[str, Any]:
    """The next earnings report of the symbol, as the Company panel asks it (same tool, same wording of a source that cannot answer)."""
    from hokma.kit import company_tools

    try:
        return company_panel._block("earnings_calendar", lambda: company_tools.earnings_calendar(today.isoformat(), (today + timedelta(days=EARNINGS_DAYS)).isoformat(), symbol, desk_id),
                                    lambda d: {"next": next((r for r in d.get("earnings", []) if r.get("symbol") in (None, symbol) and str(r.get("date")) >= today.isoformat()), None)})
    except Exception:       # any failure of the source is "no data" in the panel, never a 500
        log.warning("option detail: earnings source failed for %s", symbol, exc_info=True)
        return {"ok": False, "source": company_panel._sources("earnings_calendar"), "reason": t("desks.option_detail.source_failed")}


def _iso(day: Any) -> str | None:
    return day.isoformat() if isinstance(day, date) else None


def _ex_dividend_block(desk_id: int, symbol: str) -> dict[str, Any]:
    from hokma.market import options

    try:
        data = options.ex_dividend(symbol, desk_id)
    except options.OptionsError as exc:
        return {"ok": False, "source": t("desks.option_detail.source_ex_dividend"), "reason": str(exc)}
    except Exception:       # any other failure of the source is "no data" in the panel, never a 500
        log.warning("option detail: ex-dividend source failed for %s", symbol, exc_info=True)
        return {"ok": False, "source": t("desks.option_detail.source_ex_dividend"), "reason": t("desks.option_detail.source_failed")}
    return {"ok": True, "source": data.get("source"), "fetched_at": data.get("as_of"), "next": _iso(data.get("next_ex_date")), "last": _iso(data.get("last_ex_date")),
            "amount": data.get("amount"), "frequency": data.get("frequency")}


# ---- legs -------------------------------------------------------------------------------------------------------------------------------

def _row_of(rows: list, strike: float) -> dict | None:
    return next((r for r in rows if isinstance(r, dict) and isinstance(r.get("strike"), (int, float)) and abs(float(r["strike"]) - strike) < 1e-6), None)


def _quote_leg(raw: dict[str, Any], chain: dict[str, Any]) -> dict[str, Any] | str:
    """One option leg with its premium (the one given, else the mid of the chain) and its volatility (the one of the chain, else the one of its mark or its
    premium). A string is the reason the leg cannot be quoted."""
    row = _row_of(chain["rows"], raw["strike"])
    cell = row.get(raw["kind"]) if row else None
    mark = _mark(cell)
    premium = raw.get("premium")
    given = premium is not None
    if premium is None:
        premium = mark
    if premium is None:
        return t("desks.option_detail.no_quote", kind=raw["kind"], strike=f"{raw['strike']:g}")
    iv, iv_source = None, None
    chain_iv = cell.get("iv") if isinstance(cell, dict) else None
    if isinstance(chain_iv, (int, float)) and math.isfinite(chain_iv) and chain_iv > 0:
        iv, iv_source = float(chain_iv), "chain"
    else:
        iv = _iv_from_mark(raw["kind"], chain["spot"], raw["strike"], mark if mark is not None else premium, chain["years"], chain["rate"])
        iv_source = None if iv is None else ("mark" if mark is not None else "premium")
    return {**raw, "premium": float(premium), "premium_source": "given" if given else "chain_mid", "iv": iv, "iv_source": iv_source,
            "multiplier": chain["multiplier"]}


def _legs_from_structure(args: dict[str, Any], chain: dict[str, Any]) -> list[dict[str, Any]] | str:
    spec = CATALOG[args["structure"]]
    quotes: list[dict[str, Any]] = []
    for position, kind, idx in spec["legs"]:
        if position == "stock":
            continue
        got = _quote_leg({"instrument": "option", "position": position, "kind": kind, "strike": args["strikes"][idx], "premium": None, "quantity": args["quantity"]}, chain)
        if isinstance(got, str):
            return got
        quotes.append(got)
    try:
        built = build_legs(args["structure"], args["strikes"], [q["premium"] for q in quotes], args["quantity"], chain["multiplier"], chain["spot"])
    except ValueError:
        raise _bad() from None
    out: list[dict[str, Any]] = []
    it = iter(quotes)
    for leg in built:
        if leg.instrument == "stock":
            out.append({"instrument": "stock", "position": "long", "premium": leg.premium, "premium_source": "spot", "quantity": leg.quantity, "multiplier": 1.0})
        else:
            out.append(next(it))
    return out


def _legs_from_request(args: dict[str, Any], chain: dict[str, Any]) -> list[dict[str, Any]] | str:
    out: list[dict[str, Any]] = []
    for raw in args["legs"]:
        if raw["instrument"] == "stock":
            shares = raw["quantity"] if raw["quantity"] is not None else args["quantity"] * chain["multiplier"]     # no quantity: the shares of the contracts asked
            out.append({**raw, "quantity": shares, "premium": chain["spot"] if raw["premium"] is None else raw["premium"],
                        "premium_source": "spot" if raw["premium"] is None else "given", "multiplier": 1.0})
            continue
        got = _quote_leg(raw, chain)
        if isinstance(got, str):
            return got
        out.append(got)
    return out


def _quant_legs(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"instrument": x["instrument"], "position": x["position"], "kind": x.get("kind"), "strike": x.get("strike"), "premium": x["premium"],
             "quantity": x["quantity"], "multiplier": x["multiplier"]} for x in legs]


def _columns(total_days: float, count: int) -> list[float]:
    """Calendar days from now of each column: whole days from today to the last one, which is expiration itself (the fraction of the day to its close)."""
    steps = [round(total_days * i / (count - 1)) for i in range(count - 1)]
    days = sorted({float(s) for s in steps if s < total_days})
    return days + [total_days]


# ---- events -------------------------------------------------------------------------------------------------------------------------------

def _relation(day: date | None, expiration: date) -> str:
    if day is None:
        return "none"
    return "before_expiration" if day <= expiration else "after_expiration"


def _event_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _earnings_event(desk_id: int, symbol: str, today: date, expiration: date) -> dict[str, Any]:
    block = _earnings_block(desk_id, symbol, today)
    base = {"kind": "earnings", "ok": block["ok"], "source": block.get("source")}
    if not block["ok"]:
        return {**base, "date": None, "relation": "no_data", "message": t("desks.option_detail.no_data", source=block["source"], reason=block["reason"])}
    row = block.get("next")
    day = _event_day(row.get("date")) if row else None
    rel = _relation(day, expiration)
    days = None if day is None else (day - today).days
    text = {"before_expiration": lambda: t("desks.option_detail.earnings_before", date=day.isoformat(), days=days),
            "after_expiration": lambda: t("desks.option_detail.earnings_after", date=day.isoformat()),
            "none": lambda: t("desks.option_detail.earnings_none", days=EARNINGS_DAYS)}[rel]()
    return {**base, "fetched_at": block.get("fetched_at"), "date": None if day is None else day.isoformat(), "hour": row.get("hour") if row else None,
            "days_from_today": days, "before_expiration": rel == "before_expiration", "relation": rel, "message": text}


def _dividend_event(desk_id: int, symbol: str, today: date, expiration: date) -> dict[str, Any]:
    block = _ex_dividend_block(desk_id, symbol)
    base = {"kind": "ex_dividend", "ok": block["ok"], "source": block.get("source")}
    if not block["ok"]:
        return {**base, "date": None, "relation": "no_data", "message": t("desks.option_detail.no_data", source=block["source"], reason=block["reason"])}
    day = _event_day(block.get("next"))
    if day is not None and day < today:
        day = None
    rel = _relation(day, expiration)
    days = None if day is None else (day - today).days
    text = {"before_expiration": lambda: t("desks.option_detail.dividend_before", date=day.isoformat(), days=days),
            "after_expiration": lambda: t("desks.option_detail.dividend_after", date=day.isoformat()),
            "none": lambda: t("desks.option_detail.dividend_none")}[rel]()
    return {**base, "fetched_at": block.get("fetched_at"), "date": None if day is None else day.isoformat(), "last_date": block.get("last"),
            "amount": block.get("amount"), "frequency": block.get("frequency"), "days_from_today": days, "before_expiration": rel == "before_expiration",
            "relation": rel, "model_ignores_dividend": True, "message": text}


# ---- the answer -------------------------------------------------------------------------------------------------------------------------

def option_detail(desk_id: int, symbol: Any, expiration: Any, legs: Any = None, structure: Any = None, strikes: Any = None, quantity: Any = None,
                  range_pct: Any = None, iv_mult: Any = None, rows: Any = None, columns: Any = None) -> dict[str, Any]:
    """The detail of one strategy: table by date and price, curves, header figures and events. Raises DeskError bad_option_detail_args for a request that is not
    valid; a source that cannot answer is {"ok": False, "source", "reason", "message"}, never a zero."""
    req = check_arguments(symbol, expiration, legs, structure, strikes, quantity, range_pct, iv_mult, rows, columns)
    sym, day = req["symbol"], req["expiration"]
    today = _today()
    base = {"desk_id": desk_id, "symbol": sym, "expiration": day.isoformat(), "structure": req["structure"], "limits": LIMITS}
    if day < today:
        reason = t("desks.option_detail.expired")
        return {**base, "ok": False, "source": t("desks.option_detail.source_chain"), "code": "expired", "reason": reason,
                "message": t("desks.option_detail.no_data", source=t("desks.option_detail.source_chain"), reason=reason)}
    chain = _load_chain(desk_id, sym, day)
    if not chain["ok"]:
        return {**base, "ok": False, "source": chain["source"], "code": chain["code"], "reason": chain["reason"],
                "message": t("desks.option_detail.no_data", source=chain["source"], reason=chain["reason"])}
    if chain["years"] <= 0:
        reason = t("desks.option_detail.expired")
        return {**base, "ok": False, "source": chain["source"], "code": "expired", "reason": reason,
                "message": t("desks.option_detail.no_data", source=chain["source"], reason=reason)}
    resolved = _legs_from_structure(req, chain) if req["structure"] else _legs_from_request(req, chain)
    if isinstance(resolved, str):
        return {**base, "ok": False, "source": chain["source"], "code": "no_quote", "reason": resolved,
                "message": t("desks.option_detail.no_data", source=chain["source"], reason=resolved)}
    option_legs = [x for x in resolved if x["instrument"] == "option"]
    total_days = chain["years"] * quant.DAYS_PER_YEAR
    res = quant.pnl_grid(_quant_legs(resolved), chain["spot"], chain["years"], multiplier=chain["multiplier"], range_pct=req["range_pct"], rows=req["rows"],
                         days=_columns(total_days, req["columns"]), iv_by_leg=[x["iv"] for x in option_legs], iv_mult=req["iv_mult"], rate=chain["rate"])
    if res.value is None:
        reason = "; ".join(res.warnings)
        return {**base, "ok": False, "source": chain["source"], "code": "unusable", "reason": reason,
                "message": t("desks.option_detail.no_data", source=chain["source"], reason=reason)}
    value = res.value
    for col in value["columns"]:
        col["date"] = day.isoformat() if col["expiration"] else (today + timedelta(days=int(round(col["days"])))).isoformat()
    warnings = list(res.warnings)
    for x in option_legs:
        if x["iv"] is None:
            warnings.append(t("desks.option_detail.no_iv", kind=x["kind"], strike=f"{x['strike']:g}"))
    events = {"expiration": {"date": day.isoformat(), "days": (day - today).days},
              "earnings": _earnings_event(desk_id, sym, today, day), "ex_dividend": _dividend_event(desk_id, sym, today, day)}
    return {**base, "ok": True, "days": (day - today).days, "spot": chain["spot"], "multiplier": chain["multiplier"], "quantity": req["quantity"],
            "source": chain["source"], "price_feed": chain["price_feed"], "greeks": chain["greeks"], "as_of": chain["as_of"], "market_open": chain["market_open"],
            "delayed": chain["delayed"], "notes": chain["notes"],
            "legs": [{**x, "iv_used": None if x.get("iv") is None else x["iv"] * req["iv_mult"]} if x["instrument"] == "option" else x for x in resolved],
            "metrics": value["metrics"], "table": {"rows": value["rows"], "columns": value["columns"], "pnl_usd": value["pnl_usd"], "spot_row": value["spot_row"],
                                                   "range_pct": value["range_pct"]},
            "curves": value["curves"], "breakevens": value["breakevens"],
            "model": {"rate": value["rate"], "dividend_yield": value["dividend_yield"], "iv_mult": value["iv_mult"], "iv_mean": value["iv_mean"],
                      "years": value["years"], "days_to_expiration": value["days_to_expiration"], "method": res.method},
            "events": events, "recalc": {"price_move_pct": RECALC_PRICE_MOVE_PCT, "debounce_ms": RECALC_DEBOUNCE_MS, "by_event_only": True, "anchor_spot": chain["spot"]},
            "warnings": warnings}
