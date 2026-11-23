"""Position greeks, volatility richness and liquidity. Rows ECT-031, AFS-005 and ECT-033."""
import math
from datetime import date, datetime, timezone
from decimal import Decimal

import numpy as np
import pytest

from hokma.contracts.identity import ContractSheet
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c

from .conftest import assert_empty, call, piece_ids


def sheet(multiplier: str = "100") -> ContractSheet:
    return ContractSheet("OPT:SPY:20261218:C:590", "OPT", "SPY", "SPY", Decimal(multiplier), None, None, date(2026, 12, 18), Decimal("590"), "C", "USD",
                         "test", datetime(2026, 11, 20, tzinfo=timezone.utc))


BASE = {"spot": 100.0, "rate": 0.04, "dividend_yield": 0.0}
LONG_CALL = {"kind": "call", "strike": 100.0, "volatility": 0.30, "quantity": 2, "time_to_expiration": 0.25}


def first(**over):
    args = {"spot": 100.0, "strike": 100.0, "rate": 0.04, "volatility": 0.30, "time_to_expiration": 0.25, "dividend_yield": 0.0, **over}
    return call("options.first_order", **args).value


def test_position_greeks_in_dollars_with_the_multiplier_of_the_sheet_ECT_031():
    res = call("options.position_greeks", legs=[LONG_CALL], multiplier=sheet().multiplier, **BASE).value["totals"]
    g1 = first()
    gamma1 = call("options.gamma", spot=100.0, strike=100.0, rate=0.04, volatility=0.30, time_to_expiration=0.25).value
    # hand values: two contracts of 100 shares are 200 shares of every per-share greek
    assert res["delta_shares"] == pytest.approx(200 * g1["delta"], abs=1e-9)
    assert res["delta_usd"] == pytest.approx(200 * g1["delta"] * 100.0, abs=1e-7)
    assert res["gamma_per_dollar"] == pytest.approx(200 * gamma1, abs=1e-9)
    assert res["gamma_usd_per_1pct"] == pytest.approx(200 * gamma1 * 100.0**2 / 100.0, abs=1e-7)
    assert res["vega_usd_per_point"] == pytest.approx(200 * g1["vega"], abs=1e-9)
    assert res["theta_usd_per_day"] == pytest.approx(200 * g1["theta"], abs=1e-9)
    assert res["rho_usd_per_point"] == pytest.approx(200 * g1["rho"] / 100.0, abs=1e-9)
    # no hidden 100: a sheet with multiplier 50 gives exactly half, and a multiplier of 1 gives a hundredth
    half = call("options.position_greeks", legs=[LONG_CALL], multiplier=sheet("50").multiplier, **BASE).value["totals"]
    assert half["delta_shares"] == pytest.approx(res["delta_shares"] / 2, abs=1e-12) and half["vega_usd_per_point"] == pytest.approx(res["vega_usd_per_point"] / 2, abs=1e-12)
    one = call("options.position_greeks", legs=[LONG_CALL], multiplier=1, **BASE).value["totals"]
    assert one["theta_usd_per_day"] == pytest.approx(res["theta_usd_per_day"] / 100, abs=1e-12)
    # second order terms by hand
    second = call("options.second_order", spot=100.0, strike=100.0, rate=0.04, volatility=0.30, time_to_expiration=0.25).value
    vanna = call("options.vanna", spot=100.0, strike=100.0, rate=0.04, volatility=0.30, time_to_expiration=0.25).value
    assert res["charm_shares_per_day"] == pytest.approx(200 * second["charm"] / 365.0, abs=1e-9)
    assert res["vanna_shares_per_point"] == pytest.approx(200 * vanna / 100.0, abs=1e-9)
    assert res["vomma_usd_per_point"] == pytest.approx(200 * second["vomma"] / 10_000.0, abs=1e-9)
    # a missing multiplier is refused, never defaulted
    assert_empty(call("options.position_greeks", legs=[LONG_CALL], **BASE), "multiplier")
    assert_empty(call("options.position_greeks", legs=[LONG_CALL], multiplier=0, **BASE), "positive")
    assert call("options.position_greeks", legs=[{**LONG_CALL, "multiplier": 10}], **BASE).value["totals"]["delta_shares"] == pytest.approx(20 * g1["delta"], abs=1e-9)


