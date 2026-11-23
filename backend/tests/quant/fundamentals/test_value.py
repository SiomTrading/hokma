"""FT-013 to FT-018: DuPont, cost of capital, EVA, intrinsic value, enterprise value breakdown and growth."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _lib

from .conftest import AS_OF, call, latest, piece_ids

TOL = 1e-8


def test_dupont_FT_013(alfa):
    three = call("fundamentals.dupont", statements=alfa, as_of=AS_OF)
    margin, turnover, multiplier = 237.6 / 1300, 1300 / 1825, 1825 / 1095
    v = three.value["latest"]
    assert v["Net Profit Margin"] == pytest.approx(margin, abs=TOL)
    assert v["Asset Turnover"] == pytest.approx(turnover, abs=TOL)
    assert v["Equity Multiplier"] == pytest.approx(multiplier, abs=TOL)
    assert v["Return on Equity"] == pytest.approx(margin * turnover * multiplier, abs=TOL) == pytest.approx(237.6 / 1095, abs=TOL)
    ext = call("fundamentals.dupont", statements=alfa, as_of=AS_OF, method="extended").value["latest"]
    parts = (297 / 305) * (237.6 / 297) * (305 / 1300) * turnover * multiplier
    assert ext["Interest Burden Ratio"] == pytest.approx(297 / 305, abs=TOL)
    assert ext["Tax Burden Ratio"] == pytest.approx(237.6 / 297, abs=TOL)
    assert ext["Return on Equity"] == pytest.approx(parts, abs=TOL) == pytest.approx(v["Return on Equity"], abs=TOL)
    nissim = call("fundamentals.dupont", statements=alfa, as_of=AS_OF, method="nissim_penman")
    assert nissim.value["return_on_equity_latest"] == pytest.approx(237.6 / 1095, abs=TOL)       # the decomposition adds up to the same ROE
    assert nissim.value["latest"]["Spread"] == pytest.approx(nissim.value["latest"]["Return on Net Operating Assets"] - nissim.value["latest"]["Net Borrowing Cost"], abs=TOL)
    df = alfa[alfa["filed"] <= AS_OF]
    origin = _lib.ft_dupont.get_dupont_analysis(df["net_income"], df["revenue"], (df["total_assets"] + df["total_assets"].shift(1)) / 2,
                                                (df["total_equity"] + df["total_equity"].shift(1)) / 2)
    assert v["Return on Equity"] == pytest.approx(origin.loc["Return on Equity"].iloc[-1], abs=TOL)
    assert call("fundamentals.dupont", statements=alfa.drop(columns=["revenue"]), as_of=AS_OF).warnings[-1] == "data_missing:revenue"
    assert call("fundamentals.dupont", statements=alfa.iloc[0:0]).value is None
    with pytest.raises(BadInput):
        call("fundamentals.dupont", statements=alfa, method="five")
    assert "FT-013" in piece_ids("fundamentals.dupont")


def test_cost_of_capital_FT_014(alfa):
    coe = call("fundamentals.cost_of_equity", risk_free_rate=0.04, beta=1.2, market_return=0.09)
    assert coe.value["cost_of_equity"] == pytest.approx(0.04 + 1.2 * (0.09 - 0.04), abs=TOL)
    assert _lib.ft_performance.get_capital_asset_pricing_model(0.04, 1.2, 0.09) == pytest.approx(coe.value["cost_of_equity"], abs=TOL)
    assert call("fundamentals.cost_of_equity", risk_free_rate=5.0, beta=1.0, market_return=0.09).value is None        # percent instead of fraction
    with pytest.raises(BadInput):
        call("fundamentals.cost_of_equity", risk_free_rate="4%", beta=1.0, market_return=0.09)
    with pytest.raises(BadInput):
        call("fundamentals.cost_of_equity", risk_free_rate=float("nan"), beta=1.0, market_return=0.09)
    cod = call("fundamentals.cost_of_debt", statements=alfa, as_of=AS_OF)
    assert cod.value["latest"]["cost_of_debt"] == pytest.approx(8 / 260, abs=TOL)
    assert cod.value["latest"]["after_tax_cost_of_debt"] == pytest.approx(8 / 260 * 0.8, abs=TOL)
    zero_interest = alfa.copy()
    zero_interest["interest_expense"] = 0.0
    assert call("fundamentals.cost_of_debt", statements=zero_interest, as_of=AS_OF).value is None        # zero interest against debt is not reported
    res = call("fundamentals.wacc", statements=alfa, as_of=AS_OF, risk_free_rate=0.04, beta=1.2, market_return=0.09)
    hand = (5880 / 6140) * 0.10 + (260 / 6140) * (8 / 260) * 0.8
    assert latest(res, "wacc") == pytest.approx(hand, abs=TOL)
    df = alfa[alfa["filed"] <= AS_OF]
    idx = df.index
    origin = _lib.ft_wacc.get_weighted_average_cost_of_capital(
        df["price"], df["shares_outstanding"], df["interest_expense"], df["short_term_debt"] + df["long_term_debt"], pd.Series(0.04, index=idx),
        pd.Series(1.2, index=idx), pd.Series(0.09, index=idx), df["income_tax_expense"], df["income_before_tax"])
    assert res.value["series"]["wacc"] == pytest.approx(origin.loc["Weighted Average Cost of Capital"].tolist(), abs=TOL)
    # without debt the WACC is the cost of equity
    debt_free = alfa.copy()
    debt_free[["short_term_debt", "long_term_debt", "interest_expense"]] = 0.0
    assert latest(call("fundamentals.wacc", statements=debt_free, as_of=AS_OF, risk_free_rate=0.04, beta=1.2, market_return=0.09), "wacc") == pytest.approx(0.10, abs=TOL)
    assert call("fundamentals.wacc", statements=alfa.drop(columns=["price"]), as_of=AS_OF, risk_free_rate=0.04, beta=1.2, market_return=0.09).value is None
    assert call("fundamentals.wacc", statements=alfa.iloc[0:0], risk_free_rate=0.04, beta=1.2, market_return=0.09).value is None
    assert all("FT-014" in piece_ids(f"fundamentals.{n}") for n in ("cost_of_equity", "cost_of_debt", "wacc"))


def test_eva_FT_015(alfa):
    res = call("fundamentals.eva", statements=alfa, as_of=AS_OF, wacc=0.08)
    v = res.value["latest"]
    assert v["nopat"] == pytest.approx(305 * 0.8, abs=TOL)
    assert v["invested_capital"] == pytest.approx(1160 + 260, abs=TOL)
    assert v["eva"] == pytest.approx(244 - 0.08 * 1420, abs=TOL)
    assert v["mva"] == pytest.approx(5880 + 260 - 1420, abs=TOL)
    assert _lib.ft_eva.get_economic_value_added(244, 0.08, 1420) == pytest.approx(v["eva"], abs=TOL)
    with pytest.raises(BadInput):
        call("fundamentals.eva", statements=alfa, wacc=3.0)
    with pytest.raises(BadInput):
        call("fundamentals.eva", statements=alfa, wacc="8%")
    assert call("fundamentals.eva", statements=alfa.drop(columns=["total_equity"]), as_of=AS_OF, wacc=0.08).value is None
    assert call("fundamentals.eva", statements=alfa.iloc[0:0], wacc=0.08).value is None
    assert "FT-015" in piece_ids("fundamentals.eva")


def test_intrinsic_FT_016():
    def val(method, **p):
        return call("fundamentals.intrinsic", method=method, params=p)

    assert val("gordon", dividends_per_share=2, rate_of_return=0.08, growth_rate=0.03).value["value"] == pytest.approx(2 * 1.03 / 0.05, abs=TOL)
    assert val("growth_multiple", earnings_per_share=2, growth_rate=0.1, bond_yield=0.044).value["value"] == pytest.approx(2 * (8.5 + 20) * 4.4 / 4.4, abs=TOL)
    assert val("defensive_number", earnings_per_share=2, book_value_per_share=20).value["value"] == pytest.approx(math.sqrt(22.5 * 2 * 20), abs=TOL)
    assert val("fcff", nopat=100, depreciation_and_amortization=20, capex=-30, change_in_net_working_capital=10).value["value"] == pytest.approx(80, abs=TOL)
    assert val("fcfe", net_income=80, depreciation_and_amortization=20, capex=-30, change_in_net_working_capital=10, net_borrowing=5).value["value"] == pytest.approx(65, abs=TOL)
    assert val("residual_income", net_income=100, cost_of_equity=0.08, book_value_of_equity=800).value["value"] == pytest.approx(100 - 0.08 * 800, abs=TOL)
    h = val("h_model", dividends_per_share=1, rate_of_return=0.09, high_growth_rate=0.10, stable_growth_rate=0.03).value["value"]
    assert h == pytest.approx(1 * 1.03 / 0.06 + 1 * 5 * 0.07 / 0.06, abs=TOL)
    # two stage dividend model by hand: five years at 10 percent, then 3 percent forever, discounted at 9 percent
    divs = [1.1 ** k for k in range(1, 6)]
    pv = sum(d / 1.09 ** k for k, d in enumerate(divs, start=1)) + divs[-1] * 1.03 / 0.06 / 1.09 ** 5
    ddm = val("ddm_two_stage", dividends_per_share=1, rate_of_return=0.09, high_growth_rate=0.10, stable_growth_rate=0.03)
    assert ddm.value["value"] == pytest.approx(pv, abs=TOL) and ddm.value["detail"]["Final High-Growth Dividend"] == pytest.approx(1.1 ** 5, abs=TOL)
    # discounted cash flow by hand: five years of 5 percent growth, terminal at 2 percent, 8 percent rate, plus cash, minus debt, per share
    flows = [100 * 1.05 ** k for k in range(1, 6)]
    ev = sum(f / 1.08 ** k for k, f in enumerate(flows, start=1)) + flows[-1] * 1.02 / 0.06 / 1.08 ** 5
    dcf = val("dcf", cash_flow=100, growth_rate=0.05, perpetual_growth_rate=0.02, wacc=0.08, cash=50, total_debt=30, shares_outstanding=10)
    assert dcf.value["value"] == pytest.approx((ev + 50 - 30) / 10, abs=TOL) and dcf.value["detail"]["Enterprise Value"] == pytest.approx(ev, abs=TOL)
    # a rate at or below the growth rate has no finite value
    for method, p in (("gordon", dict(dividends_per_share=2, rate_of_return=0.03, growth_rate=0.03)),
                      ("dcf", dict(cash_flow=100, growth_rate=0.05, perpetual_growth_rate=0.09, wacc=0.08, cash=1, total_debt=1, shares_outstanding=1)),
                      ("h_model", dict(dividends_per_share=1, rate_of_return=0.02, high_growth_rate=0.1, stable_growth_rate=0.03)),
                      ("defensive_number", dict(earnings_per_share=-2, book_value_per_share=20)),
                      ("growth_multiple", dict(earnings_per_share=2, growth_rate=0.1, bond_yield=0.0))):
        res = val(method, **p)
        assert res.value is None and res.warnings, method
    assert val("gordon", dividends_per_share=2).warnings == ("data_missing:rate_of_return", "data_missing:growth_rate")
    with pytest.raises(BadInput):
        call("fundamentals.intrinsic", method="made_up", params={})
    with pytest.raises(BadInput):
        call("fundamentals.intrinsic", method="dcf", params={"cash_flow": "a lot"})
    with pytest.raises(BadInput):
        call("fundamentals.intrinsic", method="dcf", params=dict(cash_flow=100, growth_rate=0.05, perpetual_growth_rate=0.02, wacc=0.08, cash=1,
                                                              total_debt=1, shares_outstanding=1, periods=10_000))
    with pytest.raises(BadInput):
        val("gordon", dividends_per_share=float("nan"), rate_of_return=0.08, growth_rate=0.03)
    assert "FT-016" in piece_ids("fundamentals.intrinsic")


def test_ev_breakdown_and_tobins_q_FT_017(alfa):
    res = call("fundamentals.ev_breakdown", statements=alfa, as_of=AS_OF)
    v = res.value["latest"]
    assert v["Market Capitalization"] == pytest.approx(5880, abs=TOL)
    assert v["Total Debt"] == pytest.approx(260, abs=TOL) and v["Cash and Cash Equivalents"] == pytest.approx(230, abs=TOL)
    assert v["Short Term Investments"] == pytest.approx(20, abs=TOL)
    assert v["Enterprise Value"] == pytest.approx(5880 + 260 - 250, abs=TOL)
    assert v["Tobin Q"] == pytest.approx((5880 + 740) / 1900, abs=TOL)
    assert _lib.ft_enterprise.get_tobins_q_ratio(5880, 740, 1900) == pytest.approx(v["Tobin Q"], abs=TOL)
    assert call("fundamentals.ev_breakdown", statements=alfa.drop(columns=["price"]), as_of=AS_OF).warnings[-1] == "data_missing:price"
    assert call("fundamentals.ev_breakdown", statements=alfa.iloc[0:0]).value is None
    assert "FT-017" in piece_ids("fundamentals.ev_breakdown")


def test_growth_and_pvgo_FT_018(alfa):
    res = call("fundamentals.growth", statements=alfa, as_of=AS_OF, cost_of_equity=0.08)
    roe, roa, retention = 237.6 / 1095, 237.6 / 1825, 1 - 55 / 237.6
    v = res.value["latest"]
    assert v["sustainable_growth_rate"] == pytest.approx(roe * retention, abs=TOL)
    assert v["internal_growth_rate"] == pytest.approx(roa * retention / (1 - roa * retention), abs=TOL)
    assert v["pvgo"] == pytest.approx(60 - (237.6 / 98) / 0.08, abs=TOL)
    assert _lib.ft_growth.get_sustainable_growth_rate(roe, retention) == pytest.approx(v["sustainable_growth_rate"], abs=TOL)
    no_coe = call("fundamentals.growth", statements=alfa, as_of=AS_OF)
    assert "pvgo" not in no_coe.value["latest"]
    assert call("fundamentals.growth", statements=alfa.drop(columns=["dividends_paid"]), as_of=AS_OF).value is None
    assert call("fundamentals.growth", statements=alfa.iloc[0:0]).value is None
    with pytest.raises(BadInput):
        call("fundamentals.growth", statements=alfa, cost_of_equity=0.0)
    with pytest.raises(BadInput):
        call("fundamentals.growth", statements=alfa, cost_of_equity="high")
    assert "FT-018" in piece_ids("fundamentals.growth")
