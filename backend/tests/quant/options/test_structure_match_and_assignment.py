"""L-56: recognise a structure from its legs, and the early assignment marks for sold options (rules written in the tests)."""
from __future__ import annotations

import math

import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import _common as c
from hokma.quant.options import assignment, structures

from .conftest import assert_empty, call, piece_ids


def leg(position, kind, strike, quantity=1, **extra):
    return {"position": position, "kind": kind, "strike": strike, "quantity": quantity, **extra}


# ------------------------------------------------------------------------------------------------ the matcher
def test_four_legs_written_by_hand_are_an_iron_condor():
    legs = [leg("short", "call", 110), leg("long", "put", 85), leg("long", "call", 115), leg("short", "put", 90)]      # any order
    found = structures.match(legs)
    assert found == structures.StructureMatch("iron_condor", "credit", (85.0, 90.0, 110.0, 115.0), 1.0)
    res = call("options.match_structure", legs=legs)
    assert res.value == {"matched": True, "structure": "iron_condor", "premium_class": "credit", "strikes": [85.0, 90.0, 110.0, 115.0], "quantity": 1.0, "reason": None}
    assert piece_ids("options.match_structure") == ("L-56",)


@pytest.mark.parametrize("legs,name,cls", [
    ([leg("long", "call", 100)], "long_call", "debit"),
    ([leg("long", "put", 100)], "long_put", "debit"),
    ([leg("long", "call", 100), leg("short", "call", 105)], "debit_call_spread", "debit"),
    ([leg("short", "call", 100), leg("long", "call", 105)], "credit_call_spread", "credit"),
    ([leg("long", "put", 105), leg("short", "put", 100)], "debit_put_spread", "debit"),
    ([leg("short", "put", 105), leg("long", "put", 100)], "credit_put_spread", "credit"),
    ([leg("short", "put", 100), leg("short", "call", 110)], "short_strangle", "credit"),
    ([leg("short", "put", 100), leg("short", "call", 100)], "short_straddle", "credit"),
    ([leg("short", "call", 100)], "short_call", "credit"),
    ([leg("short", "put", 100)], "short_put", "credit"),                      # a cash secured put has the same legs: the answer is short_put
])
def test_every_structure_of_the_matrix_is_recognised_from_its_legs(legs, name, cls):
    found = structures.match(legs)
    assert found is not None and (found.structure, found.premium_class) == (name, cls)
    assert found.structure in structures.STRUCTURES


def test_a_covered_call_needs_its_shares():
    stock = {"instrument": "stock", "position": "long", "quantity": 100}
    assert structures.match([stock, leg("short", "call", 110)], 100).structure == "covered_call"
    assert structures.match([{**stock, "quantity": 50}, leg("short", "call", 110)], 100) is None      # half the shares needed
    assert structures.match([{**stock, "quantity": 50}, leg("short", "call", 110)]).structure == "covered_call"     # without a multiplier the share count is not checked
    assert structures.match([{**stock, "position": "short"}, leg("short", "call", 110)]) is None
    assert structures.match([stock, leg("long", "call", 110)]) is None


def test_legs_that_are_not_a_named_structure_give_none():
    assert structures.match([leg("short", "put", 110), leg("short", "call", 100)]) is None                                       # inverted strangle
    assert structures.match([leg("long", "call", 100), leg("short", "call", 105, 2)]) is None                                    # a ratio spread: two quantities
    assert structures.match([leg("long", "call", 100, expiration="2026-12-18"), leg("short", "call", 105, expiration="2027-01-15")]) is None   # a diagonal
    assert structures.match([leg("long", "call", 100, expiration="2026-12-18"), leg("short", "call", 105, expiration="2026-12-18")]).structure == "debit_call_spread"
    assert structures.match([]) is None
    two_lots = [leg("long", "call", 100), leg("long", "call", 100), leg("short", "call", 105, 2)]
    assert structures.match(two_lots).structure == "debit_call_spread" and structures.match(two_lots).quantity == 2.0      # the same leg written twice adds up
    res = call("options.match_structure", legs=[leg("short", "put", 110), leg("short", "call", 100)])
    assert res.value["matched"] is False and res.value["structure"] is None and "named structures" in res.value["reason"]


