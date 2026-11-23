"""FM-13 to FM-16 and FM-18: formulas read by the desks, and the TTM that reuses the routine of SVC-data."""
import numpy as np
import pandas as pd
import pytest

from hokma.data import _lib as data_lib
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _lib

from .conftest import AS_OF, call, latest, piece_ids, with_last

TOL = 1e-8


def test_enterprise_value_FM_13(alfa):
    res = call("fundamentals.enterprise_value", statements=alfa, as_of=AS_OF)
    # market cap 60 x 98 shares + debt (40 + 220) + minority 0 + preferred 0 - (cash 230 + short term investments 20)
    assert latest(res, "enterprise_value") == pytest.approx(5880 + 260 + 0 + 0 - 250, abs=TOL)
    assert res.value["series"]["enterprise_value"] == pytest.approx([3150, 4120, 5000, 5890], abs=TOL)
    df = alfa[alfa["filed"] <= AS_OF]
    origin = _lib.ft_valuation.get_enterprise_value(df["price"] * df["shares_outstanding"], df["short_term_debt"] + df["long_term_debt"],
                                                    df["minority_interest"], df["preferred_equity"], df["cash"] + df["short_term_investments"])
    assert res.value["series"]["enterprise_value"] == pytest.approx(origin.tolist(), abs=TOL)
    # minority interest and preferred equity are added when they exist
    heavy = with_last(alfa, minority_interest=50, preferred_equity=30)
    assert latest(call("fundamentals.enterprise_value", statements=heavy, as_of=AS_OF), "enterprise_value") == pytest.approx(5890 + 80, abs=TOL)
    # without those lines the sum goes without them and says so
    plain = call("fundamentals.enterprise_value", statements=alfa.drop(columns=["minority_interest", "preferred_equity"]), as_of=AS_OF)
    assert latest(plain, "enterprise_value") == pytest.approx(5890, abs=TOL)
    assert {"assumed_zero:minority_interest", "assumed_zero:preferred_equity"} <= set(plain.warnings)
    # a missing required line gives no value and names it
    nocash = call("fundamentals.enterprise_value", statements=alfa.drop(columns=["cash", "short_term_investments"]), as_of=AS_OF)
    assert nocash.value is None and "data_missing:cash" in nocash.warnings
    assert call("fundamentals.enterprise_value", statements=alfa.iloc[0:0]).value is None
    assert call("fundamentals.enterprise_value", statements=pd.DataFrame({"price": [np.nan]}, index=["x"])).value is None
    with pytest.raises(BadInput):
        call("fundamentals.enterprise_value", statements="not a frame")
    assert "FM-13" in piece_ids("fundamentals.enterprise_value")


def test_buyback_yield_FM_14(alfa):
    res = call("fundamentals.buyback_yield", statements=alfa, as_of=AS_OF)
    assert latest(res, "buyback_yield") == pytest.approx((30 - 5) / 5880, abs=TOL)
    assert res.value["series"]["buyback_yield"][0] == pytest.approx((0 - 5) / 3000, abs=TOL)       # issuance above buybacks: negative
    df = alfa[alfa["filed"] <= AS_OF]
    origin = _lib.ft_valuation.get_buyback_yield(df["repurchases"], df["issuance"], df["price"] * df["shares_outstanding"])
    assert res.value["series"]["buyback_yield"] == pytest.approx(origin.tolist(), abs=TOL)
    flipped = alfa.copy()
    flipped["repurchases"] = flipped["repurchases"].abs()                 # a source that reports repurchases as positive
    assert latest(call("fundamentals.buyback_yield", statements=flipped, as_of=AS_OF), "buyback_yield") == pytest.approx((30 - 5) / 5880, abs=TOL)
    assert call("fundamentals.buyback_yield", statements=alfa.drop(columns=["repurchases"]), as_of=AS_OF).warnings[-1] == "data_missing:repurchases"
    assert call("fundamentals.buyback_yield", statements=alfa.iloc[0:0]).value is None
    assert "FM-14" in piece_ids("fundamentals.buyback_yield")


def test_fcf_after_sbc_FM_15(alfa):
    res = call("fundamentals.fcf_after_sbc", statements=alfa, as_of=AS_OF)
    assert latest(res, "fcf_after_sbc") == pytest.approx(290 - 75 - 16, abs=TOL)
    assert res.value["series"]["fcf_after_sbc"] == pytest.approx([200 - 60 - 10, 230 - 65 - 12, 260 - 70 - 14, 199], abs=TOL)
    df = alfa[alfa["filed"] <= AS_OF]
    origin = _lib.ft_valuation.get_sbc_adjusted_free_cash_flow(df["operating_cash_flow"] + df["capex"], df["sbc"])
    assert res.value["series"]["fcf_after_sbc"] == pytest.approx(origin.tolist(), abs=TOL)
    positive_capex = alfa.copy()
    positive_capex["capex"] = positive_capex["capex"].abs()
    assert latest(call("fundamentals.fcf_after_sbc", statements=positive_capex, as_of=AS_OF), "fcf_after_sbc") == pytest.approx(199, abs=TOL)
    nan_sbc = alfa.copy()
    nan_sbc["sbc"] = np.nan
    assert call("fundamentals.fcf_after_sbc", statements=nan_sbc, as_of=AS_OF).value is None
    assert "FM-15" in piece_ids("fundamentals.fcf_after_sbc")


