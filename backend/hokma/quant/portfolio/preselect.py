"""Rule based pre-selection of assets (SK-079, SK-080): drop series without variance; keep all complete series, the k most extreme
by a measure, the non-dominated ones, or only contracts that are not about to expire.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _common as c
from hokma.quant.portfolio import _lib
from hokma.quant.types import QuantResult

_RET = {"type": "dataframe", "required": True}
METHODS = ("complete", "k_extremes", "non_dominated", "non_expiring")
MEASURES = ("sharpe_ratio", "mean", "standard_deviation", "variance", "cvar", "max_drawdown", "sortino_ratio", "calmar_ratio")
MAX_LOOKAHEAD_DAYS = 3650


def _result(panel: pd.DataFrame, keep: np.ndarray, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    keep = np.asarray(keep, dtype=bool)
    cols = [str(col) for col in panel.columns]
    out: dict[str, Any] = {"kept": [a for a, k in zip(cols, keep) if k], "dropped": [a for a, k in zip(cols, keep) if not k]}
    out.update(extra or {})
    return out


@registry.register("portfolio.drop_zero_variance", module="portfolio",
                   summary="Drop the series whose variance is not above a threshold (flat or constant series). Returns the kept and dropped assets.",
                   inputs={"returns": _RET, "threshold": {"type": "float", "required": False, "default": 1e-8}},
                   piece_ids=("SK-079",))
def drop_zero_variance(returns: Any, threshold: float = 1e-8) -> QuantResult:
    name, pieces = "portfolio.drop_zero_variance", ("SK-079",)
    params = {"threshold": threshold}

    def compute() -> Any:
        level = c.number("threshold", threshold, low=0.0)
        panel, warns = c.usable_panel(returns, min_obs=2)
        if panel is None:
            raise c.Domain("; ".join(warns))
        keep = _lib.sk_pre_selection.DropZeroVariance(threshold=level).fit(panel).to_keep_
        out = _result(panel, keep, {"variance": c.weights_to_dict(panel.var(ddof=0).to_numpy(), panel.columns)})
        if not out["kept"]:
            raise c.Domain("every series has no variance")
        return out, warns

    return c.run(name, unit="assets", method="DropZeroVariance", params=params, piece_ids=pieces, compute=compute, inputs=(returns, params))


def _expiration_map(expirations: Any, columns: pd.Index) -> dict[str, pd.Timestamp]:
    if not isinstance(expirations, Mapping) or not expirations:
        raise c.Domain("non_expiring needs expirations: a dict asset -> expiration date")
    names = {str(col) for col in columns}
    out: dict[str, pd.Timestamp] = {}
    for asset, when in expirations.items():
        if str(asset) not in names:
            raise BadInput(f"expirations: unknown asset {asset!r}")
        if isinstance(when, (datetime, date, pd.Timestamp)):
            stamp = pd.Timestamp(when)
        elif isinstance(when, str):
            stamp = pd.Timestamp(when)
        else:
            raise BadInput(f"expirations[{asset}]: expected a date")
        if pd.isna(stamp):
            raise c.Domain(f"expirations[{asset}] is not a valid date")
        out[str(asset)] = stamp.tz_localize(None) if stamp.tzinfo else stamp
    return out


@registry.register("portfolio.select", module="portfolio",
                   summary="Pre-select assets by rule: complete series, k extremes by a measure, non-dominated, or non-expiring contracts (expiration dates and look-ahead days).",
                   inputs={"returns": _RET, "method": {"type": "str", "required": True},
                           "k": {"type": "int", "required": False, "default": 10},
                           "measure": {"type": "str", "required": False, "default": "sharpe_ratio"},
                           "highest": {"type": "bool", "required": False, "default": True},
                           "expirations": {"type": "dict", "required": False},
                           "lookahead_days": {"type": "int", "required": False, "default": 0},
                           "as_of": {"type": "str", "required": False},
                           "threshold": {"type": "float", "required": False, "default": -0.5},
                           "drop_assets_with_internal_nan": {"type": "bool", "required": False, "default": False}},
                   piece_ids=("SK-080",))
def select(returns: Any, method: str, k: int = 10, measure: str = "sharpe_ratio", highest: bool = True, expirations: Any = None,
           lookahead_days: int = 0, as_of: Any = None, threshold: float = -0.5, drop_assets_with_internal_nan: bool = False) -> QuantResult:
    name, pieces = "portfolio.select", ("SK-080",)
    params = {"method": method, "k": k, "measure": measure, "highest": highest, "lookahead_days": lookahead_days, "as_of": as_of,
              "threshold": threshold, "drop_assets_with_internal_nan": drop_assets_with_internal_nan}

    def compute() -> Any:
        which = c.choice("method", method, METHODS)
        panel = c.as_panel(returns)
        if panel.shape[0] == 0:
            raise c.Domain("returns is empty")
        warns: tuple[str, ...] = ()
        pre = _lib.sk_pre_selection
        if which == "complete":
            est = pre.SelectComplete(drop_assets_with_internal_nan=bool(drop_assets_with_internal_nan))
            data = panel
        else:
            data, warns = c.clean_panel(panel, min_obs=2)
            if len(data) < 2:
                raise c.Domain("; ".join(warns))
            if which == "k_extremes":
                kk = int(c.number("k", k, low=1, integer=True))
                m = c.choice("measure", measure, MEASURES)
                est = pre.SelectKExtremes(k=kk, measure=_lib.sk_measures.RatioMeasure(m) if m in {x.value for x in _lib.sk_measures.RatioMeasure}
                                          else (_lib.sk_measures.PerfMeasure(m) if m in {x.value for x in _lib.sk_measures.PerfMeasure}
                                                else _lib.RiskMeasure(m)), highest=bool(highest))
            elif which == "non_dominated":
                est = pre.SelectNonDominated(threshold=c.number("threshold", threshold))
            else:
                days = int(c.number("lookahead_days", lookahead_days, low=0, high=MAX_LOOKAHEAD_DAYS, integer=True))
                dates = _expiration_map(expirations, panel.columns)
                if as_of is not None:
                    cutoff = pd.Timestamp(as_of)
                    if pd.isna(cutoff):
                        raise c.Domain("as_of is not a valid date")
                    if not isinstance(data.index, pd.DatetimeIndex):
                        raise BadInput("as_of needs returns indexed by date")
                    data = data.loc[: cutoff.tz_localize(None) if cutoff.tzinfo else cutoff]
                    if len(data) < 2:
                        raise c.Domain("fewer than 2 rows up to as_of")
                if not isinstance(data.index, pd.DatetimeIndex):
                    raise BadInput("non_expiring needs returns indexed by date")
                est = pre.SelectNonExpiring(expiration_dates=dates, expiration_lookahead=timedelta(days=days))
        keep = np.asarray(_lib.quiet(est.fit, data).to_keep_, dtype=bool)
        if keep.shape[0] != panel.shape[1]:
            raise c.Domain("the selection does not match the assets")
        out = _result(panel, keep)
        if not out["kept"]:
            raise c.Domain("the rule left no asset")
        return out, warns

    return c.run(name, unit="assets", method=method if isinstance(method, str) else "select", params=params, piece_ids=pieces,
                 compute=compute, inputs=(returns, params))
