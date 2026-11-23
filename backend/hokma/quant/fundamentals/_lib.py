"""The only file of the fundamentals module that imports quantitative or data libraries (SPEC-019, rule 3).

Every other file reaches those libraries through this one. A scan test fails if any other file imports them.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np  # noqa: F401
import pandas as pd

from edgar.entity.models import FinancialFact  # noqa: E402,F401
from financetoolkit.models.distress import altman_model as ft_altman  # noqa: E402,F401
from financetoolkit.models.distress import fulmer_model as ft_fulmer  # noqa: E402,F401
from financetoolkit.models.distress import grover_model as ft_grover  # noqa: E402,F401
from financetoolkit.models.distress import ohlson_model as ft_ohlson  # noqa: E402,F401
from financetoolkit.models.distress import springate_model as ft_springate  # noqa: E402,F401
from financetoolkit.models.distress import zmijewski_model as ft_zmijewski  # noqa: E402,F401
from financetoolkit.models.profitability import dupont_model as ft_dupont  # noqa: E402,F401
from financetoolkit.models.profitability import growth_model as ft_growth  # noqa: E402,F401
from financetoolkit.models.quality import beneish_model as ft_beneish  # noqa: E402,F401
from financetoolkit.models.quality import piotroski_model as ft_piotroski  # noqa: E402,F401
from financetoolkit.models.valuation import enterprise_model as ft_enterprise  # noqa: E402,F401
from financetoolkit.models.valuation import eva_model as ft_eva  # noqa: E402,F401
from financetoolkit.models.valuation import intrinsic_model as ft_intrinsic  # noqa: E402,F401
from financetoolkit.models.valuation import wacc_model as ft_wacc  # noqa: E402,F401
from financetoolkit.performance import performance_model as ft_performance  # noqa: E402,F401
from financetoolkit.ratios import efficiency_model as ft_efficiency  # noqa: E402,F401
from financetoolkit.ratios import liquidity_model as ft_liquidity  # noqa: E402,F401
from financetoolkit.ratios import profitability_model as ft_profitability  # noqa: E402,F401
from financetoolkit.ratios import solvency_model as ft_solvency  # noqa: E402,F401
from financetoolkit.ratios import valuation_model as ft_valuation  # noqa: E402,F401
from skfolio import descriptor as sk_descriptor  # noqa: E402,F401
from skfolio import preprocessing as sk_preprocessing  # noqa: E402,F401
from skfolio.containers import AssetPanel as SkAssetPanel  # noqa: E402,F401

from hokma.data._lib import SecTtmError, sec_ttm  # noqa: E402,F401  (the TTM of SVC-data is reused, never copied)

ALTMAN_MODELS = tuple(ft_altman.ALTMAN_COEFFICIENTS)
# Two intrinsic value formulas of the library under the neutral names the module uses.
growth_multiple_value = ft_intrinsic.get_graham_intrinsic_value
defensive_number = ft_intrinsic.get_graham_number


def sk_panel(fields: dict[str, pd.DataFrame], active: pd.DataFrame | None = None) -> Any:
    """A library asset panel from wide frames (rows are dates, columns are assets, all with the same labels)."""
    first = next(iter(fields.values()))
    arrays = {name: frame.to_numpy(dtype=float) for name, frame in fields.items()}
    mask = None if active is None else active.to_numpy(dtype=bool)
    return SkAssetPanel(fields=arrays, observations=np.asarray(first.index), asset_names=np.asarray([str(c) for c in first.columns]),
                        active_mask=mask)


def fact(concept: str, unit: str, value: float, period_start: date | None, period_end: date, filed: date,
         fiscal_year: int, fiscal_period: str, form: str) -> Any:
    """One reported figure in the form the TTM routine of SVC-data reads."""
    duration = period_start is not None
    return FinancialFact(concept=concept, taxonomy="us-gaap", label=concept, value=value, numeric_value=value, unit=unit,
                         period_start=period_start, period_end=period_end, period_type="duration" if duration else "instant",
                         fiscal_year=fiscal_year, fiscal_period=fiscal_period, filing_date=filed, form_type=form)