def test_aggregation_stock_hedge_and_short_legs_ECT_031():
    legs = [LONG_CALL, {**LONG_CALL, "strike": 110.0, "quantity": -2, "volatility": 0.28}, {"instrument": "stock", "quantity": -50}]
    res = call("options.position_greeks", legs=legs, multiplier=100, **BASE).value
    parts = [first(), first(strike=110.0, volatility=0.28)]
    assert res["totals"]["delta_shares"] == pytest.approx(200 * parts[0]["delta"] - 200 * parts[1]["delta"] - 50, abs=1e-9)
    assert res["totals"]["vega_usd_per_point"] == pytest.approx(200 * parts[0]["vega"] - 200 * parts[1]["vega"], abs=1e-9)
    assert len(res["legs"]) == 3 and res["legs"][2]["delta_shares"] == -50.0 and res["legs"][2]["gamma_per_dollar"] == 0.0
    assert sum(x["delta_usd"] for x in res["legs"]) == pytest.approx(res["totals"]["delta_usd"], abs=1e-7)


def test_full_revaluation_stress_ECT_031():
    res = call("options.position_greeks", legs=[LONG_CALL], multiplier=100, **BASE).value
    grid = {(x["spot_shock"], x["vol_shock_points"]): x["pnl_usd"] for x in res["stress"]}
    assert len(grid) == 15 and grid[(0.0, 0.0)] == pytest.approx(0.0, abs=1e-12)
    # hand value: reprice the call at 80 and at 70 percent volatility, by the formula, and take the difference of 200 shares
    base_px = c.bs_price(100, 100, 0.04, 0.30, 0.25, 0.0, False)
    assert grid[(-0.2, 0.0)] == pytest.approx(200 * (c.bs_price(80, 100, 0.04, 0.30, 0.25, 0.0, False) - base_px), abs=1e-9)
    assert grid[(0.1, 5.0)] == pytest.approx(200 * (c.bs_price(110, 100, 0.04, 0.35, 0.25, 0.0, False) - base_px), abs=1e-9)
    assert grid[(0.0, -5.0)] == pytest.approx(200 * (c.bs_price(100, 100, 0.04, 0.25, 0.25, 0.0, False) - base_px), abs=1e-9)
    # the second order estimate is not enough for a 20 percent move: the full revaluation differs from delta plus gamma
    taylor = 200 * (first()["delta"] * -20.0 + 0.5 * call("options.gamma", spot=100.0, strike=100.0, rate=0.04, volatility=0.30, time_to_expiration=0.25).value * 400.0)
    assert abs(grid[(-0.2, 0.0)] - taylor) > 1.0
    assert res["worst_case"]["pnl_usd"] == min(grid.values())
    custom = call("options.position_greeks", legs=[LONG_CALL], multiplier=100, **BASE, spot_shocks=[-0.5], vol_shocks=[0.0, 20.0]).value["stress"]
    assert [(x["spot_shock"], x["vol_shock_points"]) for x in custom] == [(-0.5, 0.0), (-0.5, 20.0)]
    # a stock leg moves one for one
    stock = call("options.position_greeks", legs=[{"instrument": "stock", "quantity": 10}], multiplier=100, **BASE, spot_shocks=[-0.2], vol_shocks=[0.0]).value
    assert stock["stress"][0]["pnl_usd"] == pytest.approx(-200.0)


