"""The only file of the risk module that imports quantitative or data libraries (SPEC-017, rule 4).

Every other file reaches those libraries through this one. A scan test fails if any other file imports them.
"""

import numpy as np  # noqa: E402,F401
from financetoolkit.risk import risk_model as ft_risk_model  # noqa: E402,F401
from financetoolkit.risk.dependence import copula_model as ft_copula  # noqa: E402,F401
from financetoolkit.risk.dependence import covar_model as ft_covar  # noqa: E402,F401
from financetoolkit.risk.liquidity import market_liquidity_model as ft_liquidity  # noqa: E402,F401
from financetoolkit.risk.value_at_risk import backtesting_model as ft_backtest  # noqa: E402,F401
from financetoolkit.risk.value_at_risk import cvar_model as ft_cvar  # noqa: E402,F401
from financetoolkit.risk.value_at_risk import evar_model as ft_evar  # noqa: E402,F401
from financetoolkit.risk.value_at_risk import var_model as ft_var  # noqa: E402,F401
from financetoolkit.risk.volatility import garch_model as ft_garch  # noqa: E402,F401
from financetoolkit.risk.volatility import realized_volatility_model as ft_realized  # noqa: E402,F401
from skfolio import distribution as sk_dist  # noqa: E402,F401
from skfolio import measures as sk_measures  # noqa: E402,F401
from skfolio import prior as sk_prior  # noqa: E402,F401
from skfolio.prior._model._family_constraint_basis import compute_family_constraint_basis  # noqa: E402,F401

# Fitted univariate families and bivariate copula families that the module exposes, by public name.
UNIVARIATE_FAMILIES = {
    "gaussian": sk_dist.Gaussian,
    "student_t": sk_dist.StudentT,
    "johnson_su": sk_dist.JohnsonSU,
    "normal_inverse_gaussian": sk_dist.NormalInverseGaussian,
}
COPULA_FAMILIES = {
    "gaussian": sk_dist.GaussianCopula,
    "student_t": sk_dist.StudentTCopula,
    "clayton": sk_dist.ClaytonCopula,
    "gumbel": sk_dist.GumbelCopula,
    "joe": sk_dist.JoeCopula,
    "independent": sk_dist.IndependentCopula,
}
