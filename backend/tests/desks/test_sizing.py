"""N-06, N-05, P-01, P-08: sizes with the multiplier of the contract sheet, written by hand for NQ, MNQ, ES and GC (and a CFD)."""
from decimal import Decimal as D

import pytest

from hokma.desks.trading import sizing
from hokma.desks.trading.sizing import SizingError

# multiplier, tick: the sheets of the instruments (NQ 20 per point, MNQ 2, ES 50, GC 100)
NQ, MNQ, ES, GC = (20, "0.25"), (2, "0.25"), (50, "0.25"), (100, "0.10")


# ---- N-05 and N-06: fixed risk with the multiplier -------------------------------------------------------------------------

def _fixed(sheet, **kwargs):
    mult, tick = sheet
    return sizing.fixed_risk(multiplier=mult, tick_size=tick, **kwargs)


def test_N_05_nq_risking_500_with_a_stop_of_10_points_is_2_contracts_rounded_down():
    out = _fixed(NQ, risk_usd=500, entry="21000", stop_distance=10)
    assert (out.status, out.contracts, out.risk_per_contract, out.risk_usd) == ("OK", 2, D(200), D(400))   # 500 / (10 * 20) = 2.5


def test_N_06_mnq_has_a_multiplier_of_2_so_the_same_risk_buys_25_contracts():
    out = _fixed(MNQ, risk_usd=500, entry="21000", stop_distance=10)
    assert (out.contracts, out.risk_per_contract, out.risk_usd) == (25, D(20), D(500))


def test_N_06_es_has_a_multiplier_of_50_and_the_floor_is_exact():
    assert _fixed(ES, risk_usd=1000, entry="5000", stop_distance=10).contracts == 2            # 1000 / 500
    assert _fixed(ES, risk_usd="999.99", entry="5000", stop_distance=10).contracts == 1
    out = _fixed(ES, risk_usd=499, entry="5000", stop_distance=10)
    assert (out.status, out.contracts, out.code) == ("NO_TRADE", 0, "no_trade_budget") and out.stop_distance == D(10)


def test_N_06_gc_has_a_multiplier_of_100():
    out = _fixed(GC, risk_usd=2000, entry="2400.0", stop_distance="4.0")
    assert (out.contracts, out.risk_per_contract) == (5, D(400))                                # 2000 / (4 * 100)


def test_N_06_the_stop_in_ticks_gives_the_same_size_as_the_stop_in_points():
    assert _fixed(NQ, risk_usd=500, entry="21000", stop_ticks=40).contracts == 2               # 40 ticks of 0.25 are 10 points
    assert _fixed(ES, risk_usd=500, entry="5000", stop_ticks=8).contracts == 5                  # 2 points * 50 = 100 each
    assert _fixed(GC, risk_usd=2000, entry="2400.0", stop_ticks=40).contracts == 5


def test_N_05_a_short_puts_the_stop_above_the_entry_and_sizes_the_same():
    assert _fixed(NQ, risk_usd=500, entry="21000", stop_distance=10, side="short").contracts == 2


def test_N_06_a_cfd_with_a_multiplier_of_one_is_sized_in_units_per_point():
    out = sizing.fixed_risk(risk_usd=500, entry="1850.5", multiplier=1, stop_distance=25)
    assert (out.contracts, out.risk_usd) == (20, D(500))


def test_N_05_fixed_risk_is_the_same_calculation_as_contracts():
    a = sizing.fixed_risk(risk_usd=777, entry="21000", multiplier=20, tick_size="0.25", stop_distance=7)
    b = sizing.contracts(risk_budget_usd=777, entry="21000", stop="20993", multiplier=20, tick_size="0.25")
    assert a == b


@pytest.mark.parametrize("kwargs, code", [
    (dict(stop_distance=10, stop_ticks=40), "bad_stop"),
    (dict(), "bad_stop"),
    (dict(stop_ticks=0), "bad_stop"),
    (dict(stop_ticks=True), "bad_stop"),
    (dict(stop_distance=0), "bad_stop"),
    (dict(stop_distance=-5), "bad_stop"),
    (dict(stop_distance=10, side="flat"), "bad_side"),
    (dict(stop_distance="0.10"), "off_grid"),
    (dict(stop_distance=30000), "bad_price"),
    (dict(stop_distance=float("nan")), "bad_number"),
])
def test_N_05_inputs_that_cannot_be_sized_are_refused_with_a_stable_code(kwargs, code):
    with pytest.raises(SizingError) as err:
        sizing.fixed_risk(risk_usd=500, entry="21000", multiplier=20, tick_size="0.25", **kwargs)
    assert err.value.code == code


def test_N_05_a_stop_in_ticks_needs_the_tick():
    with pytest.raises(SizingError) as err:
        sizing.fixed_risk(risk_usd=500, entry="21000", multiplier=20, stop_ticks=40)
    assert err.value.code == "bad_tick"


# ---- P-01: volatility target in seven steps --------------------------------------------------------------------------------

def _vol(sheet=NQ, **kwargs):
    base = dict(capital=1_000_000, target_vol="0.20", price=20000, instrument_vol="0.01", multiplier=sheet[0], periods_per_year=256)
    base.update(kwargs)
    return sizing.vol_target(**base)


