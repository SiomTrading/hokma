"""The four scenario fixtures are different on purpose, so a test that is given another scenario fails (N-41)."""
from decimal import Decimal as D

import pytest

from backtest.support import SCENARIOS, bars_of, expected, load_fixture, requires_engine, scenario_run
from hokma.backtest.fills import Decision
from hokma.backtest.ticks import Bar


def test_the_four_scenarios_have_different_results_and_different_bars(nq):
    results = {name: D(nq["scenarios"][name]["expected"].get("pnl_usd") or nq["scenarios"][name]["expected"]["long"]["pnl_usd"]) for name in SCENARIOS}
    assert results == {"T1": D("100.50"), "T2": D("-114.50"), "T3": D("-2059.50"), "T4": D("-49.50")}
    assert len(set(results.values())) == 4
    starts = {name: bars_of(name)[0].ts for name in SCENARIOS}
    assert len(set(starts.values())) == 4
    assert all(isinstance(b, Bar) for name in SCENARIOS for b in bars_of(name))


def test_the_bars_are_real_nq_with_a_tick_of_a_quarter_point(nq):
    assert nq["instrument"] == {"symbol": "NQZ5", "tick_size": "0.25", "multiplier": "20"}
    for name in SCENARIOS:
        for bar in bars_of(name):
            assert all((p / D("0.25")) == (p / D("0.25")).to_integral_value() for p in (bar.open, bar.high, bar.low, bar.close))
    assert sum(len(bars_of(name)) for name in SCENARIOS) == 30


@requires_engine
@pytest.mark.parametrize("name", SCENARIOS)
def test_every_scenario_matches_its_own_expected_result_and_no_other(scenario_name, name):
    out, _ = scenario_run(name, "short" if name == "T3" else "long")
    own = expected(name)
    assert str(out.realized_pnl_usd) == (own.get("pnl_usd") or own["long"]["pnl_usd"])
    if scenario_name != name:                                                                  # the expected value of another scenario never matches
        other = expected(scenario_name)
        assert str(out.realized_pnl_usd) != (other.get("pnl_usd") or other["long"]["pnl_usd"])


@requires_engine
def test_swapping_the_bars_of_two_scenarios_breaks_the_expected_result():
    from hokma.backtest import engine
    from backtest.support import MULT, TICK, plan, spec

    d1 = load_fixture()["scenarios"]["T1"]["decisions"]
    t4 = bars_of("T4")
    decisions = {d1["buy_after_close_of"]: [Decision("enter", "long")], d1["sell_after_close_of"]: [Decision("exit")]}
    swapped = engine.run(spec(), t4, plan(t4, {}), tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=False)
    assert swapped.fills == () and swapped.realized_pnl_usd == D(0) != D(expected("T1")["pnl_usd"])
    with pytest.raises(KeyError):
        plan(t4, decisions)                                                                    # the T1 times do not exist in the T4 bars
