"""Performance attribution (SPEC-018: ECT-078). Every expected value is written by hand."""
from decimal import Decimal

import pytest

from hokma.backoffice import attribution as at
from hokma.backoffice.attribution import AttributionError, FactorPeriod, FixedIncomeSide, Period

D = Decimal
SECTORS = {"tech": ("0.5", "0.4", "0.10", "0.08"), "bonds": ("0.5", "0.6", "0.02", "0.03")}


def test_brinson_fachler_effects_by_hand_and_the_exact_sum():
    r = at.brinson_fachler(SECTORS)
    # R_b = 0.4*0.08 + 0.6*0.03 = 0.05 ; R_p = 0.5*0.10 + 0.5*0.02 = 0.06 ; active 0.01
    assert r.benchmark_return == D("0.050") and r.portfolio_return == D("0.060") and r.active_return == D("0.010")
    tech, bonds = r.rows
    assert tech.allocation == D("0.1") * D("0.03") and tech.selection == D("0.4") * D("0.02") and tech.interaction == D("0.1") * D("0.02")
    assert (tech.allocation, tech.selection, tech.interaction) == (D("0.003"), D("0.008"), D("0.002"))
    assert (bonds.allocation, bonds.selection, bonds.interaction) == (D("0.002"), D("-0.006"), D("0.001"))
    assert r.allocation == D("0.005") and r.selection == D("0.002") and r.interaction == D("0.003")
    assert r.allocation + r.selection + r.interaction == r.active_return                    # exact, no residual


def test_weights_that_do_not_add_to_one_are_refused():
    with pytest.raises(AttributionError):
        at.brinson_fachler({"a": ("0.5", "0.5", "0.1", "0.1"), "b": ("0.4", "0.5", "0.1", "0.1")})
    with pytest.raises(AttributionError):
        at.brinson_fachler({})


def test_a_single_period_links_to_itself():
    r = at.brinson_fachler(SECTORS)
    linked = at.carino_link([Period(r.portfolio_return, r.benchmark_return, r.effects)])
    assert abs(linked.factors[0] - 1) < D("1e-30") and abs(linked.effects["allocation"] - D("0.005")) < D("1e-30")


def test_two_periods_are_linked_not_added_and_sum_to_the_accumulated_active_return():
    p1 = Period(D("0.10"), D("0.05"), {"allocation": D("0.02"), "selection": D("0.03")})
    p2 = Period(D("0.05"), D("0.04"), {"allocation": D("0.004"), "selection": D("0.006")})
    linked = at.carino_link([p1, p2])
    assert linked.portfolio_return == D("0.155") and linked.benchmark_return == D("0.092")          # 1.10*1.05-1 and 1.05*1.04-1
    assert abs(linked.active_return - D("0.063")) < D("1e-40")
    assert abs(linked.residual) < D("1e-25")
    plain_sum = D("0.02") + D("0.03") + D("0.004") + D("0.006")
    assert plain_sum == D("0.06") and abs(sum(linked.effects.values()) - plain_sum) > D("0.002")     # adding the periods would miss the cross term


def test_equal_accumulated_returns_do_not_divide_by_zero():
    # portfolio 10 percent twice, benchmark 21 percent then 0: both accumulate to 21 percent, so the whole span has no active return.
    p1 = Period(D("0.10"), D("0.21"), {"selection": D("-0.11")})
    p2 = Period(D("0.10"), D("0"), {"selection": D("0.10")})
    linked = at.carino_link([p1, p2])
    assert linked.portfolio_return == D("0.21") and linked.benchmark_return == D("0.21")
    assert abs(linked.effects["selection"]) < D("1e-25")


def test_equal_returns_in_one_period_use_the_limit_factor():
    p1 = Period(D("0.05"), D("0.05"), {"selection": D("0")})
    p2 = Period(D("0.10"), D("0.04"), {"selection": D("0.06")})
    linked = at.carino_link([p1, p2])
    assert abs(linked.residual) < D("1e-25") and linked.factors[0] > 0


def test_the_effects_of_a_period_must_add_to_its_active_return_and_a_total_loss_cannot_be_linked():
    with pytest.raises(AttributionError):
        at.carino_link([Period(D("0.10"), D("0.05"), {"selection": D("0.01")})])
    with pytest.raises(AttributionError):
        at.carino_link([Period(D("-1"), D("0.05"), {"selection": D("-1.05")})])


def test_factor_attribution_explains_the_return_over_the_benchmark_with_active_exposures():
    period = FactorPeriod(D("0.08"), D("0.05"), {"value": D("1.2"), "size": D("0.5")}, {"value": D("1.0"), "size": D("0.5")}, {"value": D("0.10"), "size": D("0.04")})
    r = at.factor_attribution([period])
    # active exposure of value 0.2 * 0.10 = 0.02, of size 0; the rest of the active return 0.03 is specific: 0.01
    assert r.effects["value"] == D("0.020") and r.effects["size"] == 0 and r.effects["specific"] == D("0.010")
    assert sum(r.effects.values()) == D("0.03") == r.active_return                  # R_p - R_b, not R_p - R_f


def test_factor_periods_are_linked_not_summed():
    a = FactorPeriod(D("0.10"), D("0.05"), {"value": D("1.5")}, {"value": D("1.0")}, {"value": D("0.04")})
    b = FactorPeriod(D("0.05"), D("0.04"), {"value": D("1.5")}, {"value": D("1.0")}, {"value": D("0.02")})
    r = at.factor_attribution([a, b])
    assert abs(r.active_return - D("0.063")) < D("1e-40") and abs(r.residual) < D("1e-25")
    assert abs(sum(r.effects.values()) - D("0.06")) > D("0.002")                       # not the plain sum of the two periods
    with pytest.raises(AttributionError):
        at.factor_attribution([FactorPeriod(D("0.1"), D("0.1"), {"value": D(1)}, {}, {})])


def test_fixed_income_income_is_scaled_by_time():
    pf = FixedIncomeSide(D("0.05"), D("6"), D("4"), D("0.012"))
    bm = FixedIncomeSide(D("0.04"), D("5"), D("4"), D("0.010"))
    r = at.fixed_income_attribution(pf, bm, days=73, rate_change=D("0.001"), spread_change=D("0.002"))
    assert r["income"] == D("0.01") * D(73) / D(365) == D("0.002")                       # a yield gap of one point earns a fifth of it in 73 days
    assert r["duration"] == D("-0.001") and r["spread"] == 0
    assert r["active"] == D("0.002") and r["selection"] == D("0.001")                  # what is left: 0.002 - 0.002 + 0.001 - 0
    assert r["income"] + r["duration"] + r["spread"] + r["selection"] == r["active"]
    with pytest.raises(AttributionError):
        at.fixed_income_attribution(pf, bm, days=0, rate_change=D(0), spread_change=D(0))


def test_the_report_table_comes_from_the_result():
    table = at.brinson_table(at.brinson_fachler(SECTORS))
    assert table[0] == ["sector", "allocation", "selection", "interaction", "total"]
    assert table[-1] == ["total", "0.005", "0.002", "0.003", "0.01"]
