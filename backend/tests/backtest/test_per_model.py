"""One backtest per registered alpha, with its Sharpe and PSR and the mark against the realistic expectation (SPEC-020: AH-37). The runs use the real daily
sessions of NQ 12-25; the indicator functions that the alphas ask for are small causal functions written in this test (the library of H3 supplies them in
production)."""
from decimal import Decimal as D

import numpy as np
import pytest
from backtest.runner_support import NQ, source_of, spec
from backtest.session_support import session_bars
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.backtest import per_model, trials
from hokma.backtest.spec import SpecError
from hokma.factory.orders import Size, SizeType
from hokma.factory.registry import Registry
from hokma.factory.strategy import Composite

pytestmark = requires_engine
DAILY = session_bars("daily")


def ema(values, length):
    out = np.full(len(values), np.nan)
    alpha, acc = 2.0 / (length + 1), None
    for i, v in enumerate(values):
        acc = v if acc is None else alpha * v + (1 - alpha) * acc
        out[i] = acc if i >= length - 1 else np.nan
    return out


def rsi(values, length):
    out = np.full(len(values), np.nan)
    for i in range(length, len(values)):
        change = np.diff(values[i - length:i + 1])
        gain, loss = change[change > 0].sum(), -change[change < 0].sum()
        out[i] = 100.0 if loss == 0 else 100.0 - 100.0 / (1 + gain / loss)
    return out


LIBRARY = {"ema": ema, "rsi": rsi}


class OneContract:
    def size(self, *, signal, ctx):
        return Size(SizeType.CONTRACTS, D(1))


class NoExit:
    def levels(self, *, side, ctx):
        return None, None


def make(alpha_id, alpha):
    composite = Composite(alpha, OneContract(), NoExit(), contract_id=NQ, desk_id=1, line="SWC", library=LIBRARY, tag=alpha_id)
    composite.protective_orders = False
    return composite


def base_spec():
    return spec(DAILY, line="SWC", bar="1d")


def test_the_sharpe_of_0_33_is_below_the_realistic_expectation_AH_37():
    from hokma.desks.agents.judge import REPLICATION_REFERENCE

    assert per_model.realistic_sharpe() == REPLICATION_REFERENCE["median_sharpe"] == 0.37          # one figure: the judge's, read and never copied
    assert per_model.below_realistic_expectation(0.33) is True                 # the example of the row: not operable
    assert per_model.below_realistic_expectation(0.37) is False and per_model.below_realistic_expectation(1.2) is False
    assert per_model.below_realistic_expectation(-0.5) is True
    assert per_model.below_realistic_expectation(None) is False
    assert per_model.below_realistic_expectation(0.5, threshold=0.6) is True        # the threshold is a parameter, the judge's figure when none is given


def test_one_backtest_per_registered_alpha_each_with_its_sharpe_and_psr_AH_37(db, tmp_path):
    out = per_model.run_per_alpha(base_spec(), make, registry=Registry.with_builtins(), alpha_ids=["ema_cross", "rsi_reversion"],
                                  source=source_of({NQ: DAILY}), db=db, curve_dir=tmp_path)
    assert [m.alpha_id for m in out] == ["ema_cross", "rsi_reversion"]
    # on these 70 real sessions the EMA cross loses (Sharpe under zero, PSR low) and the RSI reversion does not: the mark follows the Sharpe
    assert out[0].sharpe < 0 and out[0].psr < 0.5 and out[0].below_realistic_expectation
    assert out[1].sharpe > per_model.realistic_sharpe() and out[1].psr > 0.9 and not out[1].below_realistic_expectation
    for m in out:
        assert m.result.status == "done", m.result.warnings
        assert m.sharpe == m.result.stats["sharpe"].value
        assert m.psr is None or 0.0 <= m.psr <= 1.0
        assert m.below_realistic_expectation == (m.sharpe is not None and m.sharpe < 0.37)
        assert (per_model.BELOW in m.warnings) == m.below_realistic_expectation
        assert trials.count(m.alpha_id) == 1                                      # a trial of its own alpha
    with db.read() as u:
        rows = u.session.query("SELECT strategy_id, kind, status FROM backtest_runs ORDER BY id")
    assert [(r["strategy_id"], r["kind"], r["status"]) for r in rows] == [("ema_cross", "run", "done"), ("rsi_reversion", "run", "done")]


def test_the_threshold_decides_the_mark_AH_37(db, tmp_path):
    kw = dict(registry=Registry.with_builtins(), alpha_ids=["ema_cross"], source=source_of({NQ: DAILY}), db=db)
    always = per_model.run_per_alpha(base_spec(), make, realistic_sharpe=1e9, curve_dir=tmp_path / "a", **kw)[0]
    never = per_model.run_per_alpha(base_spec(), make, realistic_sharpe=-1e9, curve_dir=tmp_path / "b", **kw)[0]
    assert always.sharpe is not None and always.sharpe == never.sharpe
    assert always.below_realistic_expectation and not never.below_realistic_expectation


def test_the_alphas_that_are_not_alphas_and_bad_lists_are_refused_AH_37(db, tmp_path):
    registry = Registry.with_builtins()
    for bad in ([], ["nope"], ["ema_cross", "ema_cross"]):
        with pytest.raises((SpecError, KeyError)):
            per_model.run_per_alpha(base_spec(), make, registry=registry, alpha_ids=bad, source=source_of({NQ: DAILY}), db=db, curve_dir=tmp_path)


def test_a_failed_alpha_run_is_returned_with_its_failure_and_does_not_stop_the_others_AH_37(db, tmp_path):
    class Broken(Composite):
        def on_bar(self, ctx):
            raise ValueError("this strategy cannot decide")

    def picky(alpha_id, alpha):
        if alpha_id == "ema_cross":
            broken = Broken(alpha, OneContract(), NoExit(), contract_id=NQ, desk_id=1, line="SWC", library=LIBRARY)
            broken.protective_orders = False
            return broken
        return make(alpha_id, alpha)

    first, second = per_model.run_per_alpha(base_spec(), picky, alpha_ids=["ema_cross", "rsi_reversion"], source=source_of({NQ: DAILY}), db=db,
                                            curve_dir=tmp_path)
    assert first.result.status == "failed" and "failed_stage:signal" in first.warnings and first.sharpe is None and not first.below_realistic_expectation
    assert "sharpe_unavailable" in first.warnings and first.psr is None
    assert second.result.status == "done"
    assert trials.count("ema_cross") == 1 and len(trials.failed("ema_cross")) == 1


def test_without_a_make_strategy_the_alpha_goes_in_the_composite_with_the_indicators_of_h3(db, tmp_path):
    out = per_model.run_per_alpha(base_spec(), registry=Registry.with_builtins(), alpha_ids=["ema_cross", "rsi_reversion"],
                                  source=source_of({NQ: DAILY}), db=db, curve_dir=tmp_path)
    for m in out:
        assert m.result.status == "done" and m.result.trades and {t.qty for t in m.result.trades} == {1}, m.result.warnings
        assert m.below_realistic_expectation == (m.sharpe is not None and m.sharpe < per_model.realistic_sharpe())      # the default is the judge's figure
