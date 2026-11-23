"""DuPont decompositions of the return on equity (FT-013): three factor, five factor and the Nissim-Penman split."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _common as c
from hokma.quant.fundamentals import _lib

METHODS = ("three_factor", "extended", "nissim_penman")
_FUNCTIONS = {"three_factor": "get_dupont_analysis", "extended": "get_extended_dupont_analysis", "nissim_penman": "get_nissim_penman_decomposition"}


@registry.register("fundamentals.dupont", module="fundamentals", piece_ids=("FT-013",),
                   inputs={"statements": c.stmt_input(), "as_of": c.AS_OF_INPUT, "method": {"type": "str", "required": False, "default": "three_factor"}},
                   summary="DuPont decomposition of ROE by period: three_factor (margin, turnover, multiplier), extended (five factors) or nissim_penman (operating return, borrowing cost, leverage). Assets and equity are averaged over two periods.")
def dupont(statements: Any, as_of: Any = None, method: str = "three_factor"):
    name, pieces = "fundamentals.dupont", ("FT-013",)
    if method not in METHODS:
        raise BadInput(f"method must be one of {list(METHODS)}")
    items, warns = c.prepare(statements, as_of)
    label = f"dupont {method}"
    if len(items.frame) == 0:
        return c.empty(name, "ratio", label, warns + ("no periods in the statements",), params={"method": method}, piece_ids=pieces)
    result, missing = items.call(getattr(_lib.ft_dupont, _FUNCTIONS[method]), {"average_common_equity": "average_total_equity"})
    if result is None or not isinstance(result, pd.DataFrame):
        gaps = tuple(f"data_missing:{m}" for m in missing) or ("not computable",)
        return c.empty(name, "ratio", label, warns + gaps, params={"method": method}, piece_ids=pieces)
    rows = {str(k): result.loc[k].astype(float).replace([np.inf, -np.inf], np.nan) for k in result.index}
    table = c.series_table(rows)
    last_roe = table["latest"].get("Return on Equity") if method != "nissim_penman" else table["latest"].get("Return on Common Equity")
    table["return_on_equity_latest"] = last_roe
    return c.build(name, table, unit="ratio", method=label, params={"method": method}, piece_ids=pieces, inputs=(items.frame,),
                   warnings=warns + tuple(items.notes), as_of=items.frame.index[-1])
