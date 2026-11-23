"""Resampling is bounded: num_trials has a cap and num_trials x length has a budget."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant import limits
from hokma.quant.errors import BadInput
from hokma.quant.metrics import returns as returns_module


def test_num_trials_is_capped_and_budgeted():
    series = pd.Series(np.random.default_rng(3).normal(0, 0.01, 300), index=pd.bdate_range("2022-01-03", periods=300))
    assert limits.PARAM_CAPS["num_trials"] == 100_000
    with pytest.raises(BadInput):
        returns_module.resample_returns(series, num_trials=100_001)
    big = pd.Series(np.random.default_rng(4).normal(0, 0.01, 600_000))
    with pytest.raises(BadInput):
        returns_module.resample_returns(big, num_trials=100_000)
    assert returns_module.resample_returns(series, num_trials=50).value is not None
