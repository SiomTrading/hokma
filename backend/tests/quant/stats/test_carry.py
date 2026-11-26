"""P-05: carry of a future from its term structure (the traded contract against the next one). Two CL contracts written by hand."""
import pytest

from hokma.quant.errors import BadInput

from .conftest import call, piece_ids

# two contracts of crude oil written by hand: the traded one at 70.00 expiring 2026-11-20 and the next at 69.40 expiring 2026-12-21 (31 days later)
NEAR = {"price": 70.0, "expiry": "2026-11-20"}
FAR = {"next_price": 69.4, "next_expiry": "2026-12-21"}


def test_annualised_carry_by_hand():
    res = call("stats.futures_carry", **NEAR, **FAR)
    years = 31 / 365
    carry = (70.0 - 69.4) / years                                  # 0.60 per 31 days: 7.0645 price units a year
    assert res.value["carry_annualized"] == pytest.approx(carry, abs=1e-12) and carry == pytest.approx(7.064516, abs=1e-6)
    assert res.value["carry_pct_annualized"] == pytest.approx(carry / 70.0, abs=1e-12)           # 10.09 percent a year of the traded price
    assert res.value["spread"] == pytest.approx(0.6) and res.value["days_between"] == 31 and res.value["years_between"] == pytest.approx(years)
    assert res.value["structure"] == "backwardation" and res.unit == "price_per_year" and res.piece_ids == ("P-05",) and piece_ids("stats.futures_carry") == ("P-05",)


def test_contango_has_negative_carry_and_a_flat_curve_none():
    res = call("stats.futures_carry", price=70.0, expiry="2026-11-20", next_price=70.5, next_expiry="2026-12-21")
    assert res.value["carry_annualized"] == pytest.approx(-0.5 * 365 / 31, abs=1e-12) and res.value["structure"] == "contango"
    flat = call("stats.futures_carry", price=70.0, expiry="2026-11-20", next_price=70.0, next_expiry="2026-12-21")
    assert flat.value["carry_annualized"] == 0.0 and flat.value["structure"] == "flat"


def test_without_the_second_expiry_the_value_is_none_with_a_warning():
    for kwargs in (NEAR, {**NEAR, "next_price": 69.4}, {**NEAR, "next_expiry": "2026-12-21"}):
        res = call("stats.futures_carry", **kwargs)
        assert res.value is None and "no second expiry" in " ".join(res.warnings), kwargs


def test_dates_and_prices_are_checked():
    assert call("stats.futures_carry", **NEAR, next_price=69.4, next_expiry="2026-11-20").value is None            # the same day
    assert "after expiry" in " ".join(call("stats.futures_carry", **NEAR, next_price=69.4, next_expiry="2026-10-01").warnings)
    assert call("stats.futures_carry", price=0.0, expiry="2026-11-20", **FAR).value is None
    assert call("stats.futures_carry", price=70.0, expiry="2026-11-20", next_price=-1.0, next_expiry="2026-12-21").value is None
    assert call("stats.futures_carry", price=float("nan"), expiry="2026-11-20", **FAR).value is None
    assert call("stats.futures_carry", price=70.0, expiry="soon", **FAR).value is None
    with pytest.raises(BadInput):
        call("stats.futures_carry", price="70", expiry="2026-11-20", **FAR)
    with pytest.raises(BadInput):
        call("stats.futures_carry", price=70.0, expiry=20261120, **FAR)
