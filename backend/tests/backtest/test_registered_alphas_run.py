"""The registered alphas that read only bars (and the indicators of H3 they ask for) run in a backtest with the real fixtures (SPEC-020: AH-12, AS-006,
AS-020a, AS-020b, AS-020g): the daily bars of SPY of H3 for the swing and long term lines and the real minute bars of NQ for the intraday ones. An alpha that
reads a fact that is not made of bars (a yield, a rank, the bars of a partner) needs a feed: `test_every_other_alpha_names_the_facts_it_waits_for`."""
from decimal import Decimal as D

import pytest
from backtest import support
from backtest.runner_support import NQ, source_of
from backtest.session_support import session_bars
from backtest.support import requires_engine
from backtest.test_fundamental_runs import INSTRUMENT, SHEET, bars as spy_bars, source as spy_source
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma import backtest
from hokma.backtest.runner import Services
from hokma.factory.spec import default_registry

BAR_FACTS = {"open", "high", "low", "close", "volume"}
SWING = ["ema_cross", "macd", "rsi_reversion", "historical_returns", "tsmom", "momentum_swc", "mean_reversion_swc", "trend_channel_swc_fast", "trend_channel_swc_slow"]
INTRADAY = ["intraday_bot_fade", "intraday_bot_trend", "trend_regime_dt"]
REGISTRY = default_registry()


def facts_needed(alpha_id):
    alpha = REGISTRY.build(alpha_id)
    return set(alpha.FACTS) - BAR_FACTS - {q.name for q in getattr(alpha, "requests", lambda: ())()}


@pytest.mark.parametrize("alpha_id", SWING)
@requires_engine
def test_a_swing_or_long_term_alpha_runs_on_the_daily_bars_of_spy(alpha_id, db, tmp_path):
    assert facts_needed(alpha_id) == set()
    rows = spy_bars()
    entry = REGISTRY.get(alpha_id)
    line = entry.line or "SWC"
    spec = support.spec(strategy_id=alpha_id, params={}, line=line, instruments=(INSTRUMENT,), bar="1d", start=rows[0].ts, end=rows[-1].ts, as_of=rows[-1].ts,
                        capital_usd=D(100_000))
    result = backtest.run(spec, services=Services(db=db, registry=REGISTRY, source=spy_source, curve_dir=tmp_path))
    assert result.status == "done", result.warnings
    assert len(result.equity.timestamps) == len(rows)                       # the curve is marked at every bar of the run


@pytest.mark.parametrize("alpha_id", INTRADAY)
@requires_engine
def test_an_intraday_alpha_runs_on_the_minute_bars_of_nq(alpha_id, db, tmp_path):
    assert facts_needed(alpha_id) == set()
    rows = session_bars("day")
    spec = support.spec(strategy_id=alpha_id, params={}, line="DT", instruments=(NQ,), bar="1m", start=rows[0].ts, end=rows[-1].ts, as_of=rows[-1].ts)
    result = backtest.run(spec, services=Services(db=db, registry=REGISTRY, source=source_of({NQ: rows}), curve_dir=tmp_path))
    assert result.status == "done", result.warnings


def test_every_other_alpha_names_the_facts_it_waits_for():
    """These alphas need a source that is not the bars of the instrument: the statements, a partner's bars, a rate, a chain. They run through a feed."""
    waiting = {i: facts_needed(i) for i, e in REGISTRY.entries.items() if e.kind == "alpha" and i not in SWING + INTRADAY}
    assert {i for i, v in waiting.items() if not v} == {"mean_reversion_dt"}      # its lookback (390 bars) is the whole session of the fixture: no bar is left to decide on
    assert waiting["deep_value_lp_equity"] == {"earnings_yield", "fcf_yield", "book_to_price"} and waiting["pairs_correlation"] == {"pair_close"}
    assert waiting["premium_wheel_swo"] == {"iv_rv_ratio", "shares_held"} and waiting["momentum_lp_dual"] == {"risk_free"}
