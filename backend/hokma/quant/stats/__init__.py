"""stats module (SPEC-019, H3). Functions register as "stats.<name>" in hokma.quant.registry.

Files: events (event study, its statistics, types, chart, earnings preview, news reaction failure), volatility (range
estimators, HAR-RV, GJR-GARCH, EGARCH), timeseries (unit roots, ARIMA, VAR, IRF, FEVD, VECM, Granger, breaks), cointegration
(Engle-Granger, Johansen, hedge amounts), kalman, hurst, regression (OLS to quantile, Fama-MacBeth, panel), causal and sentiment.

Yang-Zhang and Rogers-Satchell exist once, in volatility; the indicators module registers aliases to the same functions
(see `hokma.quant.stats.volatility.ALIASES`).
"""
from __future__ import annotations

from hokma.quant.stats import causal, cointegration, events, hurst, kalman, regression, sentiment, timeseries, volatility  # noqa: F401
