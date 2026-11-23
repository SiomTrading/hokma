"""The only file of the stats module that imports quantitative or data libraries (SPEC-019, rule 3).

Every other file reaches those libraries through this one. A scan test fails if any other file imports them.

The econometrics package of the toolkit library needs statsmodels and linearmodels, which are not dependencies of
Hokma, so the unit root, cointegration, regression, panel and time series models are written in the module on top of
numpy and scipy. What the toolkit offers without them (range volatility estimators, HAR-RV, GJR-GARCH, EGARCH and the
structure function Hurst exponent) is reused from here.
"""

import numpy as np  # noqa: E402,F401
from financetoolkit.risk import risk_model as ft_risk_model  # noqa: E402,F401
from financetoolkit.risk.volatility import garch_model as ft_garch  # noqa: E402,F401
from financetoolkit.risk.volatility import realized_volatility_model as ft_realized  # noqa: E402,F401
from scipy import linalg as sp_linalg  # noqa: E402,F401
from scipy import optimize as sp_optimize  # noqa: E402,F401
from scipy import signal as sp_signal  # noqa: E402,F401
from scipy import sparse as sp_sparse  # noqa: E402,F401
from scipy import stats as sp_stats  # noqa: E402,F401
from sklearn.linear_model import LinearRegression as SkLinearRegression  # noqa: E402,F401
from sklearn.linear_model import LogisticRegression as SkLogisticRegression  # noqa: E402,F401
from sklearn.linear_model import QuantileRegressor as SkQuantileRegressor  # noqa: E402,F401
