"""TA-37: numbers written by a model are normalised; a number that is not in the snapshot is unverified."""
import math

import pytest

from hokma.desks.core import numbers as nm


@pytest.mark.parametrize("text,value,unit", [
    ("1,2k", 1200.0, "number"), ("3.4%", 3.4, "pct"), ("USD 1.000", 1000.0, "currency"), ("1,234.56", 1234.56, "number"),
    ("1.234,56", 1234.56, "number"), ("$1,234.50", 1234.5, "currency"), ("(1,200)", -1200.0, "number"), ("-3.5", -3.5, "number"),
    ("2.5M", 2.5e6, "number"), ("1.5bn", 1.5e9, "number"), ("0.5", 0.5, "number"), ("12", 12.0, "number"), ("1,000,000", 1e6, "number"),
    ("\u20ac5", 5.0, "currency"), ("150 USD", 150.0, "currency"), ("  7 ", 7.0, "number"), ("-2%", -2.0, "pct")])
def test_written_numbers_are_normalised_TA_37(text, value, unit):
    parsed = nm.parse_number(text)
    assert parsed.ok and math.isclose(parsed.value, value) and parsed.unit == unit


@pytest.mark.parametrize("text", ["N/A", "none", "", "-", "TBD", "unknown", "about 150", "150-160", "abc", "1,2,3x", None])
def test_placeholders_and_hedges_are_dropped_TA_37(text):
    parsed = nm.parse_number(text)
    assert not parsed.ok and parsed.value is None


def test_python_numbers_pass_and_non_finite_do_not_TA_37():
    assert nm.parse_number(5).value == 5.0 and nm.parse_number(2.5).value == 2.5
    assert not nm.parse_number(float("nan")).ok and not nm.parse_number(float("inf")).ok and not nm.parse_number(True).ok


def test_a_percentage_is_not_a_price_TA_37():
    assert nm.coerce_optional_float("15%") is None and nm.coerce_optional_float("15%", allow_pct=True) == 15.0
    assert nm.coerce_optional_float("$1,234.50") == 1234.5 and nm.coerce_optional_float("N/A") is None
    assert nm.coerce_optional_float(None) is None and nm.coerce_optional_float(189.5) == 189.5


def test_a_number_not_in_the_snapshot_is_unverified_TA_37():
    snapshot = {"price": 189.5, "volume": 1200, "change": 0.034, "levels": [150, 160], "text": "1.234,56"}
    text = "Price 189.50, volume 1,2k, change 3.4%, levels 150-160, cash 1.234,56 and a target of 200."
    checks = {c.raw: c.status for c in nm.check_text(text, snapshot)}
    assert checks["189.50"] == "verified" and checks["1,2k"] == "verified" and checks["3.4%"] == "verified"
    assert checks["150"] == "verified" and checks["160"] == "verified" and checks["1.234,56"] == "verified"
    assert checks["200"] == "unverified"
    assert [c.raw for c in nm.unverified_numbers(text, snapshot)] == ["200"]


def test_rounded_display_matches_the_snapshot_TA_37():
    assert nm.unverified_numbers("The close was 189.5", {"close": 189.4987}) == []
    assert [c.raw for c in nm.unverified_numbers("The close was 189.6", {"close": 189.4987})] == ["189.6"]


def test_dates_are_not_quantities_TA_37():
    assert nm.check_text("On 2026-11-22 it closed", {"close": 1}) == []


def test_mark_unverified_tags_only_the_unverified_ones_TA_37():
    assert nm.mark_unverified("Target 200 and 3.4% up", {"g": 0.034}) == "Target 200 [unverified] and 3.4% up"
    assert nm.mark_unverified("nothing here", {}) == "nothing here"


def test_snapshot_values_flatten_every_leaf_TA_37():
    values = nm.snapshot_values({"a": 1, "b": [2, {"c": "3.5"}], "d": "text with 7 and 8", "e": None, "f": True})
    assert sorted(values) == [1.0, 2.0, 3.5, 7.0, 8.0]