def test_pin_risk_and_position_from_the_recorded_chain_ECT_031(chain):
    near = {"kind": "call", "strike": 100.0, "volatility": 0.30, "quantity": -1, "time_to_expiration": 1.0 / 365.0}
    assert call("options.position_greeks", legs=[near], multiplier=100, **BASE).value["pin_risk"] is True
    assert call("options.position_greeks", legs=[{**near, "quantity": 1}], multiplier=100, **BASE).value["pin_risk"] is False          # long legs have no pin risk
    assert call("options.position_greeks", legs=[{**near, "strike": 120.0}], multiplier=100, **BASE).value["pin_risk"] is False
    assert call("options.position_greeks", legs=[{**near, "time_to_expiration": 30.0 / 365.0}], multiplier=100, **BASE).value["pin_risk"] is False
    # a short straddle of the recorded chain, read with expiration and as_of: negative gamma and vega, positive theta, delta near zero
    rows = {r["type"]: r for r in chain["contracts"] if r["expiration"] == "2026-12-18" and r["strike"] == 590.0}
    legs = [{"kind": t, "strike": 590.0, "volatility": rows[t]["iv"], "quantity": -1, "expiration": "2026-12-18"} for t in ("call", "put")]
    res = call("options.position_greeks", legs=legs, multiplier=chain["multiplier"], spot=chain["underlying_price"], rate=chain["rate"],
               dividend_yield=chain["dividend_yield"], as_of=chain["as_of"]).value["totals"]
    assert res["gamma_per_dollar"] < 0 and res["vega_usd_per_point"] < 0 and res["theta_usd_per_day"] > 0 and abs(res["delta_shares"]) < 15
    assert res["theta_usd_per_day"] == pytest.approx(-100 * (rows["call"]["theta"] + rows["put"]["theta"]), abs=0.1)
    assert_empty(call("options.position_greeks", legs=[{"kind": "call", "strike": 100.0, "volatility": 0.3, "quantity": 1}], multiplier=100, **BASE), "expiration")
    assert_empty(call("options.position_greeks", legs=[], multiplier=100, **BASE), "at least one")
    assert_empty(call("options.position_greeks", legs=[{**LONG_CALL, "volatility": float("nan")}], multiplier=100, **BASE))
    assert_empty(call("options.position_greeks", legs=[{**LONG_CALL, "quantity": 0}], multiplier=100, **BASE), "quantity")
    assert_empty(call("options.position_greeks", legs=[{**LONG_CALL, "kind": "swap"}], multiplier=100, **BASE), "kind")
    assert_empty(call("options.position_greeks", legs=[LONG_CALL], multiplier=100, spot=float("nan"), rate=0.04))
    assert_empty(call("options.position_greeks", legs=[LONG_CALL], multiplier=100, **BASE, spot_shocks=[-1.5]), "shocks")
    with pytest.raises(BadInput):
        call("options.position_greeks", legs="straddle", multiplier=100, **BASE)
    with pytest.raises(BadInput):
        call("options.position_greeks", legs=[LONG_CALL], multiplier="100", **BASE)
    with pytest.raises(BadInput):
        call("options.position_greeks", legs=[LONG_CALL], multiplier=100, **BASE, spot_shocks=[0.1] * 26)
    with pytest.raises(BadInput):
        call("options.position_greeks", legs=[LONG_CALL] * 41, multiplier=100, **BASE)
    assert "ECT-031" in piece_ids("options.position_greeks")


def test_rebalance_rules_use_the_desk_thresholds_ECT_031():
    res = call("options.rebalance_check", delta_shares=35.0, vega_usd_per_point=-900.0, delta_band_shares=20.0, vega_limit_usd=500.0).value
    assert res["delta_action"] == {"side": "sell", "shares": 35.0, "reason": "absolute delta is above the band"}
    assert res["vega_action"]["vega_to_remove_usd_per_point"] == pytest.approx(400.0) and res["rebalance"] is True
    assert call("options.rebalance_check", delta_shares=-35.0, vega_usd_per_point=100.0, delta_band_shares=20.0, vega_limit_usd=500.0).value["delta_action"]["side"] == "buy"
    quiet = call("options.rebalance_check", delta_shares=20.0, vega_usd_per_point=500.0, delta_band_shares=20.0, vega_limit_usd=500.0).value
    assert quiet == {"delta_action": None, "vega_action": None, "rebalance": False}               # exactly on the threshold is inside
    assert_empty(call("options.rebalance_check", delta_shares=float("nan"), vega_usd_per_point=1.0, delta_band_shares=20.0, vega_limit_usd=500.0))
    assert_empty(call("options.rebalance_check", delta_shares=1.0, vega_usd_per_point=1.0, delta_band_shares=-1.0, vega_limit_usd=500.0))
    with pytest.raises(BadInput):
        call("options.rebalance_check", delta_shares="35", vega_usd_per_point=1.0, delta_band_shares=20.0, vega_limit_usd=500.0)
    with pytest.raises(BadInput):
        call("options.rebalance_check", delta_shares=1.0, vega_usd_per_point=1.0, delta_band_shares=20.0)


