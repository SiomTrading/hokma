"""The only file of the options module that imports quantitative or data libraries (SPEC-019, rule 3).

Every other file reaches those libraries through this one. A scan test fails if any other file imports them.
"""

from financetoolkit.options import options_model as ft_options  # noqa: E402,F401
from financetoolkit.options.greeks import greeks_model as ft_greeks  # noqa: E402,F401
from financetoolkit.options.pricing import binomial_trees_model as ft_tree  # noqa: E402,F401
from financetoolkit.options.pricing import black_scholes_model as ft_bs  # noqa: E402,F401
from financetoolkit.options.pricing import exotics_model as ft_exotics  # noqa: E402,F401
from financetoolkit.options.volatility import risk_neutral_density_model as ft_rnd  # noqa: E402,F401
from financetoolkit.options.volatility import svi_model as ft_svi  # noqa: E402,F401
from scipy.optimize import brentq  # noqa: E402,F401


def refine_svi(log_moneyness, total_variance, start: dict) -> dict:
    """Polish a raw SVI fit with a scaled objective and tight tolerances (the library stops early on small variances).

    Returns the better of the start and the polished parameters by squared error.
    """
    import numpy as np
    from scipy.optimize import minimize

    k = np.asarray(log_moneyness, dtype=float)
    w = np.asarray(total_variance, dtype=float)
    scale = float(np.mean(w ** 2)) or 1.0
    keys = ("a", "b", "rho", "m", "sigma")

    def model(x):
        a, b, rho, m, sigma = x
        return a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + sigma ** 2))

    def objective(x):
        return float(np.sum((model(x) - w) ** 2) / scale)

    x0 = np.array([start[key] for key in keys], dtype=float)
    bounds = [(None, None), (1e-8, None), (-0.999, 0.999), (None, None), (1e-6, None)]
    res = minimize(objective, x0, method="L-BFGS-B", bounds=bounds, options={"ftol": 1e-16, "gtol": 1e-12, "maxiter": 5000, "maxfun": 50000})
    best = res.x if res.fun < objective(x0) else x0
    return {key: float(val) for key, val in zip(keys, best)}