def test_bad_legs_are_refused_or_explained():
    with pytest.raises(BadInput):
        call("options.match_structure", legs="x")
    with pytest.raises(BadInput):
        call("options.match_structure", legs=["x"])
    assert_empty(call("options.match_structure", legs=[leg("long", "straddle", 100)]), "kind")
    assert_empty(call("options.match_structure", legs=[leg("flat", "call", 100)]), "position")
    assert_empty(call("options.match_structure", legs=[leg("long", "call", -5)]), "strike")
    assert_empty(call("options.match_structure", legs=[leg("long", "call", 100, 0)]), "quantity")


# ------------------------------------------------------------------------------------------------ the assignment model
AS_OF = "2026-11-19T15:30:00-05:00"          # a Thursday; the ex-dividend date below is the next day
EX_DATE = "2026-11-20"
EXPIRATION = "2026-12-18"                    # 29 days after as_of
RATE = 0.05


def risk(legs, spot, dividends=None, **kwargs):
    res = call("options.assignment_risk", legs=legs, spot=spot, as_of=AS_OF, rate=RATE, dividends=dividends, **kwargs)
    assert res.value is not None, res.warnings
    return res.value


def test_a_short_put_in_the_money_one_day_before_the_ex_dividend_is_marked_by_the_interest_rule():
    # RULE written here: an in the money short put is marked when its extrinsic value (price minus intrinsic) is below the interest on the strike until expiration,
    # strike * (1 - exp(-rate * days / 365)). A dividend is reported but does not change the rule.
    carry = 100.0 * (1.0 - math.exp(-RATE * 29 / 365))                         # 0.3965 per share
    assert carry == pytest.approx(0.39647, abs=1e-5)
    dividends = [{"ex_date": EX_DATE, "amount": 0.40}]
    low = risk([leg("short", "put", 100, expiration=EXPIRATION, price=20.30)], spot=80.0, dividends=dividends)       # intrinsic 20, extrinsic 0.30 < 0.3965
    mark = low["legs"][0]
    assert mark["at_risk"] and mark["reasons"] == ["interest_on_strike_above_extrinsic"] and mark["assigned_at"] == "next_open"
    assert mark["extrinsic"] == pytest.approx(0.30) and mark["intrinsic"] == 20.0 and mark["days_to_expiration"] == 29
    assert mark["ex_dividend"] == {"ex_date": EX_DATE, "amount": 0.40} and low["any_at_risk"] is True
    high = risk([leg("short", "put", 100, expiration=EXPIRATION, price=20.50)], spot=80.0, dividends=dividends)       # extrinsic 0.50 > 0.3965
    assert not high["legs"][0]["at_risk"] and high["legs"][0]["assigned_at"] is None and high["any_at_risk"] is False


def test_a_put_out_of_the_money_is_never_marked():
    out = risk([leg("short", "put", 100, expiration=EXPIRATION, price=0.05)], spot=120.0)["legs"][0]
    assert not out["in_the_money"] and not out["at_risk"] and out["reasons"] == []


def test_a_short_call_is_marked_when_its_extrinsic_value_is_below_the_dividend_of_the_next_day():
    # RULE: an in the money short call, with an ex-dividend date 1 day ahead, is marked when the dividend is above strike * (1 - exp(-rate * years to the next payment))
    # (here the next payment is the expiration, 28 days after the ex-date: 0.3828) and above the extrinsic value of the call.
    threshold = 100.0 * (1.0 - math.exp(-RATE * 28 / 365))
    assert threshold == pytest.approx(0.38283, abs=1e-5)
    dividends = [{"ex_date": EX_DATE, "amount": 0.50}]
    risky = risk([leg("short", "call", 100, expiration=EXPIRATION, price=5.20)], spot=105.0, dividends=dividends)["legs"][0]     # extrinsic 0.20 < 0.50
    assert risky["at_risk"] and risky["reasons"] == ["extrinsic_below_ex_dividend"]
    safe = risk([leg("short", "call", 100, expiration=EXPIRATION, price=5.80)], spot=105.0, dividends=dividends)["legs"][0]     # extrinsic 0.80 > 0.50
    assert not safe["at_risk"]
    small = risk([leg("short", "call", 100, expiration=EXPIRATION, price=5.20)], spot=105.0, dividends=[{"ex_date": EX_DATE, "amount": 0.30}])["legs"][0]
    assert not small["at_risk"]                                                  # 0.30 is below the 0.3828 the dividend must beat, so exercising early does not pay


