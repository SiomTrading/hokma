"""Prices. Rows FT-019, FT-020, FT-022, FT-023, FT-025 and FT-034."""
import math

import pytest

from hokma.quant.errors import BadInput
from hokma.quant.options import _lib

from .conftest import assert_empty, call, piece_ids

# Classic case with published values: S=100, K=100, r=5 percent, sigma=20 percent, T=1 year, no dividends.
CLASSIC = {"spot": 100.0, "strike": 100.0, "rate": 0.05, "volatility": 0.20, "time_to_expiration": 1.0, "dividend_yield": 0.0}


def test_d1_d2_FT_019(atm):
    res = call("options.d1_d2", **CLASSIC)
    # hand value: d1 = (ln 1 + (0.05 + 0.02) * 1) / 0.2 = 0.35 and d2 = d1 - 0.2 = 0.15
    assert res.value["d1"] == pytest.approx(0.35, abs=1e-12)
    assert res.value["d2"] == pytest.approx(0.15, abs=1e-12)
    args = {k: atm[k] for k in ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield")}
    chain_res = call("options.d1_d2", **args)
    d1 = _lib.ft_bs.get_d1(args["spot"], args["strike"], args["rate"], args["volatility"], args["time_to_expiration"], args["dividend_yield"])
    assert chain_res.value["d1"] == pytest.approx(d1, abs=1e-8)
    assert chain_res.value["d2"] == pytest.approx(d1 - args["volatility"] * math.sqrt(args["time_to_expiration"]), abs=1e-8)
    assert "FT-019" in piece_ids("options.d1_d2")
    for bad in ({"spot": float("nan")}, {"volatility": 0.0}, {"time_to_expiration": -1.0}, {"strike": 0.0}, {"volatility": 50.0}):
        assert_empty(call("options.d1_d2", **{**CLASSIC, **bad}))
    with pytest.raises(BadInput):
        call("options.d1_d2", **{**CLASSIC, "spot": "100"})
    with pytest.raises(BadInput):
        call("options.d1_d2", **{**CLASSIC, "volatility": []})


def test_black_scholes_FT_020(atm, chain):
    assert call("options.black_scholes", **CLASSIC).value == pytest.approx(10.450583572185565, abs=1e-9)    # published value
    assert call("options.black_scholes", **CLASSIC, kind="put").value == pytest.approx(5.573526022256971, abs=1e-9)
    origin = _lib.ft_bs.get_black_scholes(100, 100, 0.05, 0.2, 1.0, 0.0, False)
    assert call("options.black_scholes", **CLASSIC).value == pytest.approx(origin, abs=1e-8)
    # the recorded chain: the price of the at-the-money call at its own implied volatility is its mid price
    res = call("options.black_scholes", **{k: atm[k] for k in ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield")})
    assert res.value == pytest.approx(atm["row"]["mid"], abs=1e-4)
    # expiry: the intrinsic value
    assert call("options.black_scholes", **{**CLASSIC, "time_to_expiration": 0.0, "spot": 110.0}).value == pytest.approx(10.0)
    assert call("options.black_scholes", **{**CLASSIC, "time_to_expiration": 0.0, "spot": 110.0}, kind="put").value == 0.0
    assert_empty(call("options.black_scholes", **CLASSIC, kind="straddle"), "kind")
    assert_empty(call("options.black_scholes", **{**CLASSIC, "rate": float("nan")}))
    assert_empty(call("options.black_scholes", **{**CLASSIC, "spot": -5.0}))
    assert "FT-020" in piece_ids("options.black_scholes")


def test_put_call_parity_FT_022(chain):
    call_px = call("options.black_scholes", **CLASSIC).value
    put_px = call("options.black_scholes", **CLASSIC, kind="put").value
    res = call("options.put_call_parity", spot=100.0, strike=100.0, rate=0.05, time_to_expiration=1.0, call_price=call_px, put_price=put_px)
    # hand value: C - P = S - K * exp(-r T) = 100 - 100 * exp(-0.05) = 4.877057549928599
    assert res.value["forward_differential"] == pytest.approx(100 - 100 * math.exp(-0.05), abs=1e-12)
    assert res.value["gap"] == pytest.approx(0.0, abs=1e-12)
    assert res.value["within_tolerance"] and res.value["within_american_bounds"]
    off = call("options.put_call_parity", spot=100.0, strike=100.0, rate=0.05, time_to_expiration=1.0, call_price=call_px + 1.0, put_price=put_px)
    assert off.value["gap"] == pytest.approx(1.0, abs=1e-12) and not off.value["within_tolerance"]
    # a pair of the recorded chain, with its quotes rounded to cents: inside a few cents of parity and inside the American bounds
    rows = {(r["type"], r["strike"]): r for r in chain["contracts"] if r["expiration"] == "2027-01-15"}
    c_row, p_row = rows[("call", 590.0)], rows[("put", 590.0)]
    t = _lib_years(chain, "2027-01-15")
    pair = call("options.put_call_parity", spot=chain["underlying_price"], strike=590.0, rate=chain["rate"], dividend_yield=chain["dividend_yield"],
                time_to_expiration=t, call_price=c_row["mid"], put_price=p_row["mid"], tolerance=0.05)
    assert abs(pair.value["gap"]) < 0.05 and pair.value["within_american_bounds"]
    origin = _lib.ft_bs.get_put_call_parity(chain["underlying_price"], 590.0, chain["rate"], t, chain["dividend_yield"], call_price=c_row["mid"], put_price=p_row["mid"])
    assert pair.value["gap"] == pytest.approx(origin, abs=1e-8)
    assert any("American" in w for w in pair.warnings)
    assert_empty(call("options.put_call_parity", spot=100.0, strike=100.0, rate=0.05, time_to_expiration=1.0, call_price=-1.0, put_price=1.0))
    assert_empty(call("options.put_call_parity", spot=float("nan"), strike=100.0, rate=0.05, time_to_expiration=1.0, call_price=1.0, put_price=1.0))
    assert "FT-022" in piece_ids("options.put_call_parity")


def _lib_years(chain, expiration):
    from hokma.quant.options import _common as c

    return c.years_between(chain["as_of"], expiration)


def test_garman_kohlhagen_FT_023(atm):
    args = {"spot": 100.0, "strike": 105.0, "rate": 0.03, "volatility": 0.25, "time_to_expiration": 0.5}
    res = call("options.garman_kohlhagen", **args, foreign_rate=0.03)
    # hand value: with the foreign rate equal to the risk-free rate this is Black-76 on a forward equal to spot:
    # exp(-rT) * (F N(d1) - K N(d2)) with F = S
    t, s, k, r, v = args["time_to_expiration"], args["spot"], args["strike"], args["rate"], args["volatility"]
    d1 = (math.log(s / k) + 0.5 * v * v * t) / (v * math.sqrt(t))
    d2 = d1 - v * math.sqrt(t)
    cdf = lambda x: 0.5 * math.erfc(-x / math.sqrt(2))  # noqa: E731
    assert res.value == pytest.approx(math.exp(-r * t) * (s * cdf(d1) - k * cdf(d2)), abs=1e-10)
    assert any("Black-76" in w for w in res.warnings), "the limitation stays in the warnings"
    assert any("approximation" in w for w in res.warnings)
    other = call("options.garman_kohlhagen", **args, foreign_rate=0.01, kind="put")
    assert other.value == pytest.approx(_lib.ft_bs.get_garman_kohlhagen(100.0, 105.0, 0.03, 0.01, 0.25, 0.5, True), abs=1e-8)
    assert not any("Black-76" in w for w in other.warnings)
    chain_args = {"spot": atm["spot"], "strike": atm["strike"], "rate": atm["rate"], "volatility": atm["volatility"], "time_to_expiration": atm["time_to_expiration"]}
    gk = call("options.garman_kohlhagen", **chain_args, foreign_rate=atm["dividend_yield"])
    bs = call("options.black_scholes", **chain_args, dividend_yield=atm["dividend_yield"])
    assert gk.value == pytest.approx(bs.value, abs=1e-10)
    assert_empty(call("options.garman_kohlhagen", **args, foreign_rate=float("nan")))
    assert_empty(call("options.garman_kohlhagen", **{**args, "volatility": -0.2}, foreign_rate=0.01))
    assert "FT-023" in piece_ids("options.garman_kohlhagen")


def test_bjerksund_stensland_FT_025(atm):
    # hand value: with no dividend an American call is never exercised early, so it equals the European call
    call_res = call("options.bjerksund_stensland", **CLASSIC)
    assert call_res.value["american"] == pytest.approx(10.450583572185565, abs=1e-9)
    assert call_res.value["early_exercise_premium"] == pytest.approx(0.0, abs=1e-9)
    put_res = call("options.bjerksund_stensland", **CLASSIC, kind="put")
    assert put_res.value["european"] == pytest.approx(5.573526022256971, abs=1e-9)
    assert put_res.value["american"] > put_res.value["european"]
    origin = _lib.ft_bs.get_bjerksund_stensland(100, 100, 0.05, 0.2, 1.0, 0.0, True)
    assert put_res.value["american"] == pytest.approx(origin, abs=1e-8)
    # the approximation is a lower bound of the exact American price: below a 2,000 step tree, within 1.5 percent of it
    tree = call("options.binomial_tree", **CLASSIC, kind="put", n_steps=2000, method="leisen_reimer", style="american")
    assert put_res.value["american"] <= tree.value["price"] + 1e-3
    assert put_res.value["american"] == pytest.approx(tree.value["price"], rel=0.015)
    assert any("lower bound" in w for w in put_res.warnings)
    args = {k: atm[k] for k in ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield")}
    chain_put = call("options.bjerksund_stensland", **args, kind="put")
    assert chain_put.value["american"] >= chain_put.value["european"] - 1e-12
    assert_empty(call("options.bjerksund_stensland", **{**CLASSIC, "volatility": 0.0}))
    assert_empty(call("options.bjerksund_stensland", **{**CLASSIC, "spot": float("nan")}))
    assert_empty(call("options.bjerksund_stensland", **CLASSIC, kind="x"))
    assert "FT-025" in piece_ids("options.bjerksund_stensland")


def test_monte_carlo_FT_034(atm):
    res = call("options.monte_carlo", **CLASSIC, n_simulations=20000, time_steps=20, seed=7)
    origin = _lib.ft_options.get_monte_carlo_option_price(100, 100, 0.05, 0.2, 1.0, 0.0, False, 20000, 20, 7)
    assert res.value["price"] == pytest.approx(origin[0], abs=1e-8)
    assert res.value["standard_error"] == pytest.approx(origin[1], abs=1e-8)
    # hand value: the simulated price is within four standard errors of the published Black-Scholes value
    assert abs(res.value["price"] - 10.450583572185565) < 4 * res.value["standard_error"]
    again = call("options.monte_carlo", **CLASSIC, n_simulations=20000, time_steps=20, seed=7)
    assert again.value == res.value, "the same seed gives the same answer"
    assert call("options.monte_carlo", **CLASSIC, n_simulations=20000, time_steps=20, seed=8).value != res.value
    args = {k: atm[k] for k in ("spot", "strike", "rate", "volatility", "time_to_expiration", "dividend_yield")}
    chain_res = call("options.monte_carlo", **args, n_simulations=20000, time_steps=10, seed=1)
    assert abs(chain_res.value["price"] - atm["row"]["mid"]) < 4 * chain_res.value["standard_error"] + 0.05
    with pytest.raises(BadInput):
        call("options.monte_carlo", **CLASSIC, n_simulations=100_001)
    with pytest.raises(BadInput):
        call("options.monte_carlo", **CLASSIC, time_steps=1_001)
    with pytest.raises(BadInput):
        call("options.monte_carlo", **CLASSIC, n_simulations=1.5)
    assert_empty(call("options.monte_carlo", **CLASSIC, n_simulations=100_000, time_steps=1_000), "too large")
    assert_empty(call("options.monte_carlo", **{**CLASSIC, "volatility": float("nan")}))
    assert_empty(call("options.monte_carlo", **CLASSIC, seed=-1), "seed")
    assert "FT-034" in piece_ids("options.monte_carlo")
