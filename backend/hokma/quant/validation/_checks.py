"""Input checks shared by the validation functions."""
from __future__ import annotations

from typing import Any

import pandas as pd

from hokma.quant import helpers


def clean_returns(values: Any, *, min_obs: int = 3, name: str = "returns") -> tuple[pd.Series, tuple[str, ...], bool]:
    """Clean series, warnings and a degenerate flag (too short, or constant to the last bit)."""
    series, warnings = helpers.validate_returns(values, min_obs=min_obs, name=name)
    degenerate = helpers.is_degenerate(series, min_obs=min_obs)
    if not degenerate and float(series.max() - series.min()) == 0.0:
        degenerate = True
        warnings = warnings + ("constant series: dispersion is zero",)
    return series, warnings, degenerate
