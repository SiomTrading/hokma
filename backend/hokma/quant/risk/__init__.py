"""Risk module (SPEC-017, section 4.4).

Public entry points: var, cvar, drawdown_risk, var_backtest, stress, kelly_payoff. Every function of the module is also
registered as "risk.<name>" in hokma.quant.registry, which is how agents reach it. Risk figures are positive losses.
"""
from __future__ import annotations

import importlib
from typing import Any


# Importing the submodules registers their functions. importlib is used for the ones whose name is also a public function.
from hokma.quant.risk import copulas, dispersion, distributions, liquidity, moments, scenarios, sizing, volatility  # noqa: F401
from hokma.quant.types import QuantResult

_var = importlib.import_module("hokma.quant.risk.var")
_drawdown_risk = importlib.import_module("hokma.quant.risk.drawdown_risk")
_var_backtest = importlib.import_module("hokma.quant.risk.var_backtest")

stress = scenarios.stress
kelly_payoff = sizing.kelly_payoff


def var(returns: Any, *, level: float = 0.95, method: str = "historical") -> QuantResult:
    """Value at risk as a positive loss. Methods: historical, empirical, gaussian, student_t, cornish_fisher, evt."""
    return _var.value_at_risk(returns, level=level, method=method)


def cvar(returns: Any, *, level: float = 0.95, method: str = "historical") -> QuantResult:
    """Expected shortfall as a positive loss. Methods: historical, empirical, gaussian, student_t, laplace, logistic, cornish_fisher, evt."""
    return _var.conditional_value_at_risk(returns, level=level, method=method)


def drawdown_risk(returns: Any, *, measure: str = "cdar", level: float = 0.95) -> QuantResult:
    """One drawdown measure: max, average, dar, cdar, edar or ulcer."""
    return _drawdown_risk.drawdown_measure(returns, measure=measure, level=level)


def var_backtest(returns: Any, var_series: Any, *, test: str = "kupiec", **kwargs: Any) -> QuantResult:
    """Backtest of a VaR model: kupiec, christoffersen or acerbi_szekely (the last needs cvar_series=...)."""
    return _var_backtest.backtest_var(returns, var_series, test=test, **kwargs)


__all__ = ["var", "cvar", "drawdown_risk", "var_backtest", "stress", "kelly_payoff"]