def test_gamma_scalping_with_explicit_units_ECT_031():
    # the skill said 180 dollars a day; with its own data the figure is 18 because the unit of the contract was ambiguous.
    # Here every unit is explicit: 0.5 * gamma * move^2 per share, times quantity and the multiplier of the sheet.
    res = call("options.gamma_scalp", gamma=0.15, theta_per_day=-0.125, quantity=1, multiplier=sheet().multiplier, move=10.0).value
    assert res["gamma_pnl_usd"] == pytest.approx(750.0) and res["theta_usd_per_day"] == pytest.approx(-12.5)
    assert res["net_pnl_usd_per_day"] == pytest.approx(737.5)
    # 750 over 12.5 is 60 days of theta, not the 11 the skill claimed
    assert res["theta_days_paid_by_move"] == pytest.approx(60.0)
    # the breakeven move of the skill (3.16) now has a base: gamma 0.02 and theta -0.10 per share give sqrt(2 * 0.10 / 0.02)
    be = call("options.gamma_scalp", gamma=0.02, theta_per_day=-0.10, quantity=3, multiplier=100, move=3.16227766).value
    assert be["breakeven_daily_move"] == pytest.approx(math.sqrt(10.0), abs=1e-12)
    assert be["net_pnl_usd_per_day"] == pytest.approx(0.0, abs=1e-6)                          # at the breakeven move the day is flat
    assert call("options.gamma_scalp", gamma=0.15, theta_per_day=-0.125, quantity=1, multiplier=10, move=10.0).value["gamma_pnl_usd"] == pytest.approx(75.0)
    assert_empty(call("options.gamma_scalp", gamma=-0.1, theta_per_day=-0.1, quantity=1, multiplier=100, move=1.0), "positive")
    assert_empty(call("options.gamma_scalp", gamma=0.1, theta_per_day=0.1, quantity=1, multiplier=100, move=1.0), "negative")
    assert_empty(call("options.gamma_scalp", gamma=float("nan"), theta_per_day=-0.1, quantity=1, multiplier=100, move=1.0))
    assert_empty(call("options.gamma_scalp", gamma=0.1, theta_per_day=-0.1, quantity=1, multiplier=0, move=1.0))
    with pytest.raises(BadInput):
        call("options.gamma_scalp", gamma="0.1", theta_per_day=-0.1, quantity=1, multiplier=100, move=1.0)
    with pytest.raises(BadInput):
        call("options.gamma_scalp", gamma=0.1, theta_per_day=-0.1, quantity=1, move=1.0)


def realized(closes, window):
    rets = np.diff(np.log(np.asarray(closes[-(window + 1):], dtype=float)))
    return float(np.std(rets, ddof=1) * math.sqrt(252))


def test_vol_richness_without_a_calibrated_threshold_is_uncalibrated_AFS_005(chain):
    args = {"contracts": chain["contracts"], "spot": chain["underlying_price"], "rate": chain["rate"], "as_of": chain["as_of"], "expiration": "2026-12-18",
            "dividend_yield": chain["dividend_yield"], "closes": chain["closes"]}
    res = call("options.vol_richness", **args)
    v = res.value
    # hand values: close to close volatility of the last 20, 60 and 90 returns, annualized with 252 days
    for window in (20, 60, 90):
        assert v["realized_vol"][str(window)] == pytest.approx(realized(chain["closes"], window), abs=1e-12)
        assert v["iv_over_realized"][str(window)] == pytest.approx(v["atm_iv"] / realized(chain["closes"], window), abs=1e-12)
        assert v["iv_minus_realized"][str(window)] == pytest.approx(v["atm_iv"] - realized(chain["closes"], window), abs=1e-12)
    # the chain was recorded with an at-the-money volatility of 0.155 at this expiry, a put skew (negative risk reversal) and a positive butterfly
    assert v["atm_iv"] == pytest.approx(0.155, abs=1e-3)
    assert v["risk_reversal_25d"] < 0 and v["butterfly_25d"] > 0
    assert v["risk_reversal_25d"] == pytest.approx(v["iv_25d_call"] - v["iv_25d_put"], abs=1e-15)
    assert v["butterfly_25d"] == pytest.approx(0.5 * (v["iv_25d_call"] + v["iv_25d_put"]) - v["atm_iv"], abs=1e-15)
    assert v["strike_25d_call"] > v["forward"] > v["strike_25d_put"]
    # never "rich" or "cheap" without a calibration from a backtest, whatever the ratio is
    assert v["classification"] == "uncalibrated" and "backtest" in v["classification_reason"]
    for ratio_case in ({"closes": [x * 1.0 for x in chain["closes"][-5:] * 30]},):
        out = call("options.vol_richness", **{**args, **ratio_case}).value
        assert out["classification"] == "uncalibrated"
    # the same answer from a realized volatility computed by another module
    given = call("options.vol_richness", **{k: v_ for k, v_ in args.items() if k != "closes"}, realized_vol={"20": 0.10, "60": 0.11, "90": 0.12}).value
    assert given["realized_vol"] == {"20": 0.10, "60": 0.11, "90": 0.12} and given["iv_over_realized"]["20"] == pytest.approx(given["atm_iv"] / 0.10, abs=1e-12)


