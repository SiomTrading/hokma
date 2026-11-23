"""Criteria of the eight investor profiles: INV-001 to INV-007, SHF-004 and SHF-006 to SHF-009."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.desks.core.signal import Criterion, contains_execution_terms
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import criteria

from .conftest import AS_OF, call, piece_ids, with_last

TOL = 1e-8
PRICE_FOR_MARGIN = 18          # market cap 1,764 against a buy-below value of 1,806.43
OE = 237.6 + 46 - 75 - (650 - 330 - (600 - 320))          # owner earnings of 2023 by hand: 168.6
INTRINSIC = OE / (0.10 - 0.03)


def by_name(res) -> dict:
    return {c["name"]: c for c in res.value["criteria"]}


def check_contract(res) -> None:
    """Every criterion is a valid Criterion of the Signal contract, and nothing in the result speaks of size, exits or timing."""
    for c in res.value["criteria"]:
        crit = Criterion.from_dict(c)
        assert crit.passed is None or isinstance(crit.passed, bool)
    forbidden = ("size", "sizing", "stop", "target", "entry", "quantity", "shares_to")
    keys = set(res.value) | {c["name"] for c in res.value["criteria"]}
    assert not [k for k in keys if any(f in k for f in forbidden)]
    assert contains_execution_terms(" ".join(c["name"] for c in res.value["criteria"])) is None
    assert res.value["lean"] in (-1, 0, 1)


# ---------------------------------------------------------------------------------------------------------------------
def test_owner_earnings_with_margin_of_safety_SHF_004(alfa):
    res = call("fundamentals.owner_earnings", statements=alfa, as_of=AS_OF)
    v = res.value
    # net income 237.6 + D&A 46 - capex 75 - change in working capital 40 = 168.6; value = 168.6 / (0.10 - 0.03); keep 25 percent
    assert v["owner_earnings"] == pytest.approx(168.6, abs=TOL) == pytest.approx(OE, abs=TOL)
    assert v["intrinsic_value"] == pytest.approx(168.6 / 0.07, abs=TOL)
    assert v["buy_below_value"] == pytest.approx(168.6 / 0.07 * 0.75, abs=TOL)
    assert v["market_cap"] == 5880 and v["margin_of_safety"] == pytest.approx(1 - 5880 / INTRINSIC, abs=TOL) and v["passes_margin"] is False
    assert v["period"] == "2023-12-31"
    # the assumptions are printed in the params
    assert res.params["discount_rate"] == 0.10 and res.params["growth_rate"] == 0.03 and res.params["safety_margin"] == 0.25
    assert "net income" in res.params["formula"] and "owner earnings /" in res.params["value_model"]
    cheap = call("fundamentals.owner_earnings", statements=with_last(alfa, price=PRICE_FOR_MARGIN), as_of=AS_OF).value
    assert cheap["passes_margin"] is True and cheap["margin_of_safety"] == pytest.approx(1 - 18 * 98 / INTRINSIC, abs=TOL)
    assert cheap["margin_of_safety"] > 0.25
    other = call("fundamentals.owner_earnings", statements=alfa, as_of=AS_OF, discount_rate=0.12, growth_rate=0.02, safety_margin=0.5).value
    assert other["intrinsic_value"] == pytest.approx(OE / 0.10, abs=TOL) and other["buy_below_value"] == pytest.approx(OE / 0.10 * 0.5, abs=TOL)
    # owner earnings that are not positive give no value
    loss = call("fundamentals.owner_earnings", statements=with_last(alfa, net_income=-100), as_of=AS_OF)
    assert loss.value["intrinsic_value"] is None and "owner_earnings_not_positive: no value can be built from it" in loss.warnings
    no_price = call("fundamentals.owner_earnings", statements=alfa.drop(columns=["price"]), as_of=AS_OF).value
    assert no_price["market_cap"] is None and no_price["margin_of_safety"] is None
    assert call("fundamentals.owner_earnings", statements=alfa.drop(columns=["capex"]), as_of=AS_OF).warnings[-1] == "data_missing:capex"
    assert call("fundamentals.owner_earnings", statements=alfa.iloc[0:0]).value is None
    assert call("fundamentals.owner_earnings", statements=pd.DataFrame({"net_income": [np.nan]}, index=["x"])).value is None
    for bad in (dict(growth_rate=0.2, discount_rate=0.1), dict(safety_margin=1.0), dict(discount_rate="10%")):
        with pytest.raises(BadInput):
            call("fundamentals.owner_earnings", statements=alfa, **bad)
    assert "SHF-004" in piece_ids("fundamentals.owner_earnings")


# ---------------------------------------------------------------------------------------------------------------------
def test_quality_owner_INV_001(alfa, bravo, charlie):
    res = call("fundamentals.quality_owner", statements=alfa, as_of=AS_OF)
    check_contract(res)
    c = by_name(res)
    roe = [237.6 / 1095, 208.8 / ((900 + 1030) / 2), 180 / ((800 + 900) / 2)]
    assert c["roe_average"]["value"] == pytest.approx(sum(roe) / 3, abs=TOL) and c["roe_average"]["passed"] is True
    assert c["roe_minimum"]["value"] == pytest.approx(min(roe), abs=TOL) and c["roe_minimum"]["passed"] is True
    assert c["debt_to_equity"]["value"] == pytest.approx(260 / 1160, abs=TOL) and c["debt_to_equity"]["passed"] is True
    assert c["free_cash_flow_per_share"]["value"] == pytest.approx((290 - 75) / 98, abs=TOL) and c["free_cash_flow_per_share"]["passed"] is True
    assert c["margin_of_safety"]["value"] == pytest.approx(1 - 5880 / INTRINSIC, abs=TOL) and c["margin_of_safety"]["passed"] is False
    assert c["book_value_cagr"]["value"] == pytest.approx(((1160 / 98) / (800 / 100)) ** (1 / 3) - 1, abs=TOL)
    margins = [200 / 1000, 235 / 1100, 270 / 1200, 305 / 1300]
    assert c["margin_stability_std"]["value"] == pytest.approx(float(np.std(margins)), abs=TOL)
    # a price that demands perfection (2.44 times the value) lowers the signal to negative
    assert res.value["lean"] == -1 and res.value["reason_code"] == "deterioration_or_price_demands_perfection"
    # a great business at a full price is neutral (a great business at a price that is too high), and at a price inside the margin it is positive
    full = call("fundamentals.quality_owner", statements=with_last(alfa, price=30), as_of=AS_OF)
    assert full.value["lean"] == 0 and full.value["reason_code"] == "quality_without_margin_of_safety"
    cheap = call("fundamentals.quality_owner", statements=with_last(alfa, price=PRICE_FOR_MARGIN), as_of=AS_OF)
    assert cheap.value["lean"] == 1 and cheap.value["reason_code"] == "quality_with_margin_of_safety"
    # the confidence cap goes down to 89 because the series is shorter than ten periods; the depth is declared
    assert cheap.value["confidence_cap"] == 89 and cheap.value["depth_periods"] == 4
    assert "competitive moat" in cheap.value["cannot_judge"] and "management honesty" in cheap.value["cannot_judge"]
    for name in ("margin_of_safety", "roe_average"):
        assert by_name(cheap)[name]["source"].startswith("fundamentals.")
    assert call("fundamentals.quality_owner", statements=alfa, as_of=AS_OF).params["roe_average_min"] == 0.15
    # the inputs of a required criterion decide: no price, a short series, a stale filing or losses give no value and name the gap
    no_price = call("fundamentals.quality_owner", statements=alfa.drop(columns=["price"]), as_of=AS_OF)
    assert no_price.value is None and "data_missing:market_cap_or_positive_owner_earnings" in no_price.warnings
    short = call("fundamentals.quality_owner", statements=charlie, as_of=AS_OF)
    assert short.value is None and "data_missing:periods_at_least_4_have_2" in short.warnings
    stale = call("fundamentals.quality_owner", statements=alfa, as_of="2025-01-31")
    assert stale.value is None and any(w.startswith("data_missing:filing_newer_than_150_days_is_") for w in stale.warnings)
    assert call("fundamentals.quality_owner", statements=alfa, as_of="2025-01-31", max_age_days=400).value is not None
    losses = call("fundamentals.quality_owner", statements=bravo, as_of=AS_OF)
    assert losses.value is None and "not_applicable:net_income_negative_in_most_periods" in losses.warnings
    assert call("fundamentals.quality_owner", statements=alfa.iloc[0:0]).value is None
    assert call("fundamentals.quality_owner", statements=pd.DataFrame({"net_income": [np.nan] * 4}, index=list("abcd"))).value is None
    for bad in (dict(min_periods=1), dict(min_periods=2.5), dict(max_age_days=0), dict(growth_rate=0.5)):
        with pytest.raises(BadInput):
            call("fundamentals.quality_owner", statements=alfa, as_of=AS_OF, **bad)
    assert {"INV-001", "SHF-006"} <= set(piece_ids("fundamentals.quality_owner"))


def test_quality_owner_thresholds_SHF_006(alfa):
    cheap = with_last(alfa, price=PRICE_FOR_MARGIN)

    def run(df):
        return by_name(call("fundamentals.quality_owner", statements=df, as_of=AS_OF))

    # debt to equity of 0.5 passes (at most), a little above fails
    at_edge = with_last(cheap, long_term_debt=540)                 # (40 + 540) / 1160 = 0.5
    assert run(at_edge)["debt_to_equity"]["value"] == pytest.approx(0.5, abs=TOL) and run(at_edge)["debt_to_equity"]["passed"] is True
    above = with_last(cheap, long_term_debt=545)
    assert run(above)["debt_to_equity"]["passed"] is False
    # ROE sustained: an average under 15 percent fails the criterion even with a floor above 10 percent
    low = cheap.copy()
    low["net_income"] = low["net_income"] * 0.6                    # ROE about 13 percent
    crit = run(low)
    assert crit["roe_average"]["value"] < 0.15 and crit["roe_average"]["passed"] is False and crit["roe_minimum"]["passed"] is True
    weak = call("fundamentals.quality_owner", statements=low, as_of=AS_OF)
    assert weak.value["lean"] == 0 and weak.value["reason_code"] == "quality_criteria_not_all_met"
    # a price that is excessive lowers the signal (from +1 at 18 to 0 at 30 to -1 at 60)
    leans = [call("fundamentals.quality_owner", statements=with_last(cheap, price=p), as_of=AS_OF).value["lean"] for p in (18, 30, 60)]
    assert leans == [1, 0, -1]
    # a deteriorating ROE is negative whatever the price
    falling = cheap.copy()
    falling.loc[falling["filed"] <= AS_OF, "net_income"] = [200.0, 180.0, 160.0, 100.0]
    assert call("fundamentals.quality_owner", statements=falling, as_of=AS_OF).value["lean"] == -1
    assert "SHF-006" in piece_ids("fundamentals.quality_owner")


# ---------------------------------------------------------------------------------------------------------------------
def test_inversion_veto_INV_002(alfa, bravo, charlie):
    res = call("fundamentals.inversion_veto", statements=alfa, as_of=AS_OF)
    check_contract(res)
    c = by_name(res)
    assert c["margin_change"]["value"] == pytest.approx(305 / 1300 - 200 / 1000, abs=TOL) and c["margin_change"]["passed"] is True
    assert c["earnings_quality_fcf_to_net_income"]["value"] == pytest.approx(np.mean([140 / 152, 165 / 180, 190 / 208.8, 215 / 237.6]), abs=TOL)
    assert c["loss_periods"]["value"] == 0
    # neutral by default and the veto does not exist unless the mandate turns it on
    assert res.value["lean"] == 0 and res.value["veto"] is None and res.value["veto_enabled"] is False and res.value["confidence_cap"] == 89
    assert res.value["failed_criteria"] == [] and res.value["too_hard"] is False
    # no failure found and a price inside the margin of safety: positive, but rare
    good = call("fundamentals.inversion_veto", statements=with_last(alfa, price=PRICE_FOR_MARGIN), as_of=AS_OF)
    assert good.value["lean"] == 1 and good.value["reason_code"] == "no_failure_found_and_margin_of_safety"
    # strong failures: falling margins, rising debt, cash flow far below income and losses
    bad = call("fundamentals.inversion_veto", statements=bravo, as_of=AS_OF)
    assert bad.value["lean"] == -1 and bad.value["too_hard"] is True and bad.value["veto"] is None
    assert {"margin_change", "earnings_quality_fcf_to_net_income", "loss_periods"} <= set(bad.value["failed_criteria"])
    vetoing = call("fundamentals.inversion_veto", statements=bravo, as_of=AS_OF, veto_enabled=True)
    assert vetoing.value["veto"] is True and vetoing.value["veto_enabled"] is True
    assert call("fundamentals.inversion_veto", statements=alfa, as_of=AS_OF, veto_enabled=True).value["veto"] is False
    # an inconsistent series with a single weak sign is "too hard": neutral
    one = alfa.copy()
    one.loc[one["filed"] <= AS_OF, "net_income"] = [152.0, -5.0, 208.8, 237.6]
    hard = call("fundamentals.inversion_veto", statements=one, as_of=AS_OF)
    assert hard.value["too_hard"] is True and hard.value["lean"] == 0 and hard.value["reason_code"] == "too_hard_series_is_inconsistent"
    miss = call("fundamentals.inversion_veto", statements=charlie, as_of=AS_OF)
    assert miss.value is None and "data_missing:periods_at_least_4_have_2" in miss.warnings
    assert call("fundamentals.inversion_veto", statements=alfa.drop(columns=["capex"]), as_of=AS_OF).value is None
    assert call("fundamentals.inversion_veto", statements=alfa.iloc[0:0]).value is None
    assert "cannot_judge" in res.value and "management incentives" in res.value["cannot_judge"]
    assert "INV-002" in piece_ids("fundamentals.inversion_veto")


# ---------------------------------------------------------------------------------------------------------------------
def test_safety_margin_INV_003(alfa, bravo, charlie):
    # the classic defensive thresholds, cited and fixed here: P/E 15, P/B 1.5, product 22.5, current ratio 2 (strict) and 1.5 (minimum)
    assert criteria.CLASSIC_DEFENSIVE == {"pe_max": 15.0, "pb_max": 1.5, "pe_x_pb_max": 22.5, "current_ratio_strict": 2.0, "current_ratio_minimum": 1.5,
                               "debt_to_net_current_assets_max": 1.0}
    res = call("fundamentals.safety_margin", statements=alfa, as_of=AS_OF)
    check_contract(res)
    c = by_name(res)
    eps, bvps = 237.6 / 98, 1160 / 98
    assert c["price_to_earnings"]["value"] == pytest.approx(60 / eps, abs=TOL) and c["price_to_earnings"]["passed"] is False
    assert c["price_to_book"]["value"] == pytest.approx(60 / bvps, abs=TOL)
    assert c["pe_times_pb"]["value"] == pytest.approx(60 / eps * 60 / bvps, abs=TOL)
    assert c["current_ratio_strict"]["value"] == pytest.approx(650 / 330, abs=TOL) and c["current_ratio_strict"]["passed"] is False
    assert c["current_ratio_minimum"]["passed"] is True
    assert c["long_term_debt_to_net_current_assets"]["value"] == pytest.approx(220 / 320, abs=TOL)
    assert res.value["lean"] == -1 and res.value["reason_code"].startswith("weak_finances")        # product 125 capitalizes hope
    # cheap and solid: price inside the margin and a strict current ratio
    solid = with_last(alfa, price=17, current_assets=700)           # P/E 7.0, P/B 1.44, current ratio 2.12
    good = call("fundamentals.safety_margin", statements=solid, as_of=AS_OF)
    assert good.value["lean"] == 1 and by_name(good)["current_ratio_strict"]["passed"] is True
    product_only = with_last(alfa, price=24, current_assets=700)    # P/E 9.9 and P/B 2.03: only the product of 20.1 is under 22.5
    po = call("fundamentals.safety_margin", statements=product_only, as_of=AS_OF)
    assert by_name(po)["price_to_book"]["passed"] is False and by_name(po)["pe_times_pb"]["passed"] is True and po.value["lean"] == 1
    # solid but not cheap is neutral
    neutral = call("fundamentals.safety_margin", statements=with_last(alfa, price=35, current_assets=700), as_of=AS_OF)
    assert neutral.value["lean"] == 0
    # a loss disqualifies: negative with medium confidence, not an abstention; P/E does not exist
    loss = call("fundamentals.safety_margin", statements=bravo, as_of=AS_OF)
    assert loss.value["lean"] == -1 and loss.value["disqualified"] is True and by_name(loss)["price_to_earnings"]["value"] is None
    assert by_name(loss)["price_to_earnings"]["passed"] is None
    assert call("fundamentals.safety_margin", statements=charlie, as_of=AS_OF).value is None
    no_book = call("fundamentals.safety_margin", statements=alfa.drop(columns=["total_equity"]), as_of=AS_OF)
    assert no_book.value is None and "data_missing:book_value" in no_book.warnings
    assert call("fundamentals.safety_margin", statements=alfa.iloc[0:0]).value is None
    assert "dividend record of twenty years" in res.value["cannot_judge"]
    assert "INV-003" in piece_ids("fundamentals.safety_margin")


# ---------------------------------------------------------------------------------------------------------------------
def test_peg_thresholds_SHF_009():
    # below 1 adds, above 2 subtracts; 1 and 2 themselves add nothing
    assert [criteria.peg_points(p) for p in (0.5, 0.99, 1.0, 1.01, 1.5, 1.99, 2.0, 2.01, 5.0)] == [1, 1, 0, 0, 0, 0, 0, -1, -1]
    assert criteria.peg_points(None) == 0 and criteria.peg_points(float("nan")) == 0
    assert criteria.PEG_BULLISH_BELOW == 1.0 and criteria.PEG_BEARISH_ABOVE == 2.0
    assert "SHF-009" in piece_ids("fundamentals.fair_growth")


def test_fair_growth_INV_004(alfa, bravo, charlie):
    res = call("fundamentals.fair_growth", statements=alfa, as_of=AS_OF)
    check_contract(res)
    eps = [152 / 100, 180 / 100, 208.8 / 99, 237.6 / 98]
    cagr = (eps[-1] / eps[0]) ** (1 / 3) - 1
    pe = 60 / eps[-1]
    assert res.value["category"] == "stalwart" and res.value["cyclical"] is False
    assert res.value["peg"] == pytest.approx(pe / (cagr * 100), abs=TOL) and res.value["peg_points"] == 0 and res.value["lean"] == 0
    assert by_name(res)["eps_growth_cagr"]["value"] == pytest.approx(cagr, abs=TOL)
    assert "growth between 12 and 20 percent" in res.params["category_band_assumption"]
    # PEG under 1 adds, over 2 subtracts, with the price moved
    assert call("fundamentals.fair_growth", statements=with_last(alfa, price=30), as_of=AS_OF).value["lean"] == 1
    assert call("fundamentals.fair_growth", statements=with_last(alfa, price=100), as_of=AS_OF).value["lean"] == -1
    cheap_but_levered = with_last(alfa, price=30, long_term_debt=900)
    assert call("fundamentals.fair_growth", statements=cheap_but_levered, as_of=AS_OF).value["lean"] == 0          # debt does not support it
    # a fast grower (25 percent) and a slow one
    fast = alfa.copy()
    fast.loc[fast["filed"] <= AS_OF, "net_income"] = [100.0, 125.0, 156.25, 195.3125]
    f = call("fundamentals.fair_growth", statements=fast, as_of=AS_OF)
    assert f.value["category"] == "fast"
    slow = alfa.copy()
    slow.loc[slow["filed"] <= AS_OF, "net_income"] = [100.0, 103.0, 106.0, 109.0]
    s = call("fundamentals.fair_growth", statements=with_last(slow, price=5), as_of=AS_OF)
    assert s.value["category"] == "slow" and s.value["lean"] <= 0
    # insiders and institutional ownership count only if the data arrives
    assert res.value["not_available"] == ["insider_transactions", "institutional_ownership"]
    with_data = call("fundamentals.fair_growth", statements=alfa, as_of=AS_OF, insider_net_buying=1500.0, institutional_ownership=0.62)
    assert with_data.value["not_available"] == [] and by_name(with_data)["insider_net_buying"]["passed"] is True
    assert by_name(with_data)["institutional_ownership"]["value"] == 0.62
    # without a PEG the profile abstains: a turnaround, a loss or a base near zero
    turn = alfa.copy()
    turn.loc[turn["filed"] <= AS_OF, "net_income"] = [-20.0, 10.0, 60.0, 120.0]
    t = call("fundamentals.fair_growth", statements=turn, as_of=AS_OF)
    assert t.value is None and "data_missing:peg" in t.warnings and "peg_not_defined:turnaround_has_no_peg" in t.warnings
    near_zero = alfa.copy()
    near_zero.loc[near_zero["filed"] <= AS_OF, "net_income"] = [1.0, 50.0, 100.0, 237.6]
    assert "peg_not_defined:base_near_zero" in call("fundamentals.fair_growth", statements=near_zero, as_of=AS_OF).warnings
    assert "data_missing:peg" in call("fundamentals.fair_growth", statements=bravo, as_of=AS_OF).warnings
    assert call("fundamentals.fair_growth", statements=charlie, as_of=AS_OF).value is None
    assert call("fundamentals.fair_growth", statements=alfa.iloc[0:0]).value is None
    with pytest.raises(BadInput):
        call("fundamentals.fair_growth", statements=alfa, as_of=AS_OF, periods_per_year=0)
    with pytest.raises(BadInput):
        call("fundamentals.fair_growth", statements=alfa, as_of=AS_OF, institutional_ownership=1.5)
    assert "INV-004" in piece_ids("fundamentals.fair_growth")


# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture()
def accelerating(alfa):
    """Revenue that accelerates, a margin that improves and a price that has not moved: the inflection set up."""
    df = alfa.copy()
    rows = df["filed"] <= AS_OF
    df.loc[rows, "revenue"] = [1000.0, 1050.0, 1100.0, 1200.0]
    df.loc[rows, "operating_income"] = [150.0, 160.0, 170.0, 220.0]
    df.loc[rows, "cost_of_revenue"] = [600.0, 630.0, 660.0, 700.0]
    return df


def test_inflection_INV_005(alfa, accelerating, candles):
    plain = call("fundamentals.inflection", statements=alfa, candles=candles, regime="neutral", as_of=AS_OF)
    check_contract(plain)
    assert plain.value["lean"] == 0 and plain.value["default"] == "wait" and plain.value["reason_code"].startswith("wait:")
    c = by_name(plain)
    g = [1100 / 1000 - 1, 1200 / 1100 - 1, 1300 / 1200 - 1]
    assert c["revenue_growth_acceleration"]["value"] == pytest.approx(g[2] - g[1], abs=TOL)
    assert c["operating_margin_inflection"]["value"] == pytest.approx(305 / 1300 - 270 / 1200, abs=TOL)
    bars = candles[candles.index <= pd.Timestamp(AS_OF)]
    assert plain.value["bars_used"] == len(bars) and plain.value["last_bar"] == str(bars.index[-1])[:10]
    assert c["price_move_63_bars"]["value"] == pytest.approx(bars["close"].iloc[-1] / bars["close"].iloc[-64] - 1, abs=TOL)
    assert c["price_vs_sma_200"]["value"] == pytest.approx(bars["close"].iloc[-1] / bars["close"].iloc[-200:].mean() - 1, abs=TOL)
    # the candles are cut at the date: bars after it change nothing
    future = candles.copy()
    future.loc[future.index > pd.Timestamp(AS_OF), ["open", "high", "low", "close"]] = 1.0
    same = call("fundamentals.inflection", statements=alfa, candles=future, regime="neutral", as_of=AS_OF)
    assert same.value["criteria"] == plain.value["criteria"]
    up = call("fundamentals.inflection", statements=accelerating, candles=candles, regime="neutral", as_of=AS_OF)
    uc = by_name(up)
    assert uc["revenue_growth_acceleration"]["passed"] is True and uc["operating_margin_inflection"]["passed"] is True
    assert uc["multiple_vs_own_history"]["passed"] is True and uc["price_move_63_bars"]["passed"] is True
    assert up.value["lean"] == 1 and up.value["reason_code"] == "inflection_up_not_yet_recognized"
    hostile = call("fundamentals.inflection", statements=accelerating, candles=candles, regime="hostile", as_of=AS_OF)
    assert hostile.value["lean"] == 0 and hostile.value["reason_code"] == "wait:regime_is_hostile"          # the regime vetoes the positive view
    # a trend that the price already recognized: wait
    run_up = candles.copy()
    run_up.loc[run_up.index <= pd.Timestamp(AS_OF), ["open", "high", "low", "close"]] = candles.loc[candles.index <= pd.Timestamp(AS_OF), ["open", "high", "low", "close"]].to_numpy()
    n = int((candles.index <= pd.Timestamp(AS_OF)).sum())
    ramp = np.linspace(1.0, 1.5, 64)
    for col in ("open", "high", "low", "close"):
        run_up.iloc[n - 64:n, run_up.columns.get_loc(col)] = candles[col].iloc[n - 64] * ramp
    ru = call("fundamentals.inflection", statements=accelerating, candles=run_up, regime="neutral", as_of=AS_OF)
    assert by_name(ru)["price_move_63_bars"]["passed"] is False and ru.value["lean"] == 0
    # deterioration at a multiple that still assumes the old path is negative
    down = alfa.copy()
    rows = down["filed"] <= AS_OF
    down.loc[rows, "revenue"] = [1000.0, 1100.0, 1250.0, 1300.0]
    down.loc[rows, "operating_income"] = [200.0, 235.0, 290.0, 280.0]
    dn = call("fundamentals.inflection", statements=with_last(down, price=75), candles=candles, regime="neutral", as_of=AS_OF)
    assert by_name(dn)["revenue_growth_acceleration"]["passed"] is False
    assert by_name(dn)["multiple_vs_own_history"]["value"] > 0
    assert dn.value["lean"] == -1 and dn.value["reason_code"] == "inflection_down_priced_for_the_old_path"
    # abstentions: no regime, too few candles, inconsistent candles, short statements
    no_regime = call("fundamentals.inflection", statements=alfa, candles=candles, regime=None, as_of=AS_OF)
    assert no_regime.value is None and "data_missing:regime" in no_regime.warnings
    few = call("fundamentals.inflection", statements=alfa, candles=candles.iloc[:100], regime="neutral", as_of=AS_OF)
    assert few.value is None and any(w.startswith("data_missing:candles_at_least_200") for w in few.warnings)
    broken = candles.copy()
    broken.iloc[300, broken.columns.get_loc("high")] = broken.iloc[300]["low"] - 1
    assert call("fundamentals.inflection", statements=alfa, candles=broken, regime="neutral", as_of=AS_OF).warnings[-1] == "candles_inconsistent"
    assert call("fundamentals.inflection", statements=alfa.iloc[:2], candles=candles, regime="neutral", as_of=AS_OF).value is None
    assert call("fundamentals.inflection", statements=alfa.iloc[0:0], candles=candles, regime="neutral", as_of=AS_OF).value is None
    with pytest.raises(BadInput):
        call("fundamentals.inflection", statements=alfa, candles=candles, regime="stormy", as_of=AS_OF)
    with pytest.raises(BadInput):
        call("fundamentals.inflection", statements=alfa, candles=candles.reset_index(drop=True), regime="neutral", as_of=AS_OF)
    with pytest.raises(BadInput):
        call("fundamentals.inflection", statements=alfa, candles=candles, regime="neutral", as_of=None)
    assert "central bank policy" in plain.value["cannot_judge"] and "how much to commit" in plain.value["cannot_judge"]
    assert plain.value["confidence_cap"] == 89
    assert "INV-005" in piece_ids("fundamentals.inflection")


# ---------------------------------------------------------------------------------------------------------------------
def test_contrarian_gap_and_catalyst_INV_006(alfa, bravo, charlie):
    cheap = with_last(alfa, price=15)                                # market cap 1,470 against a value of 2,408.57: gap 39 percent
    gap = 1 - 15 * 98 / INTRINSIC
    dated = call("fundamentals.contrarian_gap", statements=cheap, as_of=AS_OF, catalyst={"date": "2024-09-01", "description": "approval decision"})
    check_contract(dated)
    c = by_name(dated)
    assert c["value_gap"]["value"] == pytest.approx(gap, abs=TOL) and gap > 0.30 and c["value_gap"]["passed"] is True
    assert c["catalyst_days_ahead"]["value"] == 63.0 and dated.value["catalyst_valid"] is True and dated.value["lean"] == 1
    # without a valid dated catalyst the lean cannot be positive: absent, past or missing date
    for catalyst in (None, {"date": "2024-05-01", "description": "already happened"}):
        res = call("fundamentals.contrarian_gap", statements=cheap, as_of=AS_OF, catalyst=catalyst)
        assert res.value["lean"] == 0 and res.value["catalyst_valid"] is False and res.value["reason_code"] == "value_gap_without_a_valid_dated_catalyst"
    assert call("fundamentals.contrarian_gap", statements=cheap, as_of=AS_OF).value["data_missing"] == ["catalyst"]
    same_day = call("fundamentals.contrarian_gap", statements=cheap, as_of=AS_OF, catalyst={"date": AS_OF})
    assert same_day.value["catalyst_valid"] is True and same_day.value["lean"] == 1
    # a gap under 30 percent is not enough even with a catalyst
    thin = call("fundamentals.contrarian_gap", statements=with_last(alfa, price=20), as_of=AS_OF, catalyst={"date": "2024-09-01"})
    assert by_name(thin)["value_gap"]["value"] == pytest.approx(1 - 20 * 98 / INTRINSIC, abs=TOL)
    assert thin.value["value_gap"] < 0.30 and thin.value["lean"] == 0
    # the negative side: price over twice the value
    bubble = call("fundamentals.contrarian_gap", statements=alfa, as_of=AS_OF, catalyst={"date": "2024-09-01"})
    assert bubble.value["lean"] == -1 and bubble.value["reason_code"] == "overvalued_or_bubble_conditions"
    assert by_name(bubble)["price_to_value"]["value"] == pytest.approx(5880 / INTRINSIC, abs=TOL)
    # the value is computed by code from owner earnings, with the assumptions printed
    assert dated.params["discount_rate"] == 0.10 and dated.params["gap_min"] == 0.30
    assert "owner earnings" in dated.params["formula"]
    # no valuation, no value
    assert call("fundamentals.contrarian_gap", statements=with_last(alfa, net_income=-100), as_of=AS_OF).value is None
    assert "data_missing:positive_owner_earnings" in call("fundamentals.contrarian_gap", statements=with_last(alfa, net_income=-100), as_of=AS_OF).warnings
    assert call("fundamentals.contrarian_gap", statements=alfa.drop(columns=["price"]), as_of=AS_OF).value is None
    assert call("fundamentals.contrarian_gap", statements=charlie, as_of=AS_OF).value is None
    assert call("fundamentals.contrarian_gap", statements=alfa.iloc[0:0]).value is None
    for bad in ({"description": "no date"}, {"date": "tomorrow-ish"}, "2024-09-01", {"date": "2024-09-01", "description": "x" * 400}):
        with pytest.raises(BadInput):
            call("fundamentals.contrarian_gap", statements=cheap, as_of=AS_OF, catalyst=bad)
    assert "notes of the annual report" in dated.value["cannot_judge"]
    assert "INV-006" in piece_ids("fundamentals.contrarian_gap")


def test_catalyst_is_a_dated_field_SHF_008(alfa):
    cheap = with_last(alfa, price=15)
    results = {}
    for label, catalyst in (("future", {"date": "2025-01-15"}), ("past", {"date": "2023-01-15"}), ("absent", None)):
        r = call("fundamentals.contrarian_gap", statements=cheap, as_of=AS_OF, catalyst=catalyst)
        results[label] = (r.value["lean"], r.value["catalyst_valid"])
    assert results == {"future": (1, True), "past": (0, False), "absent": (0, False)}       # a past or absent date cancels the positive side
    assert by_name(call("fundamentals.contrarian_gap", statements=cheap, as_of=AS_OF, catalyst={"date": "2023-01-15"}))["catalyst_days_ahead"]["passed"] is False
    assert by_name(call("fundamentals.contrarian_gap", statements=cheap, as_of=AS_OF))["catalyst_days_ahead"]["value"] is None
    assert "SHF-008" in piece_ids("fundamentals.contrarian_gap")


# ---------------------------------------------------------------------------------------------------------------------
def test_disruption_INV_007(alfa, bravo, charlie):
    themes = criteria.load_themes()
    assert themes["NVDA"] == "artificial_intelligence" and themes["CRSP"] == "genomics" and set(themes.values()) <= set(criteria.THEME_NAMES)
    open_ = call("fundamentals.disruption", statements=alfa, symbol="NVDA", as_of=AS_OF)
    check_contract(open_)
    assert open_.value["theme"] == "artificial_intelligence"
    # the theme comes from the curated table, not from the model: a ticker that is not in it gives no value
    unknown = call("fundamentals.disruption", statements=alfa, symbol="ZZZZ", as_of=AS_OF)
    assert unknown.value is None and "data_missing:theme_not_in_the_curated_table" in unknown.warnings
    assert call("fundamentals.disruption", statements=alfa, symbol="nvda", as_of=AS_OF).value["theme"] == "artificial_intelligence"
    # market size, first mover and team are declared unknown and the confidence never passes 69
    assert open_.value["tam"] == "unknown" and open_.value["first_mover"] == "unknown" and open_.value["team_vision"] == "unknown"
    assert open_.value["confidence_cap"] == 69 == criteria.RUTH_CONFIDENCE_CAP
    assert open_.value["data_missing"] == ["tam", "first_mover", "team_vision"]
    c = by_name(open_)
    assert c["rd_intensity"]["value"] == pytest.approx(65 / 1300, abs=TOL)
    assert c["revenue_growth_latest"]["value"] == pytest.approx(1300 / 1200 - 1, abs=TOL)
    assert c["revenue_growth_acceleration"]["value"] == pytest.approx((1300 / 1200 - 1) - (1200 / 1100 - 1), abs=TOL)
    assert c["gross_margin_change"]["value"] == pytest.approx(550 / 1300 - 400 / 1000, abs=TOL)
    assert c["cash_runway_months"]["value"] == 999.0                              # free cash flow is positive: no burn
    assert c["dilution_per_period"]["value"] == pytest.approx((98 / 100) ** (1 / 3) - 1, abs=TOL)
    assert c["price_to_sales"]["value"] == pytest.approx(5880 / 1300, abs=TOL)
    # thresholds that no source gives are parameters: without them the profile is neutral and says so
    assert open_.value["lean"] == 0 and open_.value["reason_code"] == "thresholds_not_calibrated" and open_.params["thresholds_calibrated"] is False
    assert c["rd_intensity"]["threshold"] is None and c["rd_intensity"]["passed"] is None
    limits = dict(rd_intensity_min=0.04, runway_months_min=24, dilution_max=0.05, revenue_accel_min=-0.01)
    up = call("fundamentals.disruption", statements=alfa, symbol="NVDA", as_of=AS_OF, **limits)
    assert up.value["lean"] == 1 and up.value["confidence_cap"] == 69 and up.params["thresholds_calibrated"] is True
    # a short cash runway with dilution is negative
    burning = bravo.copy()
    burning["rd_expenses"] = burning["revenue"] * 0.1
    burning["gross_profit"] = burning["revenue"] - burning["cost_of_revenue"]
    down = call("fundamentals.disruption", statements=burning, symbol="COIN", as_of=AS_OF, **limits)
    assert by_name(down)["cash_runway_months"]["value"] == pytest.approx(20 / (25 / 12), abs=TOL)
    assert by_name(down)["dilution_per_period"]["value"] == pytest.approx((60 / 50) ** (1 / 3) - 1, abs=TOL)
    assert down.value["lean"] == -1 and down.value["reason_code"] == "short_cash_runway_with_dilution"
    # no R&D line, a short series or an empty frame: no value
    no_rd = call("fundamentals.disruption", statements=bravo, symbol="COIN", as_of=AS_OF)
    assert no_rd.value is None and "data_missing:rd_expenses" in no_rd.warnings
    assert call("fundamentals.disruption", statements=charlie, symbol="NVDA", as_of=AS_OF).value is None
    assert call("fundamentals.disruption", statements=alfa.iloc[0:0], symbol="NVDA").value is None
    for bad in ("", "../etc", "N V", "TOOLONGTICKER1"):
        with pytest.raises(BadInput):
            call("fundamentals.disruption", statements=alfa, symbol=bad, as_of=AS_OF)
    with pytest.raises(BadInput):
        call("fundamentals.disruption", statements=alfa, symbol="NVDA", as_of=AS_OF, rd_intensity_min=float("nan"))
    assert "INV-007" in piece_ids("fundamentals.disruption")


def test_disruption_rules_do_not_invent_SHF_007(alfa):
    res = call("fundamentals.disruption", statements=alfa.drop(columns=["rd_expenses"]), symbol="NVDA", as_of=AS_OF)
    assert res.value is None and res.warnings[-1] == "data_missing:rd_expenses"        # R&D that does not exist in the data is not guessed
    ok = call("fundamentals.disruption", statements=alfa, symbol="PLTR", as_of=AS_OF)
    assert {"tam", "first_mover", "team_vision"} == set(ok.value["data_missing"]) and set(ok.value["cannot_judge"]) == {
        "total addressable market", "first mover advantage", "vision of the team"}
    assert "SHF-007" in piece_ids("fundamentals.disruption")
