"""Option expirations and chain for the module (SPEC-019, B1; FT-033).

The dates and the chain come from hokma.market.options (Schwab or Alpaca, SPEC-014 and SPEC-015), which this module only
calls and never changes. The source of the library (delayed, without greeks) answers only when no chain is connected, and
the answer says so in source and delayed. A failure of a connected source is reported, never replaced by delayed data.
"""
from __future__ import annotations

from typing import Any

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import _lib
from hokma.quant.types import QuantResult

MAX_SYMBOL = 12
LIBRARY_SOURCE = "library"
NO_CHAIN_CODES = ("no_connection", "desk")


def _symbol(value: Any) -> str:
    if not isinstance(value, str):
        raise BadInput(f"symbol must be a string, got {type(value).__name__}")
    text = value.strip().upper()
    if not text or len(text) > MAX_SYMBOL or not all(ch.isalnum() or ch in ".-" for ch in text):
        raise BadInput("symbol must be a US stock or ETF symbol, for example SPY")
    return text


def _market() -> Any:
    """The market options module, imported when a call needs it so that loading the quant registry never pulls the broker packages."""
    from hokma.market import options

    return options


def _library_rows(symbol: str, expiration: str, kind: str | None) -> list[dict[str, Any]]:
    """Rows from the library source (no greeks), in the keys of the chain tool."""
    rows: list[dict[str, Any]] = []
    for put in ((False, True) if kind is None else (kind == "put",)):
        table = _lib.ft_options.get_option_chains([symbol], expiration, put_option=put)
        if table is None or len(table) == 0:
            continue
        for (_, strike), rec in table.iterrows():
            bid, ask = c.row_float(rec.to_dict(), "Bid"), c.row_float(rec.to_dict(), "Ask")
            mid = (bid + ask) / 2.0 if bid is not None and ask is not None and bid > 0 and ask > 0 else None
            rows.append({"symbol": rec.get("Contract Symbol"), "type": "put" if put else "call", "strike": float(strike), "expiration": expiration,
                         "bid": bid, "ask": ask, "mid": mid, "last": c.row_float(rec.to_dict(), "Last Price"), "volume": c.row_float(rec.to_dict(), "Volume"),
                         "open_interest": c.row_float(rec.to_dict(), "Open Interest"), "iv": c.row_float(rec.to_dict(), "Implied Volatility"),
                         "delta": None, "gamma": None, "theta": None, "vega": None})
    return rows


@registry.register("options.expiry_dates", module="options",
                   summary="Option expiration dates of a US stock or ETF from the connected chain (Schwab or Alpaca): date, days and contract multiplier. With no connected chain the library source answers, marked source 'library' and delayed true, and without multiplier.",
                   inputs={"symbol": {"type": "str", "required": True}, "desk_id": {"type": "int", "required": False, "default": None}},
                   piece_ids=("FT-033",))
def expiry_dates(symbol: str, desk_id: int | None = None) -> QuantResult:
    name, unit, method, pieces = "options.expiry_dates", "dates", "market_options_expirations", ("FT-033",)
    sym = _symbol(symbol)
    desk = None if desk_id is None else c.positive_int("desk_id", desk_id)
    params = {"symbol": sym, "desk_id": desk}
    market = _market()

    def compute() -> dict:
        try:
            got = market.expirations(sym, desk)
        except market.OptionsError as exc:
            if getattr(exc, "code", "") not in NO_CHAIN_CODES:
                raise ValueError(f"the connected chain failed ({exc.code}): {exc}") from exc
            dates = list(_lib.ft_options.get_option_expiry_dates(sym))
            if not dates:
                raise ValueError("no chain is connected and the library source has no expirations for this symbol") from exc
            return {"symbol": sym, "source": LIBRARY_SOURCE, "delayed": True, "expirations": [{"date": d, "days": None, "multiplier": None} for d in dates]}
        return {"symbol": sym, "source": got.get("source"), "delayed": False, "expirations": got.get("expirations", [])}

    result = c.guarded(name, unit, method, pieces, params, compute)
    if result.value is not None and result.value["source"] == LIBRARY_SOURCE:
        return QuantResult(name=result.name, value=result.value, unit=result.unit, method=result.method, params=result.params,
                           inputs_hash=result.inputs_hash, as_of=result.as_of,
                           warnings=result.warnings + ("no chain is connected: the library source answered, delayed and without multiplier",), piece_ids=result.piece_ids)
    return result


