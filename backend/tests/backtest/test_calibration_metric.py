"""The metric of Caleb on every day (SPEC-020, section 4.9, step 2): the implied volatility of the chain, the Yang-Zhang volatility of `quant.stats` and the grid of
thresholds. The bars are the real daily bars of SPY of the repo (`tests/quant/fixtures/ohlcv_daily_spy.csv`); the one recorded chain of the repo is a single day."""
import dataclasses
import math
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from backtest.calibration_support import alpaca_day, quote

from hokma.backtest.calibration import gate as gate_mod
from hokma.backtest.calibration import premium
from hokma.backtest.calibration.premium import ChainDay, ChainHistory
from hokma.quant.stats import volatility as stats_volatility

SPY = Path(__file__).resolve().parents[1] / "quant" / "fixtures" / "ohlcv_daily_spy.csv"


def spy_bars() -> pd.DataFrame:
    return pd.read_csv(SPY, parse_dates=["date"]).set_index("date")[["open", "high", "low", "close"]]


def spy_history(*, iv=0.20, limit=None) -> ChainHistory:
    """The real bars of SPY with one expiration 40 days out and the call and the put of the strike nearest the spot, each with the implied volatility `iv`
    (a number, or a function of the position of the day). The quotes are hand made; only the bars are recorded."""
    bars = spy_bars().iloc[:limit]
    days = []
    for k, (stamp, row) in enumerate(bars.iterrows()):
        spot = round(float(row["close"]), 2)
        strike = round(spot / 5) * 5
        value = iv(k) if callable(iv) else iv
        expiry = stamp.date() + timedelta(days=40)
        days.append(ChainDay(stamp.date(), Decimal(str(spot)), (quote(expiry, "call", strike, "1.00", "1.10", value), quote(expiry, "put", strike, "1.00", "1.10", value))))
    return ChainHistory("SPY", tuple(days), bars)


def test_the_one_recorded_day_gives_its_atm_volatility_at_the_expiration_nearest_the_middle_of_the_window():
    """AAPL on 2026-11-20, spot 228.40. The window 21 to 60 days holds 2026-12-18 (28 days) and 2027-01-15 (56 days); the middle is 40.5, so 12.5 against 15.5 picks
    2026-12-18. The strikes go every 5: 230 is 1.60 from the spot and 225 is 3.40, so the at the money strike is 230. The value is the mean of its call and put."""
    chain = alpaca_day()
    gate = gate_mod.load_default()
    assert premium._pick_expiry(chain, gate.dte) == date(2026, 12, 18)
    call = chain.quote(date(2026, 12, 18), "call", Decimal(230))
    put = chain.quote(date(2026, 12, 18), "put", Decimal(230))
    assert call.iv is not None and put.iv is not None and call.bid <= call.ask and put.bid <= put.ask
    assert premium.atm_iv(chain, gate.dte) == pytest.approx((call.iv + put.iv) / 2)
    assert premium.atm_iv(chain, (60, 70)) is not None and premium.atm_iv(chain, (200, 300)) is None      # no expiration inside the window: no value


def test_a_chain_with_no_implied_volatility_has_no_metric():
    chain = ChainDay(date(2024, 1, 2), Decimal(100), (quote(date(2024, 2, 9), "call", 100, "1", "1.1"), quote(date(2024, 2, 9), "put", 100, "1", "1.1")))
    assert premium.atm_iv(chain, (21, 60)) is None
    odd = ChainDay(date(2024, 1, 2), Decimal(100), (quote(date(2024, 2, 9), "call", 100, "1", "1.1", 0.0), quote(date(2024, 2, 9), "put", 100, "1", "1.1", 0.2)))
    assert premium.atm_iv(odd, (21, 60)) is None                                              # a volatility of zero is no volatility


