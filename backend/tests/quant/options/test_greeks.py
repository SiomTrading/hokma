"""Implied volatility and greeks. Rows FT-021, FT-026 to FT-032, FM-11 and FM-12."""
import math

import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import _lib

from .conftest import assert_empty, call, piece_ids

CLASSIC = {"spot": 100.0, "strike": 100.0, "rate": 0.05, "volatility": 0.20, "time_to_expiration": 1.0, "dividend_yield": 0.0}
OFFSET = {"spot": 100.0, "strike": 105.0, "rate": 0.04, "volatility": 0.30, "time_to_expiration": 0.5, "dividend_yield": 0.012}


def price(put=False, **over):
    p = {**OFFSET, **over}
    return c.bs_price(p["spot"], p["strike"], p["rate"], p["volatility"], p["time_to_expiration"], p["dividend_yield"], put)


def central(f, x, h):
    return (f(x + h) - f(x - h)) / (2.0 * h)


def test_implied_volatility_FT_021(chain, atm):
    # round trip: the price of a known volatility gives that volatility back
    for sigma in (0.08, 0.2, 0.55, 1.4):
        px = c.bs_price(100, 100, 0.05, sigma, 1.0, 0.0, False)
        assert call("options.implied_volatility", market_price=px, **{k: v for k, v in CLASSIC.items() if k != "volatility"}).value == pytest.approx(sigma, abs=1e-9)
    # hand value: the published price 10.450583572185565 belongs to 20 percent
    res = call("options.implied_volatility", market_price=10.450583572185565, spot=100.0, strike=100.0, rate=0.05, time_to_expiration=1.0)
    assert res.value == pytest.approx(0.20, abs=1e-9)
    origin = _lib.ft_bs.get_implied_volatility(7.5, 100, 100, 0.05, 1.0, 0.0, False)
    assert call("options.implied_volatility", market_price=7.5, spot=100.0, strike=100.0, rate=0.05, time_to_expiration=1.0).value == pytest.approx(origin, abs=1e-8)
    # the recorded chain: every quote inverts to the volatility stored with it
    checked = 0
    for row in chain["contracts"]:
        if row["expiration"] != "2027-01-15":
            continue
        t = c.years_between(chain["as_of"], row["expiration"])
        out = call("options.implied_volatility", market_price=row["mid"], spot=chain["underlying_price"], strike=row["strike"], rate=chain["rate"],
                   time_to_expiration=t, dividend_yield=chain["dividend_yield"], kind=row["type"])
        assert out.value == pytest.approx(row["iv"], abs=1e-6)
        checked += 1
    assert checked >= 40
    # American model: round trip through the approximation
    american = call("options.bjerksund_stensland", **{**CLASSIC, "volatility": 0.31}, kind="put").value["american"]
    back = call("options.implied_volatility", market_price=american, spot=100.0, strike=100.0, rate=0.05, time_to_expiration=1.0, kind="put", model="american")
    assert back.value == pytest.approx(0.31, abs=1e-8)
    # outside the arbitrage bounds: None with the bounds in the warning
    below = call("options.implied_volatility", market_price=4.0, spot=110.0, strike=100.0, rate=0.05, time_to_expiration=1.0)
    assert_empty(below, "no-arbitrage bounds")
    assert_empty(call("options.implied_volatility", market_price=120.0, spot=100.0, strike=100.0, rate=0.05, time_to_expiration=1.0), "no-arbitrage")
    assert_empty(call("options.implied_volatility", market_price=0.0, spot=100.0, strike=100.0, rate=0.05, time_to_expiration=1.0), "positive")
    assert_empty(call("options.implied_volatility", market_price=float("nan"), spot=100.0, strike=100.0, rate=0.05, time_to_expiration=1.0))
    assert_empty(call("options.implied_volatility", market_price=5.0, spot=100.0, strike=100.0, rate=0.05, time_to_expiration=0.0))
    assert_empty(call("options.implied_volatility", market_price=5.0, spot=100.0, strike=100.0, rate=0.05, time_to_expiration=1.0, model="local"), "model")
    with pytest.raises(BadInput):
        call("options.implied_volatility", market_price="5", spot=100.0, strike=100.0, rate=0.05, time_to_expiration=1.0)
    assert "FT-021" in piece_ids("options.implied_volatility")


