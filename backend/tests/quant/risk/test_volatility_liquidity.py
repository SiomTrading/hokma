"""Volatility and liquidity. Rows FT-056, FT-060 and FT-065 (Yang-Zhang volatility is listed under CC-06)."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.risk import _lib

from .conftest import call, piece_ids

TOL = 1e-9


def test_ewma_volatility_FT_056(ret):
    res = call("risk.ewma_volatility", returns=ret)
    origin = _lib.ft_risk_model.get_ewma_volatility(ret, lambda_=0.94).dropna()
    assert res.value == pytest.approx(origin.iloc[-1], abs=TOL)
    # hand value: the first volatility is |r0| and each next one blends the previous variance with the last squared return
    x = ret.to_numpy()
    sigma = abs(x[0])
    for t in range(1, len(x)):
        sigma = float(np.sqrt(0.94 * sigma**2 + 0.06 * x[t - 1] ** 2)) if t > 1 else abs(x[0])
    assert res.value == pytest.approx(sigma, abs=1e-9)
    short = call("risk.ewma_volatility", returns=ret, lambda_=0.80).value
    long = call("risk.ewma_volatility", returns=ret, lambda_=0.99).value
    assert short != long
    assert call("risk.ewma_volatility", returns=ret, lambda_=1.0).value is None
    with pytest.raises(BadInput):
        call("risk.ewma_volatility", returns=ret, lambda_="0.94")
    assert "FT-056" in piece_ids("risk.ewma_volatility")


def test_garch_11_FT_060(ret):
    res = call("risk.garch", returns=ret, horizon=3)
    omega, alpha, beta = _lib.ft_garch.get_garch_weights(ret.to_numpy())
    assert res.value["omega"] == pytest.approx(omega, abs=TOL)
    assert res.value["alpha"] == pytest.approx(alpha, abs=TOL)
    assert res.value["beta"] == pytest.approx(beta, abs=TOL)
    origin = _lib.ft_garch.get_garch_forecast(ret.to_numpy(), [omega, alpha, beta], time_steps=3)
    assert res.value["forecast_variance"] == pytest.approx(list(origin), abs=TOL)
    assert res.value["forecast_volatility"] == pytest.approx(list(np.sqrt(origin)), abs=TOL)
    # hand value: the long run variance is omega / (1 - alpha - beta) and a long horizon converges to it
    assert res.value["persistence"] == pytest.approx(alpha + beta, abs=TOL)
    assert res.value["long_run_variance"] == pytest.approx(omega / (1 - alpha - beta), abs=TOL)
    far = call("risk.garch", returns=ret, horizon=60).value
    assert far["forecast_variance"][-1] == pytest.approx(far["long_run_variance"], rel=1e-6)
    assert omega > 0 and alpha >= 0 and beta >= 0 and alpha + beta < 1
    assert call("risk.garch", returns=ret.iloc[:50]).value is None
    assert call("risk.garch", returns=ret, horizon=0).value is None
    with pytest.raises(BadInput):
        call("risk.garch", returns=ret, horizon=2.5)
    assert "FT-060" in piece_ids("risk.garch")


def test_amihud_illiquidity_FT_065():
    r = pd.Series([0.01, -0.02, 0.03, 0.01, -0.01], index=pd.bdate_range("2021-01-04", periods=5))
    volume = pd.Series([1e6, 2e6, 1e6, 4e6, 1e6], index=r.index)
    res = call("risk.amihud_illiquidity", returns=r, dollar_volume=volume)
    origin = _lib.ft_liquidity.get_amihud_illiquidity(r, volume, scale=1_000_000)
    assert res.value == pytest.approx(float(origin), abs=TOL)
    # hand value: mean(|r| / dollar volume) scaled by one million = mean(0.01, 0.01, 0.03, 0.0025, 0.01)
    assert res.value == pytest.approx(0.0125, abs=TOL)
    assert call("risk.amihud_illiquidity", returns=r, dollar_volume=volume, scale=1.0).value == pytest.approx(0.0125e-6, abs=1e-15)
    zero_volume = volume.copy()
    zero_volume.iloc[1] = 0.0
    dropped = call("risk.amihud_illiquidity", returns=r, dollar_volume=zero_volume)
    assert dropped.value == pytest.approx(np.mean([0.01, 0.03, 0.0025, 0.01]), abs=TOL) and dropped.warnings
    assert call("risk.amihud_illiquidity", returns=r.iloc[:1], dollar_volume=volume.iloc[:1]).value is None
    with pytest.raises(BadInput):
        call("risk.amihud_illiquidity", returns=r, dollar_volume=volume.iloc[:3])
    assert "FT-065" in piece_ids("risk.amihud_illiquidity")


def test_roll_spread_FT_065():
    bounce = pd.Series([100.0, 101.0] * 50 + [100.0], index=pd.bdate_range("2021-01-04", periods=101))
    res = call("risk.roll_spread", close_prices=bounce)
    origin = _lib.ft_liquidity.get_roll_spread(bounce)
    assert res.value["roll_spread"] == pytest.approx(float(origin["Roll Spread"]), abs=TOL)
    assert res.value["autocovariance"] == pytest.approx(float(origin["Autocovariance"]), abs=TOL)
    # hand value: price changes alternate +1 and -1, so the serial covariance is close to -1 and the spread to 2 * sqrt(1)
    assert res.value["roll_spread"] == pytest.approx(2.0, abs=0.05)
    assert res.value["valid_estimate"] is True
    # price changes that persist (positive serial covariance) give no valid spread
    steps = np.repeat([1.0, 3.0] * 6, 5)
    trend = pd.Series(100 + np.cumsum(steps), index=pd.bdate_range("2021-01-04", periods=60))
    assert call("risk.roll_spread", close_prices=trend).value["valid_estimate"] is False
    assert call("risk.roll_spread", close_prices=bounce * -1).value is None
    assert "FT-065" in piece_ids("risk.roll_spread")


def _bars(seed=1, n=300):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    close = pd.Series(100 * np.exp(rng.normal(0, 0.01, n).cumsum()), index=idx)
    opened = close.shift(1).fillna(close.iloc[0]) * 1.001
    high = np.maximum(opened, close) * 1.005
    low = np.minimum(opened, close) * 0.995
    return opened, high, low, close


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.filterwarnings("ignore:.*dayofweek.*")
def test_yang_zhang_volatility_CC_06():
    o, h, l, c = _bars()
    res = call("risk.yang_zhang_volatility", open_=o, high=h, low=l, close=c, period="monthly")

    def daily(s):
        out = s.copy()
        out.index = out.index.to_period("D")
        return out

    origin = _lib.ft_realized.get_yang_zhang_volatility(daily(o), daily(h), daily(l), daily(c), "monthly").dropna()
    assert res.value["latest"] == pytest.approx(origin.iloc[-1], abs=TOL)
    assert res.value["values"] == pytest.approx(list(origin.to_numpy()), abs=TOL)
    # hand values: prices in other units give the same volatility, and a wider daily range gives a higher one
    scaled = call("risk.yang_zhang_volatility", open_=o * 7, high=h * 7, low=l * 7, close=c * 7)
    assert scaled.value["latest"] == pytest.approx(res.value["latest"], abs=1e-9)
    wide = call("risk.yang_zhang_volatility", open_=o, high=h * 1.02, low=l * 0.98, close=c)
    assert wide.value["latest"] > res.value["latest"] > 0
    assert "CC-06" in piece_ids("risk.yang_zhang_volatility")


def test_yang_zhang_bad_input_CC_06():
    o, h, l, c = _bars()
    assert call("risk.yang_zhang_volatility", open_=o.iloc[:5], high=h.iloc[:5], low=l.iloc[:5], close=c.iloc[:5]).value is None
    assert call("risk.yang_zhang_volatility", open_=o, high=l, low=h, close=c).value is None          # high below low
    assert call("risk.yang_zhang_volatility", open_=o * -1, high=h, low=l, close=c).value is None
    nan = c.copy()
    nan[:] = np.nan
    assert call("risk.yang_zhang_volatility", open_=o, high=h, low=l, close=nan).value is None
    with pytest.raises(BadInput):
        call("risk.yang_zhang_volatility", open_=o, high=h, low=l, close=c.iloc[:10])
    with pytest.raises(BadInput):
        call("risk.yang_zhang_volatility", open_=o.reset_index(drop=True), high=h.reset_index(drop=True), low=l.reset_index(drop=True),
             close=c.reset_index(drop=True))