def test_vol_richness_with_a_calibration_and_an_earnings_date_AFS_005(chain):
    args = {"contracts": chain["contracts"], "spot": chain["underlying_price"], "rate": chain["rate"], "as_of": chain["as_of"], "expiration": "2026-12-18",
            "dividend_yield": chain["dividend_yield"], "closes": chain["closes"]}
    ratio = call("options.vol_richness", **args).value["iv_over_realized"]["60"]
    cal = {"metric_window": 60, "rich_ratio": ratio - 0.01, "cheap_ratio": ratio - 0.02, "backtest_run_ref": "bt-001"}
    assert call("options.vol_richness", **args, calibration=cal).value["classification"] == "rich"
    assert call("options.vol_richness", **args, calibration={**cal, "rich_ratio": ratio + 0.02, "cheap_ratio": ratio + 0.01}).value["classification"] == "cheap"
    assert call("options.vol_richness", **args, calibration={**cal, "rich_ratio": ratio + 0.01, "cheap_ratio": ratio - 0.01}).value["classification"] == "fair"
    # an incomplete calibration is not used
    for bad in ({**cal, "backtest_run_ref": None}, {**cal, "backtest_run_ref": ""}, {"metric_window": 60, "rich_ratio": 1.2}, {**cal, "rich_ratio": 0.5, "cheap_ratio": 0.9},
                {**cal, "metric_window": 45}, {**cal, "rich_ratio": True}):
        out = call("options.vol_richness", **args, calibration=bad)
        assert out.value["classification"] == "uncalibrated", bad
    # the earnings date before the expiry makes the implied volatility include the jump: reported, and never classified
    jump = call("options.vol_richness", **args, calibration=cal, earnings_date="2026-12-03")
    assert jump.value["earnings_in_window"] is True and jump.value["classification"] == "uncalibrated" and "earnings" in jump.value["classification_reason"]
    assert any("earnings" in w for w in jump.warnings)
    after = call("options.vol_richness", **args, calibration=cal, earnings_date="2027-02-03")
    assert after.value["earnings_in_window"] is False and after.value["classification"] == "rich"
    assert_empty(call("options.vol_richness", **args, earnings_date="soon"), "ISO date")