def test_first_order_FT_026(atm):
    res = call("options.first_order", **CLASSIC).value
    cdf = lambda x: 0.5 * math.erfc(-x / math.sqrt(2))  # noqa: E731
    # hand values from the closed forms at d1 = 0.35, d2 = 0.15
    assert res["delta"] == pytest.approx(0.6368306511756191, abs=1e-12)
    assert call("options.first_order", **CLASSIC, kind="put").value["delta"] == pytest.approx(0.6368306511756191 - 1.0, abs=1e-12)
    assert res["vega"] == pytest.approx(0.3752403469169379, abs=1e-12)                       # per volatility point
    assert res["rho"] == pytest.approx(100 * math.exp(-0.05) * cdf(0.15), abs=1e-10)
    assert res["dual_delta"] == pytest.approx(-math.exp(-0.05) * cdf(0.15), abs=1e-12)
    assert res["epsilon"] == pytest.approx(-100 * cdf(0.35), abs=1e-10)
    assert res["lambda"] == pytest.approx(0.6368306511756191 * 100 / 10.450583572185565, abs=1e-10)
    # independent check: bump the price and read the slope
    got = call("options.first_order", **OFFSET).value
    put_got = call("options.first_order", **OFFSET, kind="put").value
    assert got["delta"] == pytest.approx(central(lambda s: price(spot=s), 100.0, 1e-3), abs=1e-7)
    assert got["vega"] == pytest.approx(central(lambda v: price(volatility=v), 0.30, 1e-5) / 100.0, abs=1e-7)
    assert got["theta"] == pytest.approx(-central(lambda t: price(time_to_expiration=t), 0.5, 1e-5) / 365.0, abs=1e-7)
    assert put_got["theta"] == pytest.approx(-central(lambda t: price(True, time_to_expiration=t), 0.5, 1e-5) / 365.0, abs=1e-7)
    assert got["rho"] == pytest.approx(central(lambda r: price(rate=r), 0.04, 1e-6), abs=1e-5)
    assert got["epsilon"] == pytest.approx(central(lambda q: price(dividend_yield=q), 0.012, 1e-6), abs=1e-5)
    assert got["dual_delta"] == pytest.approx(central(lambda k: price(strike=k), 105.0, 1e-4), abs=1e-7)
    # library parity
    g = _lib.ft_greeks
    assert got["delta"] == pytest.approx(g.get_delta(100, 105, 0.5, 0.04, 0.3, 0.012, False), abs=1e-8)
    assert put_got["rho"] == pytest.approx(g.get_rho(100, 105, 0.5, 0.04, 0.3, 0.012, True), abs=1e-8)
    assert got["vega"] == pytest.approx(g.get_vega(100, 105, 0.5, 0.04, 0.3, 0.012), abs=1e-8)
    # the recorded chain: the provider's delta of the at-the-money call is the one computed from its volatility (within the rounding of the fixture)
    args = {k: atm[k] for k in ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield")}
    assert call("options.first_order", **args).value["delta"] == pytest.approx(atm["row"]["delta"], abs=1e-5)
    assert_empty(call("options.first_order", **{**CLASSIC, "volatility": 0.0}))
    assert_empty(call("options.first_order", **{**CLASSIC, "spot": float("nan")}))
    assert_empty(call("options.first_order", **CLASSIC, kind="swap"), "kind")
    assert_empty(call("options.first_order", **{**CLASSIC, "time_to_expiration": 0.0}))
    with pytest.raises(BadInput):
        call("options.first_order", **{**CLASSIC, "strike": "100"})
    assert "FT-026" in piece_ids("options.first_order")


def test_gamma_FM_11_FT_027(atm, chain):
    res = call("options.gamma", **CLASSIC)
    # hand value: gamma = pdf(d1) / (S * sigma * sqrt(T)) = 0.3752403469169379 / 20
    assert res.value == pytest.approx(0.018762017345846895, abs=1e-12)
    delta = lambda s: call("options.first_order", **{**OFFSET, "spot": s}).value["delta"]  # noqa: E731
    assert call("options.gamma", **OFFSET).value == pytest.approx(central(delta, 100.0, 1e-3), abs=1e-7)
    assert call("options.gamma", **OFFSET).value == pytest.approx(_lib.ft_greeks.get_gamma(100, 105, 0.5, 0.04, 0.3, 0.012), abs=1e-8)
    args = {k: atm[k] for k in ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield")}
    assert call("options.gamma", **args).value == pytest.approx(atm["row"]["gamma"], abs=1e-6)
    # a single implementation carries both rows, and no other function claims FT-027
    assert set(piece_ids("options.gamma")) == {"FM-11", "FT-027"}
    assert [s.name for s in registry.list("options") if "FT-027" in s.piece_ids] == ["options.gamma"]
    assert [s.name for s in registry.list("options") if "FM-11" in s.piece_ids] == ["options.gamma"]
    assert_empty(call("options.gamma", **{**CLASSIC, "volatility": -0.1}))
    assert_empty(call("options.gamma", **{**CLASSIC, "strike": float("nan")}))
    assert_empty(call("options.gamma", **{**CLASSIC, "time_to_expiration": 0.0}))


def test_dual_gamma_FT_028():
    res = call("options.dual_gamma", **OFFSET)
    # hand value: the second derivative of the price with respect to the strike (central difference of the price)
    h = 0.01
    second = (price(strike=105 + h) - 2 * price() + price(strike=105 - h)) / h**2
    assert res.value == pytest.approx(second, abs=1e-6)
    assert res.value == pytest.approx(_lib.ft_greeks.get_dual_gamma(100, 105, 0.5, 0.04, 0.3, 0.012), abs=1e-8)
    assert_empty(call("options.dual_gamma", **{**OFFSET, "volatility": float("nan")}))
    assert_empty(call("options.dual_gamma", **{**OFFSET, "spot": 0.0}))
    assert "FT-028" in piece_ids("options.dual_gamma")


def test_vanna_FM_12_FT_029(atm):
    res = call("options.vanna", **OFFSET)
    # hand value: vanna is the change of delta per unit of volatility
    delta = lambda v: call("options.first_order", **{**OFFSET, "volatility": v}).value["delta"]  # noqa: E731
    assert res.value == pytest.approx(central(delta, 0.30, 1e-5), abs=1e-6)
    assert res.value == pytest.approx(_lib.ft_greeks.get_vanna(100, 105, 0.5, 0.04, 0.3, 0.012), abs=1e-8)
    assert call("options.vanna", **CLASSIC).value == pytest.approx(-0.3752403469169379 * 0.15 / 0.2, abs=1e-12)   # -pdf(d1) * d2 / sigma
    assert set(piece_ids("options.vanna")) == {"FM-12", "FT-029"}
    assert [s.name for s in registry.list("options") if "FT-029" in s.piece_ids] == ["options.vanna"]
    assert_empty(call("options.vanna", **{**OFFSET, "rate": float("nan")}))
    assert_empty(call("options.vanna", **{**OFFSET, "volatility": 0.0}))


def test_second_order_FT_030():
    res = call("options.second_order", **OFFSET).value
    put = call("options.second_order", **OFFSET, kind="put").value
    delta = lambda t, put=False: call("options.first_order", **{**OFFSET, "time_to_expiration": t}, kind="put" if put else "call").value["delta"]  # noqa: E731
    vega_unit = lambda **o: call("options.first_order", **{**OFFSET, **o}).value["vega"] * 100.0  # noqa: E731
    # hand values: charm is minus the change of delta with the time to expiry; vomma, vera and veta are changes of vega
    assert res["charm"] == pytest.approx(-central(delta, 0.5, 1e-5), abs=1e-6)
    assert put["charm"] == pytest.approx(-central(lambda t: delta(t, True), 0.5, 1e-5), abs=1e-6)
    assert res["vomma"] == pytest.approx(central(lambda v: vega_unit(volatility=v), 0.30, 1e-5), abs=1e-5)
    assert res["vera"] == pytest.approx(central(lambda r: vega_unit(rate=r), 0.04, 1e-6), abs=1e-4)
    assert res["veta"] * 100 * 365 == pytest.approx(-central(lambda t: vega_unit(time_to_expiration=t), 0.5, 1e-5), abs=1e-4)
    g = _lib.ft_greeks
    for key, fn in (("vomma", g.get_vomma), ("vera", g.get_vera), ("veta", g.get_veta)):
        assert res[key] == pytest.approx(fn(100, 105, 0.5, 0.04, 0.3, 0.012), abs=1e-8)
    assert res["charm"] == pytest.approx(g.get_charm(100, 105, 0.5, 0.04, 0.3, 0.012, False), abs=1e-8)
    assert_empty(call("options.second_order", **{**OFFSET, "volatility": float("nan")}))
    assert_empty(call("options.second_order", **OFFSET, kind=""))
    assert_empty(call("options.second_order", **{**OFFSET, "strike": -1.0}))
    assert "FT-030" in piece_ids("options.second_order")


def test_second_order_partial_FT_031():
    res = call("options.second_order_partial", **OFFSET)
    # hand value: exp(-rT) times the lognormal density of the terminal price at the strike, written out
    s, k, r, v, t, q = 100.0, 105.0, 0.04, 0.30, 0.5, 0.012
    var = v * v * t
    density = math.exp(-((math.log(k / s) - (r - q - 0.5 * v * v) * t) ** 2) / (2 * var)) / (k * math.sqrt(2 * math.pi * var))
    assert res.value == pytest.approx(math.exp(-r * t) * density, abs=1e-12)
    assert res.value == pytest.approx(call("options.dual_gamma", **OFFSET).value, abs=1e-12)     # d2C/dK2 is both
    assert res.value == pytest.approx(_lib.ft_greeks.get_second_order_partial_derivative(100, 105, 0.5, 0.04, 0.3, 0.012), abs=1e-8)
    assert_empty(call("options.second_order_partial", **{**OFFSET, "spot": float("nan")}))
    assert_empty(call("options.second_order_partial", **{**OFFSET, "time_to_expiration": -1.0}))
    assert "FT-031" in piece_ids("options.second_order_partial")


def test_third_order_FT_032(atm):
    res = call("options.third_order", **OFFSET).value
    g = _lib.ft_greeks
    gamma = lambda **o: call("options.gamma", **{**OFFSET, **o}).value  # noqa: E731
    vomma = lambda **o: call("options.second_order", **{**OFFSET, **o}).value["vomma"]  # noqa: E731
    # hand values: each third order greek is the derivative of a second order one
    assert res["speed"] == pytest.approx(central(lambda s: gamma(spot=s), 100.0, 1e-3), abs=1e-9)
    assert res["zomma"] == pytest.approx(central(lambda v: gamma(volatility=v), 0.30, 1e-5), abs=1e-7)
    assert res["color"] == pytest.approx(-central(lambda t: gamma(time_to_expiration=t), 0.5, 1e-5), abs=1e-7)
    assert res["ultima"] == pytest.approx(central(lambda v: vomma(volatility=v), 0.30, 1e-5), abs=1e-3)
    # written from the closed forms and equal to the library at the same point
    assert res["speed"] == pytest.approx(g.get_speed(100, 105, 0.5, 0.04, 0.3, 0.012), abs=1e-8)
    assert res["zomma"] == pytest.approx(g.get_zomma(100, 105, 0.5, 0.04, 0.3, 0.012), abs=1e-8)
    assert res["color"] == pytest.approx(g.get_color(100, 105, 0.5, 0.04, 0.3, 0.012), abs=1e-8)
    assert res["ultima"] == pytest.approx(g.get_ultima(100, 105, 0.5, 0.04, 0.3, 0.012), abs=1e-8)
    args = {k: atm[k] for k in ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield")}
    chain_res = call("options.third_order", **args).value
    assert chain_res["speed"] == pytest.approx(g.get_speed(*[args[k] for k in ("spot", "strike", "time_to_expiration", "rate", "volatility", "dividend_yield")]), abs=1e-10)
    assert_empty(call("options.third_order", **{**OFFSET, "volatility": float("nan")}))
    assert_empty(call("options.third_order", **{**OFFSET, "volatility": 0.0}))
    assert_empty(call("options.third_order", **{**OFFSET, "strike": 0.0}))
    assert "FT-032" in piece_ids("options.third_order")