def test_the_ex_dividend_window_decides_which_dividends_count():
    leg_call = [leg("short", "call", 100, expiration=EXPIRATION, price=5.20)]
    far = [{"ex_date": "2026-11-23", "amount": 0.50}]                            # 4 days ahead
    assert not risk(leg_call, 105.0, far)["legs"][0]["at_risk"]
    assert risk(leg_call, 105.0, far, ex_div_window_days=4)["legs"][0]["at_risk"]
    assert not risk(leg_call, 105.0, [{"ex_date": "2026-11-19", "amount": 0.50}])["legs"][0]["at_risk"]       # ex-date today: it is too late to act on
    after = [{"ex_date": "2027-01-04", "amount": 0.50}]
    assert not risk(leg_call, 105.0, after, ex_div_window_days=30)["legs"][0]["at_risk"]                     # after the expiration


def test_expiry_and_no_extrinsic_value_are_marked_too():
    today = risk([leg("short", "put", 100, expiration="2026-11-19", price=20.0)], spot=80.0)["legs"][0]
    assert today["days_to_expiration"] == 0 and set(today["reasons"]) == {"expiry_in_the_money", "no_extrinsic_value"}
    parity = risk([leg("short", "call", 100, expiration=EXPIRATION, price=5.0)], spot=105.0)["legs"][0]       # price equals the intrinsic value
    assert parity["reasons"] == ["no_extrinsic_value"]
    expiring_otm = risk([leg("short", "call", 100, expiration="2026-11-19", price=0.01)], spot=95.0)["legs"][0]
    assert not expiring_otm["at_risk"]


def test_a_leg_can_be_priced_from_its_volatility_and_long_legs_are_ignored():
    value = risk([leg("short", "put", 100, expiration=EXPIRATION, volatility=0.30), leg("long", "put", 90, expiration=EXPIRATION, price=1.0),
                  {"instrument": "stock", "position": "long", "quantity": 100}], spot=80.0)
    assert value["n_short_legs"] == 1
    price = c.bs_price(80.0, 100.0, RATE, 0.30, 29 / 365, 0.0, True)
    assert value["legs"][0]["extrinsic"] == pytest.approx(price - 20.0)


def test_assignment_inputs_are_checked():
    ok = [leg("short", "put", 100, expiration=EXPIRATION, price=20.3)]
    assert_empty(call("options.assignment_risk", legs=[leg("short", "put", 100, expiration="2026-11-01", price=20.3)], spot=80.0, as_of=AS_OF, rate=RATE), "before as_of")
    assert_empty(call("options.assignment_risk", legs=[leg("short", "put", 100, expiration=EXPIRATION)], spot=80.0, as_of=AS_OF, rate=RATE), "price")
    assert_empty(call("options.assignment_risk", legs=ok, spot=80.0, as_of="not a date", rate=RATE), "as_of")
    assert_empty(call("options.assignment_risk", legs=ok, spot=-1.0, as_of=AS_OF, rate=RATE), "spot")
    assert_empty(call("options.assignment_risk", legs=ok, spot=80.0, as_of=AS_OF, rate=RATE, dividends=[{"ex_date": EX_DATE, "amount": -1.0}]), "amount")
    assert_empty(call("options.assignment_risk", legs=[], spot=80.0, as_of=AS_OF, rate=RATE), "at least one")
    with pytest.raises(BadInput):
        call("options.assignment_risk", legs=ok, spot=80.0, as_of=AS_OF, rate=RATE, dividends="x")
    with pytest.raises(BadInput):
        call("options.assignment_risk", legs=ok, spot=80.0, as_of=AS_OF, rate=RATE, ex_div_window_days=assignment.MAX_WINDOW_DAYS + 1)
    assert registry.describe("options.assignment_risk").piece_ids == ("L-56",)
