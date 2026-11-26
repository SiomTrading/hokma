"""Known traps of the metrics of a run, kept as fixed tests (SPEC-020: B-48, L-36). A version that falls into the trap fails here."""
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pandas as pd

from hokma.backtest.results import BarRange, Fill, group_trades
from hokma.quant import registry
from hokma.quant.metrics import returns as returns_module  # noqa: F401  (importing registers metrics.cagr_by_date)

UTC = timezone.utc
NQ = "FUT:NQ:2026-03"


def cagr_by_bars(returns: pd.Series, periods_per_year: int = 252) -> float:
    """The wrong version: years as the number of bars over the periods of a year."""
    total = float((1.0 + returns).prod())
    return total ** (periods_per_year / len(returns)) - 1.0


def cagr_by_date(returns: pd.Series) -> float:
    """The right version, worked out here by hand: years are the calendar days from the first to the last date over 365.25."""
    days = (returns.index[-1] - returns.index[0]).days
    return float((1.0 + returns).prod()) ** (365.25 / days) - 1.0


def test_cagr_of_eighteen_months_uses_fractional_years_by_date_and_not_by_number_of_bars_B_48():
    index = pd.date_range("2024-01-31", periods=18, freq="ME")         # month ends: 516 days from the first to the last
    series = pd.Series([0.02, 0.0] * 9, index=index)                   # total return 1.02^9 - 1 = 19.5%
    expected = (1.02 ** 9) ** (365.25 / 516) - 1                       # about 13.4% a year
    got = registry.call("metrics.cagr_by_date", returns=series).value
    assert math.isclose(got, expected, rel_tol=1e-12)
    assert math.isclose(got, cagr_by_date(series), rel_tol=1e-12)
    wrong = cagr_by_bars(series)                                       # 18 bars over 252 is 0.07 years: a CAGR above 1000 percent
    assert wrong > 10 and not math.isclose(got, wrong, rel_tol=1e-3)   # a version that counts bars does not pass
    assert 0.13 < got < 0.14


def test_a_grouping_from_flat_to_reduce_never_leaves_mae_and_mfe_at_zero_L_36():
    t0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    bars = [BarRange(t0 + timedelta(minutes=i), D(h), D(l)) for i, (h, l) in enumerate([("20005", "19995"), ("20015", "20000"), ("20025", "20008"), ("20040", "20020")])]
    # flat, buy 2 at 20000 (bar 0), reduce 1 at 20010 (bar 2), reduce the last at 20030 (bar 3); multiplier 20
    fills = [Fill(NQ, "buy", D(2), D("20000"), t0), Fill(NQ, "sell", D(1), D("20010"), t0 + timedelta(minutes=2)),
             Fill(NQ, "sell", D(1), D("20030"), t0 + timedelta(minutes=3))]
    g = group_trades(fills, {NQ: bars}, {NQ: D(20)})
    # first reduce (bars 0..2): MAE (19995 - 20000) * 20 = -100, MFE (20025 - 20000) * 20 = 500
    # second reduce (bars 0..3): MAE -100, MFE (20040 - 20000) * 20 = 800
    assert [(t.mae_usd, t.mfe_usd) for t in g.exit_trades] == [(D(-100), D(500)), (D(-100), D(800))]
    # the entry and the position (qty 2, bars 0..3): MAE (19995 - 20000) * 40 = -200, MFE (20040 - 20000) * 40 = 1600
    assert [(t.mae_usd, t.mfe_usd) for t in g.entry_trades] == [(D(-200), D(1600))] == [(t.mae_usd, t.mfe_usd) for t in g.positions]
    for group in (g.entry_trades, g.exit_trades, g.positions):
        assert all(t.mae_usd != 0 and t.mfe_usd != 0 for t in group)
