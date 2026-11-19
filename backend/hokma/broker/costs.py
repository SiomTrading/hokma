"""Cost of a fill: slippage against the trader and commission (task 3294, C7-5b). Pure functions. No route calls them in
class 7; class 9 will, with a real reference price. The idea (adverse slippage, a commission model per broker) comes from
Nautilus Trader's fill and fee models; no code is copied (LGPL)."""
from __future__ import annotations

from typing import Any, Mapping

from hokma.db.errors import ValidationError

DEFAULT_COMMISSION: dict[str, Any] = {"type": "bps", "value": 1.0, "min": 0.0}
DEFAULT_SLIPPAGE_BPS = 2.0
COMMISSION_TYPES = ("bps", "per_share")


MAX_BPS = 10_000            # 100 %: slippage and a commission in basis points cannot be more than the whole


def _number(value: Any, what: str, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
        raise ValidationError(f"{what} must be a number of 0 or more; got {value!r}")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{what} must be at most {maximum:g}; got {value!r}")
    return float(value)


def validate_commission(cfg: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(cfg, Mapping) or cfg.get("type") not in COMMISSION_TYPES:
        raise ValidationError(f"commission type must be one of {', '.join(COMMISSION_TYPES)}")
    return {"type": cfg["type"], "value": _number(cfg.get("value"), "commission value", MAX_BPS),
            "min": _number(cfg.get("min", 0.0), "commission minimum", MAX_BPS)}


def fill_price(side: str, ref_price: float, slippage_bps: float) -> float:
    """Against the trader: a buy fills higher, a sell lower. Rounded to 4 decimals."""
    if side not in ("buy", "sell"):
        raise ValidationError(f"side must be 'buy' or 'sell'; got {side!r}")
    ref, bps = _number(ref_price, "reference price"), _number(slippage_bps, "slippage")
    return round(ref * (1 + bps / 10_000 if side == "buy" else 1 - bps / 10_000), 4)


def commission(notional: float, qty: float, cfg: Mapping[str, Any]) -> float:
    """`bps`: value basis points of the notional; `per_share`: value per unit. At least `min`, except when nothing
    traded (qty 0 pays nothing). Rounded to the cent."""
    cfg = validate_commission(cfg)
    notional, qty = _number(notional, "notional"), _number(qty, "quantity")
    if qty == 0:
        return 0.0
    raw = notional * cfg["value"] / 10_000 if cfg["type"] == "bps" else qty * cfg["value"]
    return round(max(raw, cfg["min"]), 2)