def test_vol_richness_bad_input_AFS_005(chain):
    args = {"contracts": chain["contracts"], "spot": chain["underlying_price"], "rate": chain["rate"], "as_of": chain["as_of"], "expiration": "2026-12-18",
            "dividend_yield": chain["dividend_yield"], "closes": chain["closes"]}
    short = call("options.vol_richness", **{**args, "closes": chain["closes"][-30:]})
    assert short.value["realized_vol"]["20"] is not None and short.value["realized_vol"]["60"] is None and short.value["iv_over_realized"]["60"] is None
    assert any("60 days" in w for w in short.warnings) and short.value["classification"] == "uncalibrated"
    assert_empty(call("options.vol_richness", **{**args, "closes": [float("nan")] * 100}), "finite positive")
    assert_empty(call("options.vol_richness", **{**args, "closes": [0.0] * 100}), "finite positive")
    assert_empty(call("options.vol_richness", **{k: v for k, v in args.items() if k != "closes"}), "closes or realized_vol")
    assert_empty(call("options.vol_richness", **{**args, "contracts": []}), "no rows")
    assert_empty(call("options.vol_richness", **{**args, "contracts": [{"strike": 100.0}]}), "no rows")
    assert_empty(call("options.vol_richness", **{**args, "expiration": "2030-01-01"}), "no rows")
    assert_empty(call("options.vol_richness", **{**args, "expiration": "2020-01-01"}), "expired")
    assert_empty(call("options.vol_richness", **{**args, "spot": float("nan")}))
    thin = [r for r in chain["contracts"] if r["expiration"] == "2026-12-18"][:2]
    assert_empty(call("options.vol_richness", **{**args, "contracts": thin}), "at least 3")
    with pytest.raises(BadInput):
        call("options.vol_richness", **{**args, "contracts": "chain"})
    with pytest.raises(BadInput):
        call("options.vol_richness", **{**args, "closes": "100,101"})
    with pytest.raises(BadInput):
        call("options.vol_richness", **{**args, "realized_vol": [0.1]})
    with pytest.raises(BadInput):
        call("options.vol_richness", **{**args, "calibration": "bt"})
    with pytest.raises(BadInput):
        call("options.vol_richness", **{**args, "rate": "0.04"})
    assert "AFS-005" in piece_ids("options.vol_richness")


def test_liquidity_of_a_contract_ECT_033(chain):
    res = call("options.liquidity", bid=1.00, ask=1.10, quantity=10, multiplier=sheet().multiplier, open_interest=500, volume=100, trade_price=1.08).value
    # hand values: mid 1.05, spread 0.10; exit cost at 3x is 10 contracts * 100 * 0.30 / 2 = 150 dollars
    assert res["mid"] == pytest.approx(1.05) and res["quoted_spread"] == pytest.approx(0.10) and res["relative_spread"] == pytest.approx(0.10 / 1.05)
    assert res["effective_spread"] == pytest.approx(2 * 0.03) and res["relative_effective_spread"] == pytest.approx(0.06 / 1.05)
    assert [x["multiple"] for x in res["stressed"]] == [3.0, 4.0, 5.0]
    assert [x["spread"] for x in res["stressed"]] == pytest.approx([0.30, 0.40, 0.50])
    assert [x["exit_cost_usd"] for x in res["stressed"]] == pytest.approx([150.0, 200.0, 250.0])
    assert res["position_value_usd"] == pytest.approx(1050.0)
    # 10 contracts against 20 percent of a volume of 100 is half a day; open interest share is 2 percent
    assert res["days_to_liquidate"] == pytest.approx(0.5) and res["above_one_day_of_volume"] is False
    assert res["oi_share"] == pytest.approx(0.02) and res["above_oi_share_cap"] is False
    big = call("options.liquidity", bid=1.00, ask=1.10, quantity=30, multiplier=100, open_interest=200, volume=100).value
    assert big["days_to_liquidate"] == pytest.approx(1.5) and big["above_one_day_of_volume"] is True      # more than one day of volume at 20 percent
    assert big["oi_share"] == pytest.approx(0.15) and big["above_oi_share_cap"] is True
    custom = call("options.liquidity", bid=1.00, ask=1.10, quantity=10, multiplier=100, volume=100, participation=0.10, stress_multiples=[2, 5], bid_size=40).value
    assert custom["days_to_liquidate"] == pytest.approx(1.0) and len(custom["stressed"]) == 2 and custom["bid_depth_cover"] == pytest.approx(4.0)
    # a recorded LEAPS-like contract (the longest expiry, an out-of-the-money put) with its own open interest and volume
    row = next(r for r in chain["contracts"] if r["expiration"] == "2027-06-18" and r["type"] == "put" and r["strike"] == 540.0)
    leaps = call("options.liquidity", bid=row["bid"], ask=row["ask"], quantity=50, multiplier=chain["multiplier"], open_interest=row["open_interest"], volume=row["volume"]).value
    assert leaps["oi_share"] == pytest.approx(50 / row["open_interest"]) and leaps["days_to_liquidate"] == pytest.approx(50 / (0.2 * row["volume"]))
    # no quantity: spread figures only; no volume today: warns instead of dividing by zero
    spread_only = call("options.liquidity", bid=1.0, ask=1.1).value
    assert "days_to_liquidate" not in spread_only and spread_only["stressed"][0]["exit_cost_usd"] is None
    quiet = call("options.liquidity", bid=1.0, ask=1.1, quantity=5, multiplier=100, volume=0, open_interest=0)
    assert quiet.value["days_to_liquidate"] is None and any("no volume" in w for w in quiet.warnings) and any("open interest is zero" in w for w in quiet.warnings)


