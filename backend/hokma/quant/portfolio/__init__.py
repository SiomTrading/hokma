"""portfolio module (SPEC-019, H3). Functions register as "portfolio.<name>" in hokma.quant.registry.

Files: blend (mix the views of a desk), optimize (mean-risk, robust CVaR, baselines), priors (variance, implied covariance, Black-Litterman,
entropy pooling), factors (factor model container, time series and characteristics models), uncertainty (uncertainty sets), preselect
(drop zero variance, rule based selection), alpha (forecast units, three alpha estimators, evaluation), container (portfolio, charts,
contribution, failed result, population), weights (baseline weights), ic (cross-sectional correlation). `_lib.py` is the only file that
imports the portfolio library; `_common.py` and `_panel.py` are shared plumbing.
"""
from __future__ import annotations

from hokma.quant.portfolio import (  # noqa: F401
    alpha, blend, container, factors, ic, optimize, preselect, priors, uncertainty, weights,
)