def test_roic_simple_FM_16(alfa, bravo):
    res = call("fundamentals.roic_simple", statements=alfa, as_of=AS_OF)
    # NOPAT = 305 x (1 - 59.4 / 297) = 244 over equity 1160 plus debt 260 at the closing balance
    assert latest(res, "roic_simple") == pytest.approx(244 / 1420, abs=TOL)
    df = alfa[alfa["filed"] <= AS_OF]
    origin = _lib.ft_profitability.get_return_on_invested_capital(df["operating_income"], df["income_tax_expense"], df["income_before_tax"],
                                                                  df["total_equity"], df["short_term_debt"] + df["long_term_debt"])
    assert res.value["series"]["roic_simple"] == pytest.approx(origin.tolist(), abs=TOL)
    # a company with negative equity has no invested capital to speak of: no value, never a negative return on a negative base
    assert latest(call("fundamentals.roic_simple", statements=bravo, as_of=AS_OF), "roic_simple") is not None       # equity -130 plus debt 680 is positive
    broke = with_last(bravo, total_equity=-900)
    assert latest(call("fundamentals.roic_simple", statements=broke, as_of=AS_OF), "roic_simple") is None
    assert call("fundamentals.roic_simple", statements=alfa.iloc[0:0]).value is None
    assert "FM-16" in piece_ids("fundamentals.roic_simple")


def test_shareholder_yield_FT_006(alfa):
    res = call("fundamentals.shareholder_yield", statements=alfa, as_of=AS_OF)
    assert latest(res, "shareholder_yield") == pytest.approx(55 / 98 / 60 + (30 - 5) / 5880, abs=TOL)
    assert call("fundamentals.shareholder_yield", statements=alfa.drop(columns=["dividends_paid"]), as_of=AS_OF).value is None
    assert "FT-006" in piece_ids("fundamentals.shareholder_yield")


def test_ttm_FM_18(quarterly):
    facts = quarterly
    res = call("fundamentals.ttm", facts=facts, as_of="2024-06-30")
    # Q2 2023 (320) + Q3 2023 (335) + Q4 2023 derived as 1300 - 965 = 335 + Q1 2024 (340)
    assert res.value["value"] == pytest.approx(320 + 335 + 335 + 340, abs=TOL)
    assert res.value["window_end"] == "2024-03-31" and res.value["has_calculated_q4"] is True
    assert res.value["periods"] == ["2023-Q2", "2023-Q3", "2023-Q4", "2024-Q1"]
    # point in time: on 2024-04-30 the first quarter of 2024 is not filed yet
    early = call("fundamentals.ttm", facts=facts, as_of="2024-04-30")
    assert early.value["value"] == pytest.approx(310 + 320 + 335 + 335, abs=TOL) and early.value["window_end"] == "2023-12-31"
    # a window that ends more than 185 days before the cut-off is not today's number
    stale = call("fundamentals.ttm", facts=facts, as_of="2024-12-31")
    assert stale.value is None and any(w.startswith("ttm_window_ends_275_days_before_as_of") for w in stale.warnings)
    edge = call("fundamentals.ttm", facts=facts, as_of="2024-09-30")           # 183 days after 2024-03-31: still inside
    assert edge.value is not None and edge.value["lag_days"] == 183
    # not enough quarters, per share concepts and empty or NaN facts give no value
    assert call("fundamentals.ttm", facts=facts.iloc[:4], as_of="2022-12-31").value is None
    assert call("fundamentals.ttm", facts=facts, as_of="2024-06-30", unit="USD/share").value is None
    assert call("fundamentals.ttm", facts=facts.iloc[0:0], as_of="2024-06-30").value is None
    nan = facts.copy()
    nan["value"] = np.nan
    assert call("fundamentals.ttm", facts=nan, as_of="2024-06-30").value is None
    with pytest.raises(BadInput):
        call("fundamentals.ttm", facts=facts.drop(columns=["filed"]), as_of="2024-06-30")
    with pytest.raises(BadInput):
        call("fundamentals.ttm", facts=facts, as_of="not a date")
    with pytest.raises(BadInput):
        call("fundamentals.ttm", facts=facts, as_of="2024-06-30", concept="../../etc")
    # reused, not copied: the routine is the one of SVC-data (ED-026)
    assert _lib.sec_ttm is data_lib.sec_ttm
    assert "FM-18" in piece_ids("fundamentals.ttm")