def test_liquidity_bad_input_ECT_033():
    base = {"bid": 1.0, "ask": 1.1}
    assert_empty(call("options.liquidity", **base, quantity=5), "multiplier")
    assert_empty(call("options.liquidity", **base, quantity=5, multiplier=0), "multiplier")
    assert_empty(call("options.liquidity", bid=1.2, ask=1.1), "bid <= ask")
    assert_empty(call("options.liquidity", bid=float("nan"), ask=1.1))
    assert_empty(call("options.liquidity", bid=-1.0, ask=1.1))
    assert_empty(call("options.liquidity", **base, quantity=-3, multiplier=100), "quantity")
    assert_empty(call("options.liquidity", **base, quantity=3, multiplier=100, volume=-1), "must not be negative")
    assert_empty(call("options.liquidity", **base, participation=0.0), "participation")
    assert_empty(call("options.liquidity", **base, participation=1.5), "participation")
    assert_empty(call("options.liquidity", **base, stress_multiples=[0.5]), "stress")
    assert_empty(call("options.liquidity", **base, stress_multiples=[11]), "stress")
    assert_empty(call("options.liquidity", **base, trade_price=float("inf")))
    with pytest.raises(BadInput):
        call("options.liquidity", **base, stress_multiples=[])
    with pytest.raises(BadInput):
        call("options.liquidity", **base, stress_multiples="3,5")
    with pytest.raises(BadInput):
        call("options.liquidity", bid="1.0", ask=1.1)
    assert "ECT-033" in piece_ids("options.liquidity")


def test_chain_liquidity_scan_of_the_recorded_chain_ECT_033(chain):
    res = call("options.chain_liquidity", contracts=chain["contracts"]).value
    rels = sorted((r["ask"] - r["bid"]) / r["mid"] for r in chain["contracts"])
    assert res["n_valid"] == len(chain["contracts"]) and res["n_invalid"] == 0 and res["failing"] == [] and res["n_failing"] == 0
    assert res["median_relative_spread"] == pytest.approx(float(np.median(rels)), abs=1e-12)
    assert res["p90_relative_spread"] == pytest.approx(rels[math.ceil(0.9 * len(rels)) - 1], abs=1e-12)
    flagged = call("options.chain_liquidity", contracts=chain["contracts"], max_relative_spread=0.10, min_open_interest=1000).value
    expected = [r for r in chain["contracts"] if (r["ask"] - r["bid"]) / r["mid"] > 0.10 or r["open_interest"] < 1000]
    assert flagged["n_failing"] == len(expected) and 0 < len(flagged["failing"]) <= 20
    assert [x["relative_spread"] for x in flagged["failing"]] == sorted((x["relative_spread"] for x in flagged["failing"]), reverse=True)
    mixed = call("options.chain_liquidity", contracts=[{"bid": 1.0, "ask": 1.2}, {"bid": None, "ask": 1.0}, {"bid": 2.0, "ask": 1.0}, {"bid": float("nan"), "ask": 1.0}])
    assert mixed.value["n_valid"] == 1 and mixed.value["n_invalid"] == 3
    assert_empty(call("options.chain_liquidity", contracts=[]), "no contract")
    assert_empty(call("options.chain_liquidity", contracts=[{"bid": None, "ask": None}]), "no contract")
    assert_empty(call("options.chain_liquidity", contracts=chain["contracts"], max_relative_spread=-1.0), "thresholds")
    with pytest.raises(BadInput):
        call("options.chain_liquidity", contracts="chain")
    with pytest.raises(BadInput):
        call("options.chain_liquidity", contracts=[1])
