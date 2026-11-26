"""Monte Carlo over a saved run (SPEC-020: B-31 and V-04 use it as the placebo; the figures of bootstrap and permutation are the functions of
hokma.quant.metrics.montecarlo, so the tests prove identity with them and the determinism, not a second calculation)."""
import dataclasses
from decimal import Decimal as D

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from backtest import search_support as ss
from backtest.support import requires_engine
from hokma.backtest import placebo, results, sweep
from hokma.backtest.placebo import PlaceboError, PlaceboInputs, simulate
from hokma.quant.errors import BadInput
from hokma.quant.metrics import montecarlo


@pytest.fixture()
def saved(db, tmp_path):
    """A finished run with the daily equity of the real fixture returns, and the bars its trades were made on."""
    bars = ss.chained_bars(1000)
    return db, tmp_path, ss.saved_run(db, tmp_path, bars), bars


pytestmark = requires_engine


def returns_of(db, tmp_path, run_id):
    return results.equity_returns(results.load_curve(db, run_id, curve_dir=tmp_path))


def test_the_bootstrap_is_the_stationary_bootstrap_of_the_quant_module_over_the_saved_curve(saved):
    db, tmp_path, run_id, _ = saved
    out = simulate(run_id, n=300, method="bootstrap", seed=4, db=db, curve_dir=tmp_path)
    series = returns_of(db, tmp_path, run_id)
    assert out.figures["sharpe"] == montecarlo.sharpe(series, runs=300, seed=4).value
    assert out.figures["cagr"] == montecarlo.cagr(series, runs=300, seed=4).value
    assert (out.run_id, out.method, out.n, out.seed, out.p_value) == (run_id, "bootstrap", 300, 4, None)
    assert out.figures["sharpe"]["runs"] == 300 and out.figures["sharpe"]["seed"] == 4          # every figure declares its seed and its runs
    assert simulate(run_id, n=300, method="bootstrap", seed=4, db=db, curve_dir=tmp_path) == out
    assert simulate(run_id, n=300, method="bootstrap", seed=5, db=db, curve_dir=tmp_path).figures != out.figures
    assert out.to_dict()["figures"]["cagr"] == out.figures["cagr"]


def test_the_permutation_is_the_maximum_drawdown_of_the_same_returns_in_another_order(saved):
    db, tmp_path, run_id, _ = saved
    out = simulate(run_id, n=200, method="permutation", seed=1, db=db, curve_dir=tmp_path)
    assert out.figures == {"max_drawdown": montecarlo.drawdown(returns_of(db, tmp_path, run_id), runs=200, seed=1).value}
    assert out.figures["max_drawdown"]["min"] <= out.figures["max_drawdown"]["max"] <= 0           # a drawdown is never above zero
    assert simulate(run_id, n=200, method="permutation", seed=1, db=db, curve_dir=tmp_path) == out


def test_the_sizes_and_the_method_are_validated(saved):
    db, tmp_path, run_id, _ = saved
    with pytest.raises(BadInput):
        simulate(run_id, n=100_001, method="bootstrap", seed=1, db=db, curve_dir=tmp_path)           # above the cap of runs
    with pytest.raises(BadInput):
        simulate(run_id, n=1, method="permutation", seed=1, db=db, curve_dir=tmp_path)              # the quant module needs at least 2 runs
    with pytest.raises(PlaceboError):
        simulate(run_id, n=10, method="shuffle", seed=1, db=db, curve_dir=tmp_path)
    with pytest.raises(PlaceboError):
        simulate(run_id, n=10, method="bootstrap", seed=1.5, db=db, curve_dir=tmp_path)
    with pytest.raises(results.ResultError):
        simulate(run_id + 100, n=10, method="bootstrap", seed=1, db=db, curve_dir=tmp_path)        # a run that does not exist


def test_the_placebo_compares_the_run_with_random_runs_of_the_same_frequency_and_is_seeded(saved):
    db, tmp_path, run_id, bars = saved
    inputs = PlaceboInputs(bars, ss.costs_of(bars))
    out = simulate(run_id, n=200, method="placebo", seed=3, db=db, curve_dir=tmp_path, placebo=inputs)
    assert simulate(run_id, n=200, method="placebo", seed=3, db=db, curve_dir=tmp_path, placebo=inputs) == out
    assert simulate(run_id, n=200, method="placebo", seed=4, db=db, curve_dir=tmp_path, placebo=inputs) != out
    copy = placebo.profile(results.load_trades(db, run_id), bars)
    assert out.figures["n_trades"] == copy.n_trades and out.figures["mean_hold_bars"] == copy.mean_hold and out.figures["side"] == "long"
    # the same random runs, priced by the sweep: the distribution is theirs, and the p value counts the real run among them
    scores = sweep.sweep_signals(bars, placebo.random_signals(len(bars), n_trades=copy.n_trades, mean_hold=copy.mean_hold, seed=3, count=200), inputs.costs).mtm
    spread = out.figures["placebo"]
    assert spread["min"] == scores.min() and spread["max"] == scores.max() and spread["mean"] == pytest.approx(scores.mean())
    observed = out.figures["observed_mtm_pnl"]
    assert out.p_value == (1 + int((scores >= observed).sum())) / 201 and 1 / 201 <= out.p_value <= 1.0


def test_the_p_value_of_the_best_and_of_the_worst_possible_run_are_the_bounds(db, tmp_path):
    bars = ss.chained_bars(1000)
    inputs = PlaceboInputs(bars, ss.costs_of(bars))
    best = ss.saved_run(db, tmp_path, bars, mtm=D(10) ** 9)                       # a result above every random run: only itself is as good
    worst = ss.saved_run(db, tmp_path, bars, mtm=-D(10) ** 9)                     # below every one: all 100 random runs and itself are at least as good
    assert simulate(best, n=100, method="placebo", seed=1, db=db, curve_dir=tmp_path, placebo=inputs).p_value == 1 / 101
    assert simulate(worst, n=100, method="placebo", seed=1, db=db, curve_dir=tmp_path, placebo=inputs).p_value == 1.0


def test_the_placebo_needs_its_inputs_and_the_bars_of_the_run(saved):
    db, tmp_path, run_id, bars = saved
    with pytest.raises(PlaceboError, match="needs the bars"):
        simulate(run_id, n=10, method="placebo", seed=1, db=db, curve_dir=tmp_path)
    other = ss.chained_bars(1000)[:100]                                           # the first 100 bars: the trades of the run lie beyond them
    with pytest.raises(PlaceboError, match="not the ones of the run"):
        simulate(run_id, n=10, method="placebo", seed=1, db=db, curve_dir=tmp_path, placebo=PlaceboInputs(other, ss.costs_of(other)))
    with pytest.raises(PlaceboError, match="bad_run"):
        simulate(run_id + 100, n=10, method="placebo", seed=1, db=db, curve_dir=tmp_path, placebo=PlaceboInputs(bars, ss.costs_of(bars)))


def test_a_failed_run_has_no_placebo(db, tmp_path):
    bars = ss.chained_bars(300)
    with db.write() as u:
        run_id = u.session.insert("backtest_runs", {"kind": "run", "spec_json": "{}", "spec_hash": "h", "engine_version": "e", "seed": 1, "status": "running"})
    results.record_failure(db, run_id, "engine_unavailable")
    with pytest.raises(PlaceboError, match="bad_run"):
        simulate(run_id, n=10, method="placebo", seed=1, db=db, curve_dir=tmp_path, placebo=PlaceboInputs(bars, ss.costs_of(bars)))