def test_the_realized_volatility_is_the_root_mean_square_of_four_complete_weeks_of_the_yang_zhang_of_the_stats_module():
    """Computed here by hand from `stats.yang_zhang` on the same bars: for a day, the weeks that ended before it (Sunday earlier than the day), the last four,
    the root of the mean of their squares, times the root of 52."""
    history = spy_history()
    rv = premium.realized_volatility(history)
    bars = history.bars
    weekly = stats_volatility.yang_zhang(bars["open"], bars["high"], bars["low"], bars["close"], "weekly").value
    ends = [date.fromisoformat(label.split("/")[1]) for label in weekly["index"]]
    values = np.asarray(weekly["values"])
    for position in (30, 250, 600, 999):
        day = history.days[position].day
        done = [i for i, end in enumerate(ends) if end < day]
        last4 = values[done[-4:]]
        assert rv[position] == pytest.approx(math.sqrt(float(np.mean(last4 ** 2))) * math.sqrt(52))
    assert np.isnan(rv[:15]).all() and np.isfinite(rv[25:]).all()                             # four weeks are needed: the first days have nothing
    assert 0.05 < np.nanmean(rv) < 0.40                                                       # a plausible annual volatility for an index


def test_the_realized_volatility_of_a_day_does_not_move_when_later_bars_change_point_in_time():
    history = spy_history()
    full = premium.realized_volatility(history)
    cut_at = history.days[500].day
    cut = premium.realized_volatility(history.until(cut_at))
    assert len(cut) == 501 and np.array_equal(cut, full[:501], equal_nan=True)
    shaken = history.bars.copy()
    shaken.iloc[501:] = shaken.iloc[501:] * 3.0                                               # a different future
    other = premium.realized_volatility(ChainHistory("SPY", history.days, shaken))
    assert np.array_equal(other[:502], full[:502], equal_nan=True)                           # the week of day 501 had not ended on day 501


def test_a_history_cut_reads_nothing_after_its_day():
    history = spy_history(limit=100)
    cut = history.until(history.days[49].day)
    assert len(cut.days) == 50 and cut.bars.index.max().date() == history.days[49].day and history.days[50].day > cut.days[-1].day


def test_the_three_metrics_are_what_their_names_say():
    history = spy_history(iv=0.22, limit=300)
    rv = premium.realized_volatility(history)
    gate = gate_mod.load_default()
    ratio = premium.metric_series(history, gate)
    diff = premium.metric_series(history, dataclasses.replace(gate, metric="iv_minus_rv"))
    assert ratio[200] == pytest.approx(0.22 / rv[200]) and diff[200] == pytest.approx(0.22 - rv[200])
    assert np.isnan(ratio[:15]).all() and np.isnan(diff[:15]).all()


def test_the_rank_of_the_implied_volatility_is_its_place_between_the_lowest_and_the_highest_of_the_year():
    """The implied volatility is 0.20 for 70 days, then 0.30 and 0.25. On the day it is 0.25 the days so far span 0.20 to 0.30: (0.25 - 0.20) / (0.30 - 0.20) = 50."""
    def level(k):
        return {70: 0.30, 71: 0.25}.get(k, 0.20)
    history = spy_history(iv=level, limit=80)
    rank = premium.metric_series(history, dataclasses.replace(gate_mod.load_default(), metric="iv_rank"))
    assert rank[71] == pytest.approx(50.0) and rank[70] == pytest.approx(100.0) and rank[72] == pytest.approx(0.0)
    assert np.isnan(rank[:59]).all() and np.isnan(rank[60])                                  # 60 snapshots are needed and a flat window has no range


def test_the_grid_is_the_percentiles_ten_to_ninety_of_the_metric_alone():
    metric = np.concatenate([np.arange(1.0, 102.0), [np.nan, np.nan]])
    assert premium.threshold_grid(metric) == [11.0, 21.0, 31.0, 41.0, 51.0, 61.0, 71.0, 81.0, 91.0]
    assert premium.threshold_grid(np.full(100, 2.0)) == [2.0]                                  # a flat metric repeats one value: one threshold, not nine
