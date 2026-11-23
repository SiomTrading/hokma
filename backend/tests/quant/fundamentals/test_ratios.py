"""Ratio families FT-001 to FT-005 and the convention reader FT-006."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _lib, ratios

from .conftest import AS_OF, call, latest, piece_ids

TOL = 1e-8
DOCS = Path(__file__).resolve().parents[4] / "docs" / "quant" / "fundamentals-conventions.md"


def cut(df: pd.DataFrame) -> pd.DataFrame:
    """The four periods filed by the cut-off, as the module sees them."""
    return df[df["filed"] <= AS_OF].drop(columns=["filed"])


def test_profitability_FT_001(alfa):
    res = call("fundamentals.profitability", statements=alfa, as_of=AS_OF)
    v = res.value["latest"]
    # hand values of the last period filed by the cut-off (2023): the period of 2024 was filed later and must not count
    assert res.value["periods"][-1] == "2023-12-31" and "2024-06-30" not in res.value["periods"]
    assert v["gross_margin"] == pytest.approx(550 / 1300, abs=TOL)
    assert v["operating_margin"] == pytest.approx(305 / 1300, abs=TOL)
    assert v["net_profit_margin"] == pytest.approx(237.6 / 1300, abs=TOL)
    assert v["return_on_equity"] == pytest.approx(237.6 / ((1030 + 1160) / 2), abs=TOL)
    assert v["return_on_assets"] == pytest.approx(237.6 / ((1750 + 1900) / 2), abs=TOL)
    assert v["effective_tax_rate"] == pytest.approx(0.2, abs=TOL)
    assert v["ebitda_margin"] == pytest.approx((305 + 46) / 1300, abs=TOL)
    assert v["free_cash_flow_margin"] == pytest.approx((290 - 75) / 1300, abs=TOL)
    assert v["income_quality_ratio"] == pytest.approx(290 / 237.6, abs=TOL)
    assert len(v) >= 23
    # parity with the library, same inputs
    df = cut(alfa)
    avg_eq = (df["total_equity"] + df["total_equity"].shift(1)) / 2
    origin = _lib.ft_profitability.get_return_on_equity(df["net_income"], avg_eq)
    assert res.value["series"]["return_on_equity"][-1] == pytest.approx(origin.iloc[-1], abs=TOL)
    assert res.value["series"]["return_on_equity"][0] is None            # the first period has no average balance
    assert "FT-001" in piece_ids("fundamentals.profitability")
    assert "assumed_zero:long_term_investments" in res.warnings        # a line that is not reported is said, never silently zero


def test_ratio_families_reject_bad_input_FT_001(alfa):
    for fam in ("profitability", "liquidity", "efficiency", "solvency", "valuation"):
        name = f"fundamentals.{fam}"
        assert call(name, statements=alfa.iloc[0:0]).value is None
        assert call(name, statements=pd.DataFrame({"revenue": [np.nan, np.nan]}, index=["a", "b"])).value is None
        with pytest.raises(BadInput):
            call(name, statements=[1, 2, 3])
    with pytest.raises(BadInput):
        call("fundamentals.profitability", statements=alfa, ratios=["not_a_ratio"])
    only = call("fundamentals.profitability", statements=alfa, as_of=AS_OF, ratios=["gross_margin"])
    assert list(only.value["latest"]) == ["gross_margin"]
    no_price = call("fundamentals.valuation", statements=alfa.drop(columns=["price"]), as_of=AS_OF)
    assert "price_to_earnings_ratio" not in no_price.value["latest"]
    assert call("fundamentals.profitability", statements=alfa.drop(columns=["net_income"]), as_of=AS_OF, ratios=["return_on_equity"]).warnings


def test_liquidity_FT_002(alfa):
    v = call("fundamentals.liquidity", statements=alfa, as_of=AS_OF).value["latest"]
    assert v["current_ratio"] == pytest.approx(650 / 330, abs=TOL)
    assert v["quick_ratio"] == pytest.approx((230 + 20 + 150) / 330, abs=TOL)
    assert v["cash_ratio"] == pytest.approx((230 + 20) / 330, abs=TOL)
    assert v["working_capital"] == pytest.approx(320, abs=TOL)
    assert v["operating_cash_flow_ratio"] == pytest.approx(290 / 330, abs=TOL)
    df = cut(alfa)
    origin = _lib.ft_liquidity.get_current_ratio(df["current_assets"], df["current_liabilities"])
    assert v["current_ratio"] == pytest.approx(origin.iloc[-1], abs=TOL)
    assert len(v) >= 7 and "FT-002" in piece_ids("fundamentals.liquidity")


def test_efficiency_FT_003(alfa):
    v = call("fundamentals.efficiency", statements=alfa, as_of=AS_OF).value["latest"]
    assert v["asset_turnover_ratio"] == pytest.approx(1300 / 1825, abs=TOL)
    assert v["inventory_turnover_ratio"] == pytest.approx(750 / 112.5, abs=TOL)
    assert v["days_of_inventory_outstanding"] == pytest.approx(112.5 / 750 * 365, abs=TOL)
    assert v["capex_intensity"] == pytest.approx(75 / 1300, abs=TOL)
    assert v["research_and_development_ratio"] == pytest.approx(65 / 1300, abs=TOL)
    assert v["stock_based_compensation_ratio"] == pytest.approx(16 / 1300, abs=TOL)
    df = cut(alfa)
    origin = _lib.ft_efficiency.get_asset_turnover_ratio(df["revenue"], (df["total_assets"] + df["total_assets"].shift(1)) / 2)
    assert v["asset_turnover_ratio"] == pytest.approx(origin.iloc[-1], abs=TOL)
    assert len(v) >= 17 and "FT-003" in piece_ids("fundamentals.efficiency")


def test_solvency_FT_004(alfa):
    v = call("fundamentals.solvency", statements=alfa, as_of=AS_OF).value["latest"]
    assert v["debt_to_equity_ratio"] == pytest.approx(260 / 1160, abs=TOL)
    assert v["interest_coverage_ratio"] == pytest.approx(305 / 8, abs=TOL)
    assert v["net_debt_to_ebitda_ratio"] == pytest.approx((260 - 250) / (305 + 46), abs=TOL)
    assert v["equity_multiplier"] == pytest.approx(1825 / 1095, abs=TOL)
    assert v["debt_to_assets_ratio"] == pytest.approx(260 / 1900, abs=TOL)
    assert v["free_cash_flow_yield"] == pytest.approx((290 - 75) / 5880, abs=TOL)
    df = cut(alfa)
    origin = _lib.ft_solvency.get_debt_to_equity_ratio(df["long_term_debt"] + df["short_term_debt"], df["total_equity"])
    assert v["debt_to_equity_ratio"] == pytest.approx(origin.iloc[-1], abs=TOL)
    assert len(v) >= 15 and "FT-004" in piece_ids("fundamentals.solvency")


def test_valuation_FT_005(alfa, bravo, charlie):
    v = call("fundamentals.valuation", statements=alfa, as_of=AS_OF).value["latest"]
    eps = 237.6 / 98
    assert v["price_to_earnings_ratio"] == pytest.approx(60 / eps, abs=TOL)
    assert v["price_to_book_ratio"] == pytest.approx(60 / (1160 / 98), abs=TOL)
    assert v["market_cap"] == pytest.approx(5880, abs=TOL)
    assert v["enterprise_value"] == pytest.approx(5880 + 260 - 250, abs=TOL)
    assert v["ev_to_ebitda_ratio"] == pytest.approx(5890 / 351, abs=TOL)
    assert v["earnings_yield"] == pytest.approx(eps / 60, abs=TOL)
    assert v["dividend_yield"] == pytest.approx(55 / 98 / 60, abs=TOL)
    assert v["sbc_adjusted_free_cash_flow"] == pytest.approx(199, abs=TOL)
    # PEG takes growth in percent points, the dividend adjusted form takes a fraction: each is fed the unit it expects
    growth = 237.6 / 98 / (208.8 / 99) - 1
    assert v["price_to_earnings_growth_ratio"] == pytest.approx((60 / eps) / (growth * 100), abs=TOL)
    assert v["dividend_adjusted_price_to_earnings_growth_ratio"] == pytest.approx((60 / eps) / ((growth + 55 / 98 / 60) * 100), abs=TOL)
    # a company with losses has no P/E (the library would return a negative number)
    loss = call("fundamentals.valuation", statements=bravo, as_of=AS_OF)
    assert loss.value["latest"]["price_to_earnings_ratio"] is None             # the loss years have none, and 2020 (profit) is not "the latest"
    assert loss.value["series"]["price_to_earnings_ratio"][1:] == [None, None, None]
    assert call("fundamentals.valuation", statements=charlie, as_of=AS_OF).value["latest"]["price_to_earnings_ratio"] == pytest.approx(30 / (72 / 40), abs=TOL)
    assert len(v) >= 28 and "FT-005" in piece_ids("fundamentals.valuation")
    assert "get_weighted_dividend_yield" in ratios.EXCLUDED          # the library returns a negative yield from dividends paid


def test_read_formulas_and_written_convention_FT_006(alfa):
    res = call("fundamentals.read_formulas", statements=alfa, as_of=AS_OF)
    table = res.value["comparison"]
    assert set(table) == {"roic_simple", "enterprise_value", "buyback_yield", "shareholder_yield", "fcf_after_sbc"}
    # ROIC: the module takes the closing capital, the library the average capital; the difference is only that denominator
    roic = (305 * 0.8) / (1160 + 260)
    assert table["roic_simple"]["module"] == pytest.approx(roic, abs=TOL)
    df = cut(alfa)
    closing = _lib.ft_profitability.get_return_on_invested_capital(df["operating_income"], df["income_tax_expense"], df["income_before_tax"],
                                                                   df["total_equity"], df["long_term_debt"] + df["short_term_debt"])
    assert table["roic_simple"]["module"] == pytest.approx(closing.iloc[-1], abs=TOL)
    avg_eq, avg_debt = (df["total_equity"] + df["total_equity"].shift(1)) / 2, ((df["total_debt"] if "total_debt" in df else df["long_term_debt"] + df["short_term_debt"]))
    assert table["roic_simple"]["library"] != pytest.approx(table["roic_simple"]["module"], abs=1e-4)
    assert table["roic_simple"]["library"] == pytest.approx(305 * 0.8 / (1095 + (260 + 270) / 2), abs=TOL)
    assert avg_eq.iloc[-1] == 1095
    # EV: the difference is the 20 of short term investments
    assert table["enterprise_value"]["library"] - table["enterprise_value"]["module"] == pytest.approx(20, abs=TOL)
    assert table["buyback_yield"]["module"] == pytest.approx(table["buyback_yield"]["library"], abs=TOL)
    assert table["fcf_after_sbc"]["module"] == pytest.approx(table["fcf_after_sbc"]["library"], abs=TOL) == pytest.approx(199, abs=TOL)
    assert all(row["difference_explained"] for row in table.values())
    # the convention is written down for every formula that is read
    text = DOCS.read_text(encoding="utf-8")
    for needle in ("Enterprise value (FM-13)", "Buyback yield (FM-14)", "FCF after SBC (FM-15)", "ROIC, simplified (FM-16)", "TTM (FM-18)", "Point in time", "Missing"):
        assert needle in text
    for key, line in res.value["conventions"].items():
        assert line and isinstance(line, str)
    assert call("fundamentals.read_formulas", statements=alfa.iloc[0:0]).value is None
    assert "FT-006" in piece_ids("fundamentals.read_formulas")
