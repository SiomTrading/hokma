"""DTS-10 (derived Q4 and lag warnings) and AFS-028 (fiscal quarter from the name of the call, LTM and NTM)."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput

from .conftest import call, piece_ids

TOL = 1e-9


def by_end(res) -> dict:
    return {q["period_end"]: q for q in res.value["quarters"]}


def test_quarter_facts_derive_q4_DTS_10(quarterly):
    res = call("fundamentals.quarter_facts", facts=quarterly, as_of="2024-06-30")
    q = by_end(res)
    # Q4 is the year minus the three quarters: 1300 - (310 + 320 + 335) and 1200 - (290 + 300 + 305)
    assert q["2023-12-31"]["value"] == pytest.approx(1300 - (310 + 320 + 335), abs=TOL) and q["2023-12-31"]["derived"] is True
    assert q["2023-12-31"]["fiscal_period"] == "Q4"
    assert q["2022-12-31"]["value"] == pytest.approx(1200 - (290 + 300 + 305), abs=TOL) and q["2022-12-31"]["derived"] is True
    assert q["2023-09-30"]["derived"] is False and q["2023-09-30"]["value"] == 335     # the three month figure, not the nine month one
    assert q["2024-03-31"]["value"] == 340 and res.value["latest_period_end"] == "2024-03-31"
    assert res.value["days_since_latest_period"] == 91
    # point in time: on 2023-03-31 only 2022 is filed; the year was filed on 2023-02-20
    early = call("fundamentals.quarter_facts", facts=quarterly, as_of="2023-03-31")
    assert [x["fiscal_period"] for x in early.value["quarters"]] == ["Q1", "Q2", "Q3", "Q4"] and "excluded 7 fact(s) filed after as_of" in early.warnings
    before_year = call("fundamentals.quarter_facts", facts=quarterly, as_of="2023-01-31")
    assert not [x for x in before_year.value["quarters"] if x["derived"]]       # the annual figure is not filed yet: no Q4
    assert "FT" not in "".join(before_year.warnings) and not any(w.startswith("late_filing") for w in res.warnings)
    assert "DTS-10" in piece_ids("fundamentals.quarter_facts")


def test_quarter_facts_lag_warnings_DTS_10(quarterly):
    late = quarterly.copy()
    late.loc[(late["fiscal_period"] == "Q3") & (late["fiscal_year"] == 2023), "filed"] = "2024-02-10"     # 133 days after the period ended
    res = call("fundamentals.quarter_facts", facts=late, as_of="2024-06-30")
    assert any(w.startswith("late_filing:Q3:133d") for w in res.warnings)
    stale = call("fundamentals.quarter_facts", facts=quarterly, as_of="2024-12-31")
    assert any(w.startswith("stale: the latest period ended 275 days before as_of") for w in stale.warnings)
    gap = quarterly[~((quarterly["fiscal_period"] == "Q2") & (quarterly["fiscal_year"] == 2023) & (pd.to_datetime(quarterly["period_start"]) == pd.Timestamp("2023-04-01")))]
    broken = call("fundamentals.quarter_facts", facts=gap, as_of="2024-06-30")
    assert any(w.startswith("q4_not_derivable:2023-12-31") for w in broken.warnings)
    assert "2023-12-31" not in by_end(broken) or by_end(broken)["2023-12-31"]["derived"] is False
    assert call("fundamentals.quarter_facts", facts=quarterly.iloc[0:0], as_of="2024-06-30").value is None
    nan = quarterly.copy()
    nan["value"] = np.nan
    assert call("fundamentals.quarter_facts", facts=nan, as_of="2024-06-30").value is None
    assert call("fundamentals.quarter_facts", facts=quarterly, as_of="2019-01-01").value is None
    with pytest.raises(BadInput):
        call("fundamentals.quarter_facts", facts=quarterly.drop(columns=["filed"]), as_of="2024-06-30")
    with pytest.raises(BadInput):
        call("fundamentals.quarter_facts", facts=quarterly, as_of="soon")
    with pytest.raises(BadInput):
        call("fundamentals.quarter_facts", facts=[1], as_of="2024-06-30")


@pytest.mark.parametrize("name, year, quarter", [
    ("Q3 FY2024 Earnings Call", 2024, 3), ("3Q24 results", 2024, 3), ("Third Quarter 2023 Conference Call", 2023, 3),
    ("FY24 Q2 call", 2024, 2), ("Q1 2025 Earnings Webcast", 2025, 1), ("Fourth Quarter of Fiscal Year 2022 Results", 2022, 4)])
def test_fiscal_quarter_from_the_name_AFS_028(name, year, quarter):
    res = call("fundamentals.fiscal_quarter", call_name=name)
    assert res.value["fiscal_year"] == year and res.value["fiscal_quarter"] == quarter and res.value["source"] == "call_name"
    assert res.value["label"] == f"Q{quarter} FY{year}"
    # the calendar date is ignored: a call held in January for the fourth quarter still reads what the name says
    other = call("fundamentals.fiscal_quarter", call_name=name, calendar_date="2024-01-20")
    assert (other.value["fiscal_year"], other.value["fiscal_quarter"]) == (year, quarter) and "calendar_date_ignored" in other.warnings


def test_fiscal_quarter_never_from_the_calendar_AFS_028():
    none = call("fundamentals.fiscal_quarter", call_name="Annual Earnings Call", calendar_date="2024-04-15")
    assert none.value is None and "fiscal_quarter_not_in_call_name" in none.warnings and "calendar_date_ignored" in none.warnings
    ambiguous = call("fundamentals.fiscal_quarter", call_name="Q1 2024 and Q2 2024 combined call")
    assert ambiguous.value is None and ambiguous.warnings == ("ambiguous_call_name",)
    assert call("fundamentals.fiscal_quarter", call_name="").value is None
    with pytest.raises(BadInput):
        call("fundamentals.fiscal_quarter", call_name=2024)
    with pytest.raises(BadInput):
        call("fundamentals.fiscal_quarter", call_name="Q3 FY2024 " * 40)
    assert "AFS-028" in piece_ids("fundamentals.fiscal_quarter")


def test_valuation_multiple_labels_ltm_and_ntm_AFS_028():
    ntm = call("fundamentals.valuation_multiple", price=100, quarterly_values=[1, 1.5, 2, 2.5], basis="NTM")
    assert ntm.value["multiple"] == pytest.approx(100 / 7, abs=TOL) and ntm.value["basis"] == "NTM" and ntm.value["label"] == "price_to_earnings NTM"
    # NTM is the sum of the next four quarterly estimates and nothing else
    assert call("fundamentals.valuation_multiple", price=100, quarterly_values=[1, 1.5, 2], basis="NTM").value is None
    assert call("fundamentals.valuation_multiple", price=100, quarterly_values=[1, 1, 1, 1, 1], basis="NTM").value is None
    ltm = call("fundamentals.valuation_multiple", price=100, quarterly_values=[9, 9, 1, 2, 3, 4], basis="LTM", kind="price_to_sales")
    assert ltm.value["multiple"] == pytest.approx(100 / 10, abs=TOL) and ltm.value["quarters_used"] == [1, 2, 3, 4] and ltm.value["label"] == "price_to_sales LTM"
    assert call("fundamentals.valuation_multiple", price=100, quarterly_values=[1, 2, 3], basis="LTM").value is None
    assert call("fundamentals.valuation_multiple", price=100, quarterly_values=[1, -1, -2, 0], basis="NTM").warnings == ("non_positive_denominator",)
    assert call("fundamentals.valuation_multiple", price=100, quarterly_values=[1, float("nan"), 2, 3], basis="LTM").value is None
    assert call("fundamentals.valuation_multiple", price=100, quarterly_values=[], basis="LTM").value is None
    for bad in ("TTM", "", None, "ltm"):
        with pytest.raises(BadInput):
            call("fundamentals.valuation_multiple", price=100, quarterly_values=[1, 2, 3, 4], basis=bad)
    with pytest.raises(BadInput):
        call("fundamentals.valuation_multiple", price=float("nan"), quarterly_values=[1, 2, 3, 4], basis="LTM")
    with pytest.raises(BadInput):
        call("fundamentals.valuation_multiple", price=100, quarterly_values=[1, 2, 3, 4], basis="LTM", kind="Price To Earnings")
    assert "AFS-028" in piece_ids("fundamentals.valuation_multiple")
