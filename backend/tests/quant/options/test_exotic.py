"""Exotic options in closed form. Rows FT-024 and FT-037."""
import itertools
import math

import pytest

from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import _lib

from .conftest import assert_empty, call, piece_ids

BASE = {"spot": 100.0, "strike": 100.0, "rate": 0.05, "volatility": 0.30, "time_to_expiration": 0.75, "dividend_yield": 0.01}


def test_binary_FT_024(atm):
    cash_call = call("options.binary", **BASE, cash_payout=10.0).value
    cash_put = call("options.binary", **BASE, kind="put", cash_payout=10.0).value
    # hand value: a cash-or-nothing call plus put always pays the cash for sure, so they add up to its present value
    assert cash_call + cash_put == pytest.approx(10.0 * math.exp(-0.05 * 0.75), abs=1e-12)
    asset_call = call("options.binary", **BASE, binary_type="asset_or_nothing").value
    asset_put = call("options.binary", **BASE, kind="put", binary_type="asset_or_nothing").value
    assert asset_call + asset_put == pytest.approx(100.0 * math.exp(-0.01 * 0.75), abs=1e-12)
    d2 = (math.log(1.0) + (0.05 - 0.01 - 0.045) * 0.75) / (0.30 * math.sqrt(0.75))
    assert cash_call == pytest.approx(10.0 * math.exp(-0.05 * 0.75) * 0.5 * math.erfc(-d2 / math.sqrt(2)), abs=1e-12)
    origin = _lib.ft_bs.get_binary_option(100, 100, 0.05, 0.30, 0.75, 0.01, False, "cash-or-nothing", 10.0)
    assert cash_call == pytest.approx(origin, abs=1e-8)
    origin = _lib.ft_bs.get_binary_option(100, 100, 0.05, 0.30, 0.75, 0.01, True, "asset-or-nothing", 1.0)
    assert asset_put == pytest.approx(origin, abs=1e-8)
    args = {k: atm[k] for k in ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield")}
    assert 0.0 < call("options.binary", **args).value < 1.0
    assert_empty(call("options.binary", **{**BASE, "volatility": float("nan")}))
    assert_empty(call("options.binary", **BASE, binary_type="double"), "binary_type")
    assert_empty(call("options.binary", **BASE, cash_payout=-1.0))
    assert_empty(call("options.binary", **{**BASE, "time_to_expiration": 0.0}))
    with pytest.raises(BadInput):
        call("options.binary", **{**BASE, "spot": "100"})
    assert "FT-024" in piece_ids("options.binary")


def test_barrier_FT_037(atm):
    vanilla = c.bs_price(100, 100, 0.05, 0.30, 0.75, 0.01, False)
    # hand value: a knock-in plus the knock-out with the same barrier is the plain option, for every barrier side
    for direction, barrier in (("down", 90.0), ("up", 115.0)):
        knock_in = call("options.barrier_asian", exotic="barrier", **BASE, barrier=barrier, barrier_direction=direction, knock_type="in").value
        knock_out = call("options.barrier_asian", exotic="barrier", **BASE, barrier=barrier, barrier_direction=direction, knock_type="out").value
        assert knock_in + knock_out == pytest.approx(vanilla, abs=1e-10)
        assert 0.0 < knock_in < vanilla and 0.0 < knock_out < vanilla
    # library parity over every combination, with a rebate
    for put, direction, knock, (strike, barrier) in itertools.product((False, True), ("down", "up"), ("in", "out"), ((100.0, 90.0), (100.0, 112.0), (95.0, 90.0), (105.0, 112.0))):
        mine = call("options.barrier_asian", exotic="barrier", **{**BASE, "strike": strike}, kind="put" if put else "call", barrier=barrier,
                    barrier_direction=direction, knock_type=knock, rebate=1.5).value
        origin = _lib.ft_exotics.get_barrier_option(100, strike, barrier, 0.05, 0.30, 0.75, 0.01, put, direction, knock, 1.5)
        assert mine == pytest.approx(origin, abs=1e-8), (put, direction, knock, strike, barrier)
    # a barrier already crossed (spot above an up barrier): the knock-out is worth its rebate, the knock-in the plain option
    out = call("options.barrier_asian", exotic="barrier", **BASE, barrier=95.0, barrier_direction="up", knock_type="out", rebate=2.0).value
    assert out == pytest.approx(2.0, abs=1e-12)
    crossed_in = call("options.barrier_asian", exotic="barrier", **BASE, barrier=95.0, barrier_direction="up", knock_type="in").value
    assert crossed_in == pytest.approx(c.bs_price(100, 100, 0.05, 0.30, 0.75, 0.01, False), abs=1e-12)
    args = {k: atm[k] for k in ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield")}
    hit = call("options.barrier_asian", exotic="barrier", **args, barrier=args["spot"] * 0.9, barrier_direction="down", knock_type="out").value
    assert 0.0 < hit < call("options.black_scholes", **args).value
    assert_empty(call("options.barrier_asian", exotic="barrier", **BASE), "barrier")
    assert_empty(call("options.barrier_asian", exotic="barrier", **BASE, barrier=-1.0), "barrier")
    assert_empty(call("options.barrier_asian", exotic="barrier", **BASE, barrier=90.0, barrier_direction="left"), "barrier_direction")
    assert_empty(call("options.barrier_asian", exotic="barrier", **BASE, barrier=90.0, rebate=-1.0), "rebate")
    assert_empty(call("options.barrier_asian", exotic="lookback", **BASE), "exotic")
    assert_empty(call("options.barrier_asian", exotic="barrier", **{**BASE, "volatility": float("nan")}, barrier=90.0))
    assert "FT-037" in piece_ids("options.barrier_asian")


def test_asian_FT_037(atm):
    asian_call = call("options.barrier_asian", exotic="asian", **BASE).value
    asian_put = call("options.barrier_asian", exotic="asian", **BASE, kind="put").value
    european = c.bs_price(100, 100, 0.05, 0.30, 0.75, 0.01, False)
    # hand values: averaging lowers the volatility, so the average price option costs less than the plain one
    assert 0.0 < asian_call < european
    assert asian_put < c.bs_price(100, 100, 0.05, 0.30, 0.75, 0.01, True)
    # with the volatility reduced by the square root of 3 and carry b = (r - q - sigma^2 / 6) / 2 the formula is Black-Scholes on those inputs
    va, ba = 0.30 / math.sqrt(3), 0.5 * (0.04 - 0.09 / 6)
    d1 = (math.log(1.0) + (ba + va**2 / 2) * 0.75) / (va * math.sqrt(0.75))
    cdf = lambda x: 0.5 * math.erfc(-x / math.sqrt(2))  # noqa: E731
    manual = 100 * math.exp((ba - 0.05) * 0.75) * cdf(d1) - 100 * math.exp(-0.05 * 0.75) * cdf(d1 - va * math.sqrt(0.75))
    assert asian_call == pytest.approx(manual, abs=1e-12)
    assert asian_call == pytest.approx(_lib.ft_exotics.get_asian_option(100, 100, 0.05, 0.30, 0.75, 0.01, False), abs=1e-8)
    assert asian_put == pytest.approx(_lib.ft_exotics.get_asian_option(100, 100, 0.05, 0.30, 0.75, 0.01, True), abs=1e-8)
    args = {k: atm[k] for k in ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield")}
    assert 0.0 < call("options.barrier_asian", exotic="asian", **args).value < call("options.black_scholes", **args).value
    assert_empty(call("options.barrier_asian", exotic="asian", **{**BASE, "spot": float("nan")}))
    assert_empty(call("options.barrier_asian", exotic="asian", **{**BASE, "volatility": 0.0}))
    assert_empty(call("options.barrier_asian", exotic="asian", **BASE, kind="x"), "kind")
