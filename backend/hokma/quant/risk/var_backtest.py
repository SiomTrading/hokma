"""Backtest of a value at risk (and expected shortfall) model: Kupiec, Christoffersen and Acerbi-Szekely tests.

`var_series` and `cvar_series` use the module convention: positive losses, aligned to the returns by index
(or by position when they are plain lists of the same length).
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.risk import _common as c
from hokma.quant.risk import _lib
from hokma.quant.types import QuantResult

TESTS = ("kupiec", "christoffersen", "acerbi_szekely")
PIECES = ("FT-048", "CC-06")


def _aligned(returns: Any, estimates: Any, label: str) -> pd.Series:
    r = helpers.as_series(returns, name="returns")
    if isinstance(estimates, pd.Series):
        e = helpers.as_series(estimates, name=label)
    else:
        e = helpers.as_series(estimates, name=label)
        if len(e) != len(r):
            raise BadInput(f"{label}: expected {len(r)} values like returns, got {len(e)}")
        e.index = r.index
    return e


@registry.register("risk.var_backtest", module="risk",
                   summary="Backtest of a VaR model: kupiec (breach frequency), christoffersen (independence of breaches) or acerbi_szekely (expected shortfall). Returns the statistic and the p value.",
                   inputs={"returns": {"type": "series", "required": True}, "var_series": {"type": "series", "required": True},
                           "test": {"type": "str", "required": False, "default": "kupiec"},
                           "level": {"type": "float", "required": False, "default": 0.95},
                           "cvar_series": {"type": "series", "required": False, "default": None},
                           "seed": {"type": "int", "required": False, "default": 42}},
                   piece_ids=PIECES)
def backtest_var(returns: Any, var_series: Any, test: str = "kupiec", level: float = 0.95, cvar_series: Any = None,
                 seed: int = 42) -> QuantResult:
    name, unit = "risk.var_backtest", "test_statistic"
    params = {"test": test, "level": level, "seed": seed}
    if test not in TESTS:
        return c.empty(name, unit, test, f"unknown test {test!r}; use one of {list(TESTS)}", params=params, piece_ids=PIECES)
    problem = c.level_warning(level)
    if problem:
        return c.empty(name, unit, test, problem, params=params, piece_ids=PIECES)
    r = helpers.as_series(returns, name="returns")
    v = _aligned(returns, var_series, "var_series")
    frame = pd.concat([r.rename("r"), v.rename("v")], axis=1, join="inner").replace([np.inf, -np.inf], np.nan)
    if test == "acerbi_szekely":
        if cvar_series is None:
            return c.empty(name, unit, test, "acerbi_szekely needs cvar_series", params=params, piece_ids=PIECES)
        frame["e"] = _aligned(returns, cvar_series, "cvar_series")
    cleaned = frame.dropna()
    warns = (f"dropped {len(frame) - len(cleaned)} row(s) with missing values",) if len(cleaned) != len(frame) else ()
    if len(cleaned) < 10 or c.is_constant(cleaned["r"]):
        return c.empty(name, unit, test, warns + ("need 10 complete observations and a non constant return series",),
                       params=params, piece_ids=PIECES)
    ret, est = cleaned["r"], -cleaned["v"]
    try:
        if test == "kupiec":
            table = _lib.ft_backtest.get_kupiec_test(ret, est, 1.0 - level)
            statistic, p_value = table["Kupiec Statistic"], table["P-Value"]
        elif test == "christoffersen":
            table = _lib.ft_backtest.get_christoffersen_test(ret, est)
            statistic, p_value = table["Christoffersen Statistic"], table["P-Value"]
        else:
            table = _lib.ft_backtest.get_acerbi_szekely_test(ret, est, -cleaned["e"], 1.0 - level, random_state=seed)
            statistic, p_value = table["Acerbi-Szekely Statistic"], table["P-Value"]
    except Exception as exc:
        return c.empty(name, unit, test, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=PIECES)
    breaches = int((ret < est).sum())
    value = {"statistic": float(statistic), "p_value": float(p_value), "rejected_at_5pct": bool(p_value < 0.05),
             "breaches": breaches, "expected_breaches": float((1.0 - level) * len(cleaned)), "observations": int(len(cleaned))}
    return c.build(name, value, unit=unit, method=test, params=params, piece_ids=PIECES, inputs=(ret, est),
                   as_of_source=ret, warnings=warns)