def test_P_01_the_seven_steps_match_the_hand_values():
    out = _vol()
    assert out.annual_cash_target == D(200000)                 # 1,000,000 * 0.20
    assert out.period_cash_target == D(12500)                  # 200,000 / sqrt(256) = 200,000 / 16
    assert out.price_volatility == D(200)                      # 20,000 * 0.01
    assert out.contract_volatility == D(4000)                  # 200 * 20
    assert out.scalar == D("3.125")                            # 12,500 / 4,000
    assert (out.exact_position, out.contracts, out.capped) == (D("3.125"), 3, False)


def test_P_01_the_forecast_scales_the_position_and_a_negative_one_is_a_short():
    assert _vol(forecast=20).contracts == 6                    # 3.125 * 20 / 10 = 6.25
    assert _vol(forecast=5).contracts == 1                     # 1.5625
    assert _vol(forecast=-10).contracts == -3                  # rounded toward zero
    assert _vol(forecast=-20).contracts == -6
    assert _vol(forecast=1).contracts == 0                     # 0.3125 is less than one contract


def test_P_01_the_multiplier_changes_the_size_mnq_is_a_tenth_of_nq():
    out = _vol(MNQ)
    assert (out.contract_volatility, out.contracts) == (D(400), 31)    # 12,500 / 400 = 31.25


def test_P_01_an_annual_volatility_with_an_annual_target_gives_the_same_scalar():
    out = _vol(vol_basis="annual", instrument_vol="0.16")      # 20,000 * 0.16 * 20 = 64,000 a year; 200,000 / 64,000
    assert (out.period_cash_target, out.scalar, out.contracts) == (D(200000), D("3.125"), 3)


def test_P_01_the_period_target_uses_the_square_root_of_the_periods_of_the_year():
    out = _vol(periods_per_year=252)
    assert float(out.period_cash_target) == pytest.approx(200000 / 252 ** 0.5, rel=1e-12)


def test_P_01_the_cap_of_the_prop_firm_cuts_the_size():
    out = _vol(forecast=20, max_contracts=4)
    assert (out.contracts, out.capped, out.code) == (4, True, "capped")
    assert _vol(forecast=-20, max_contracts=4).contracts == -4


def test_P_01_a_currency_other_than_usd_needs_its_rate_and_is_priced_in_usd():
    out = _vol(currency="EUR", fx_rate="1.25")                 # contract volatility 5,000 USD: 12,500 / 5,000 = 2.5
    assert (out.contract_volatility, out.contracts) == (D(5000), 2)
    with pytest.raises(SizingError) as err:
        _vol(currency="EUR")
    assert err.value.code == "fx_missing"


@pytest.mark.parametrize("kwargs, code", [
    (dict(capital=0), "absurd_budget"), (dict(capital=D("1e13")), "absurd_budget"), (dict(target_vol=0), "bad_vol"),
    (dict(target_vol=6), "bad_vol"), (dict(instrument_vol=0), "bad_vol"), (dict(price=0), "bad_vol"), (dict(multiplier=0), "bad_multiplier"),
    (dict(vol_basis="weekly"), "bad_vol"), (dict(periods_per_year=0), "bad_periods"), (dict(periods_per_year=True), "bad_periods"),
    (dict(max_contracts=0), "bad_cap"), (dict(average_forecast=0), "bad_forecast"), (dict(capital=float("inf")), "bad_number"),
    (dict(currency="EUR", fx_rate=0), "bad_fx"),
])
def test_P_01_absurd_inputs_are_refused_with_a_stable_code(kwargs, code):
    with pytest.raises(SizingError) as err:
        _vol(**kwargs)
    assert err.value.code == code


# ---- P-08: dead band -------------------------------------------------------------------------------------------------------

def test_P_08_a_position_inside_the_band_does_not_trade():
    out = sizing.buffer(position=10, target="10.4", band="0.10")
    assert (out.traded, out.position, out.trade) == (False, D(10), D(0))
    assert (out.lower, out.upper) == (D("9.360"), D("11.440"))        # 10.4 plus or minus 1.04


def test_P_08_a_target_outside_the_band_trades_only_to_the_edge():
    out = sizing.buffer(position=10, target=12, band="0.10")
    assert (out.traded, out.position, out.trade) == (True, D("10.8"), D("0.8"))      # 12 minus 1.2
    down = sizing.buffer(position=15, target=12, band="0.10")
    assert (down.position, down.trade) == (D("13.2"), D("-1.8"))


def test_P_08_the_band_follows_the_size_of_a_short_target():
    out = sizing.buffer(position=-10, target=-12, band="0.10")        # band from -13.2 to -10.8
    assert (out.traded, out.position, out.trade) == (True, D("-10.8"), D("-0.8"))
    assert sizing.buffer(position=-12.5, target=-12).traded is False


def test_P_08_the_edge_of_the_band_is_inside_it_and_a_zero_target_has_no_band():
    assert sizing.buffer(position="10.8", target=12, band="0.10").traded is False
    out = sizing.buffer(position=3, target=0)
    assert (out.traded, out.position, out.trade) == (True, D(0), D(-3))
    assert sizing.buffer(position=10, target=12, band=0).position == D(12)


@pytest.mark.parametrize("kwargs", [dict(band="1.5"), dict(band=-0.1), dict(band=float("nan")), dict(position=None), dict(target="x")])
def test_P_08_bad_inputs_are_refused(kwargs):
    args = dict(position=10, target=12, band="0.10")
    args.update(kwargs)
    with pytest.raises(SizingError):
        sizing.buffer(**args)