@registry.register("options.option_chain", module="options",
                   summary="Option chain of a US stock or ETF for one expiration (YYYY-MM-DD) or a window of days, from the connected chain (Schwab or Alpaca) with bid, ask, mid, volume, open interest, implied volatility and the provider's greeks. With no connected chain the library source answers for one expiration, marked source 'library' and delayed true, with no greeks. The rows are the contracts that the other options functions take.",
                   inputs={"symbol": {"type": "str", "required": True}, "expiration": {"type": "str", "required": False, "default": None},
                           "kind": {"type": "str", "required": False, "default": None}, "min_days": {"type": "int", "required": False, "default": None},
                           "max_days": {"type": "int", "required": False, "default": None}, "limit": {"type": "int", "required": False, "default": None},
                           "desk_id": {"type": "int", "required": False, "default": None}},
                   piece_ids=("FT-033",))
def option_chain(symbol: str, expiration: str | None = None, kind: str | None = None, min_days: int | None = None, max_days: int | None = None,
                 limit: int | None = None, desk_id: int | None = None) -> QuantResult:
    name, unit, method, pieces = "options.option_chain", "contracts", "market_options_chain", ("FT-033",)
    sym = _symbol(symbol)
    if kind is not None and c.kind_flag(kind) is None:
        return c.empty(name, unit, method, "kind must be 'call' or 'put'", params={"symbol": sym, "kind": kind}, piece_ids=pieces)
    if expiration is not None and not isinstance(expiration, str):
        raise BadInput("expiration must be an ISO date string")
    desk = None if desk_id is None else c.positive_int("desk_id", desk_id)
    lo = None if min_days is None else c.positive_int("min_days", min_days, low=0)
    hi = None if max_days is None else c.positive_int("max_days", max_days, low=0)
    lim = None if limit is None else c.positive_int("limit", limit)
    params = {"symbol": sym, "expiration": expiration, "kind": kind, "min_days": lo, "max_days": hi, "limit": lim, "desk_id": desk}
    market = _market()

    def compute() -> dict:
        try:
            got = market.options_chain(sym, expiration=expiration, type=kind, min_days=lo, max_days=hi, limit=lim, desk_id=desk)
        except market.OptionsError as exc:
            if getattr(exc, "code", "") not in NO_CHAIN_CODES:
                raise ValueError(f"the connected chain failed ({exc.code}): {exc}") from exc
            if expiration is None:
                raise ValueError("no chain is connected and the library source needs one expiration date") from exc
            rows = _library_rows(sym, expiration, kind)
            if not rows:
                raise ValueError("no chain is connected and the library source has no contracts for this symbol and date") from exc
            return {"symbol": sym, "source": LIBRARY_SOURCE, "delayed": True, "underlying_price": None, "as_of": None, "greeks": "none", "contracts": rows}
        delayed = bool(got["delayed"]) if "delayed" in got else got.get("price_feed") != "opra"
        return {"symbol": sym, "source": got.get("source"), "delayed": delayed, "underlying_price": got.get("underlying_price"), "as_of": got.get("as_of"),
                "greeks": got.get("greeks"), "contracts": got.get("contracts", [])}

    result = c.guarded(name, unit, method, pieces, params, compute)
    if result.value is not None and result.value["source"] == LIBRARY_SOURCE:
        return QuantResult(name=result.name, value=result.value, unit=result.unit, method=result.method, params=result.params,
                           inputs_hash=result.inputs_hash, as_of=result.as_of,
                           warnings=result.warnings + ("no chain is connected: the library source answered, delayed and without greeks",), piece_ids=result.piece_ids)
    return result
