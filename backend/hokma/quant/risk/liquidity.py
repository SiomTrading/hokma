"""Liquidity cost: illiquidity per dollar traded and the effective spread implied by price reversals."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.risk import _common as c
from hokma.quant.risk import _lib
from hokma.quant.types import QuantResult


@registry.register("risk.amihud_illiquidity", module="risk",
                   summary="Amihud illiquidity: mean absolute return per scaled dollar of volume (default scale one million). Higher means a costlier exit.",
                   inputs={"returns": {"type": "series", "required": True}, "dollar_volume": {"type": "series", "required": True},
                           "scale": {"type": "float", "required": False, "default": 1_000_000.0}},
                   piece_ids=("FT-065",))
def amihud_illiquidity(returns: Any, dollar_volume: Any, scale: float = 1_000_000.0) -> QuantResult:
    name, unit, method, pieces = "risk.amihud_illiquidity", "ratio", "amihud", ("FT-065",)
    params = {"scale": scale}
    r = helpers.as_series(returns, name="returns")
    v = helpers.as_series(dollar_volume, name="dollar_volume")
    if len(r) != len(v):
        raise BadInput(f"returns and dollar_volume must have the same length, got {len(r)} and {len(v)}")
    frame = pd.concat([r.rename("r"), v.rename("v")], axis=1).replace([np.inf, -np.inf], np.nan)
    cleaned = frame.dropna()
    cleaned = cleaned[cleaned["v"] > 0]
    warns = (f"dropped {len(frame) - len(cleaned)} row(s) with missing values or no volume",) if len(cleaned) != len(frame) else ()
    if len(cleaned) < 2 or not scale > 0:
        return c.empty(name, unit, method, warns + ("need 2 observations with positive volume and a positive scale",), params=params, piece_ids=pieces)
    try:
        value = _lib.ft_liquidity.get_amihud_illiquidity(cleaned["r"], cleaned["v"], scale=scale)
    except Exception as exc:
        return c.empty(name, unit, method, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    return c.build(name, float(value), unit=unit, method=method, params=params, piece_ids=pieces, inputs=(cleaned,),
                   as_of_source=cleaned["r"], warnings=warns)


@registry.register("risk.roll_spread", module="risk",
                   summary="Roll effective spread from the serial covariance of price changes; includes the relative spread and whether the estimate is valid.",
                   inputs={"close_prices": {"type": "series", "required": True}}, piece_ids=("FT-065",))
def roll_spread(close_prices: Any) -> QuantResult:
    def compute(s: pd.Series) -> dict:
        table = _lib.ft_liquidity.get_roll_spread(s)
        return {"roll_spread": table["Roll Spread"], "relative_roll_spread": table["Relative Roll Spread"],
                "autocovariance": table["Autocovariance"], "valid_estimate": bool(table["Valid Estimate"])}

    series = helpers.as_series(close_prices, name="close_prices")
    if (series.dropna() <= 0).any():
        return c.empty("risk.roll_spread", "price", "roll", "close prices must be positive", piece_ids=("FT-065",))
    return c.measure("risk.roll_spread", series, unit="price", method="roll", params={}, piece_ids=("FT-065",), compute=compute, min_obs=10)
