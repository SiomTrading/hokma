"""Example alphas (SPEC-020: L-52, FM-20): each gives a valid Signal, with values computed by hand."""
import math
from datetime import datetime, timezone

import numpy as np
import pytest

from hokma.desks.core.signal import AlphaModel, Signal, SignalContext
from hokma.factory.alphas import BUILTIN_ALPHAS, EmaCross, HistoricalReturns, Macd, PairsCorrelation, RsiReversion, Tsmom
from hokma.factory.alphas.tsmom import tsmom_weight

AS_OF = datetime(2025, 3, 3, 21, 0, tzinfo=timezone.utc)


def ctx(**facts) -> SignalContext:
    return SignalContext("alpha.test", 7, "SWC", "SPY", AS_OF, "days", "h" * 8, facts={k: np.asarray(v, dtype=float) for k, v in facts.items()})


def test_every_builtin_is_an_alpha_model_with_a_schema():
    assert set(BUILTIN_ALPHAS) == {"ema_cross", "rsi_reversion", "macd", "historical_returns", "pairs_correlation", "tsmom"}
    for klass in BUILTIN_ALPHAS.values():
        alpha = klass(**klass.SCHEMA.defaults())
        assert isinstance(alpha, AlphaModel) and alpha.lookback >= 1 and set(alpha.FACTS)


def test_ema_cross():
    alpha = EmaCross()
    signal = alpha.signal(ctx(ema_fast=[100.5], ema_slow=[100.0]))
    assert isinstance(signal, Signal) and signal.value == pytest.approx(0.5)             # gap 0.005 over scale 0.01
    assert alpha.signal(ctx(ema_fast=[102.0], ema_slow=[100.0])).value == 1.0            # clipped
    assert alpha.signal(ctx(ema_fast=[99.0], ema_slow=[100.0])).value == pytest.approx(-1.0)
    assert alpha.signal(ctx(ema_fast=[100.0], ema_slow=[100.0])).value == 0.0
    assert alpha.signal(ctx(ema_fast=[100.0])).abstained
    assert [r.name for r in alpha.requests()] == ["ema_fast", "ema_slow"] and alpha.requests()[1].warmup == 26
    with pytest.raises(ValueError):
        EmaCross(fast=30, slow=20)


def test_rsi_reversion():
    alpha = RsiReversion()
    assert alpha.signal(ctx(rsi=[20.0])).value == pytest.approx(1 / 3)                  # (30 - 20) / 30
    assert alpha.signal(ctx(rsi=[85.0])).value == pytest.approx(-0.5)                   # -(85 - 70) / 30
    assert alpha.signal(ctx(rsi=[50.0])).value == 0.0
    assert alpha.signal(ctx(rsi=[0.0])).value == 1.0
    assert alpha.signal(ctx(rsi=[float("nan")])).abstained


def test_macd():
    alpha = Macd()
    signal = alpha.signal(ctx(macd_line=[0.2], macd_signal=[0.1], close=[100.0]))
    assert signal.value == pytest.approx(0.5)                                            # 0.001 over scale 0.002
    assert alpha.signal(ctx(macd_line=[-1.0], macd_signal=[0.0], close=[100.0])).value == -1.0
    assert alpha.signal(ctx(macd_line=[0.2], macd_signal=[0.1])).abstained
    assert alpha.requests()[1].warmup == 35


def test_historical_returns():
    alpha = HistoricalReturns(periods=4, cap=0.2)
    assert alpha.lookback == 5
    assert alpha.signal(ctx(close=[100, 102, 104, 106, 110])).value == pytest.approx(0.5)     # 10 percent over a 20 percent cap
    assert alpha.signal(ctx(close=[100, 99, 98, 97, 70])).value == -1.0
    assert alpha.signal(ctx(close=[100, 102, 104, 106])).abstained                       # one close short


def test_pairs_correlation_by_hand():
    # log prices: a = [0, 2, 0, 2, 2] and b = [0, 2, 0, 2, 1]. beta = cov / var = 4 / 4 = 1, spread = [0, 0, 0, 0, 1].
    # mean 0.2, sum of squares 4 * 0.04 + 0.64 = 0.8, sd = sqrt(0.8 / 4), z = 0.8 / sd = sqrt(1.6). value = -z / (2 * 2).
    a, b = np.exp([0, 2, 0, 2, 2]), np.exp([0, 2, 0, 2, 1])
    alpha = PairsCorrelation(window=5, min_corr=0.7, entry_z=2.0)
    signal = alpha.signal(ctx(close=a, pair_close=b))
    assert signal.value == pytest.approx(-math.sqrt(0.2), rel=1e-9)
    assert {c.name for c in signal.criteria} == {"spread_z", "change_correlation"}
    # a and b with no correlation of their changes: the view is flat
    flat = alpha.signal(ctx(close=np.exp([0, 0, 1, 1, 0]), pair_close=np.exp([0, 1, 0, 1, 0])))
    assert flat.value == 0.0 and not flat.abstained
    assert alpha.signal(ctx(close=a, pair_close=np.exp([1, 1, 1, 1, 1]))).abstained     # the partner did not move
    assert alpha.signal(ctx(close=a)).abstained


def test_tsmom_weight_by_hand():                                                         # FM-20
    assert tsmom_weight(0.30, 0.20, 0.10) == pytest.approx(0.5)                          # 0.10 / 0.20
    assert tsmom_weight(-0.30, 0.20, 0.10) == pytest.approx(-0.5)
    assert tsmom_weight(0.05, 0.05, 0.10) == 1.0                                         # 2.0 clipped
    assert tsmom_weight(0.0, 0.20, 0.10) == 0.0
    with pytest.raises(ValueError):
        tsmom_weight(0.1, 0.0, 0.10)


def test_tsmom_signal_by_hand():
    # closes 100, 110, 99, 108.9, 98.01: returns +10%, -10%, +10%, -10%. Sample sd = 0.1 * sqrt(4 / 3), annual sigma = sd * sqrt(252) = 0.1 * sqrt(336).
    # The return over 4 bars is 98.01 / 100 - 1 < 0, so the sign is -1 and the weight is -0.10 / sigma = -1 / sqrt(336).
    closes = [100.0, 110.0, 99.0, 108.9, 98.01]
    alpha = Tsmom(periods=4, vol_periods=4)
    signal = alpha.signal(ctx(close=closes))
    assert signal.value == pytest.approx(-1 / math.sqrt(336), rel=1e-6) and -1.0 <= signal.value <= 1.0
    assert {c.name: round(c.value, 4) for c in signal.criteria} == {"trailing_return": -0.0199, "sigma_annual": 1.8330}
    up = alpha.signal(ctx(close=[100.0, 110.0, 99.0, 108.9, 120.0]))
    assert up.value > 0
    assert alpha.signal(ctx(close=closes[1:])).abstained
    assert alpha.signal(ctx(close=[100.0] * 5)).abstained                                 # no volatility
    assert Tsmom().lookback == 253
