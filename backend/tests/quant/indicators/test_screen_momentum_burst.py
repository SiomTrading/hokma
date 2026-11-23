"""CTS-004: momentum burst screen. Hand built bars with known trigger dates, the fill rule, closed bars only and bad input."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators import _common
from hokma.quant.indicators.screens import momentum_burst as mb

from .helpers import stamp

IDX = pd.bdate_range("2024-01-02", periods=60)


UP = (20.84, 20.85, 20.3, 1100.0, 20.3)          # a gap up burst: close, high, low, volume, open


def prefix(base: float = 20.0, n: int = 35) -> list[tuple[float, float, float, float, float]]:
    """35 bars (close, high, low, volume): wide ranges (2.0) for 20 bars, then a contracted base (range 0.6) for 15."""
    rows = []
    for i in range(n):
        if i < 20:
            close = base if i % 2 == 0 else base + 0.2
            rows.append((close, close + 1.0, close - 1.0, 1000.0))
        else:
            close = base if i % 2 == 0 else base + 0.1
            rows.append((close, close + 0.3, close - 0.3, 1000.0))
    return rows


def frame(rows) -> pd.DataFrame:
    close = np.array([r[0] for r in rows])
    previous = np.r_[close[0], close[:-1]]
    open_ = np.array([r[4] if len(r) > 4 else previous[i] for i, r in enumerate(rows)])
    return pd.DataFrame({"open": open_, "high": [r[1] for r in rows], "low": [r[2] for r in rows], "close": close,
                         "volume": [r[3] for r in rows]}, index=IDX[:len(rows)])


def with_bar(bar, base: float = 20.0, edit=None):
    rows = prefix(base)
    for pos, row in (edit or {}).items():
        rows[pos] = row
    rows.append(bar)
    return frame(rows)


def run(bars: pd.DataFrame, **kw):
    return mb.momentum_burst(bars, as_of=stamp(bars, len(bars) - 1), **kw)


def test_percent_trigger_only_by_hand():
    bars = with_bar(UP)                                        # +4.2 percent, 0.84 dollars, range 0.55 inside the base range
    value = run(bars).value
    assert value["triggers"] == {"percent": True, "dollar": False, "range": False}
    assert value["signal"] and value["bar_date"] == str(IDX[35].date())


def test_dollar_trigger_by_hand():
    bars = with_bar((30.95, 31.0, 29.9, 1100.0), base=30.0)    # +3.2 percent but +0.95 dollars on a 30 dollar stock
    value = run(bars).value
    assert value["triggers"]["dollar"] is True and value["triggers"]["percent"] is False
    assert value["dollar_gain"] == pytest.approx(0.95)


def test_range_trigger_by_hand():
    bars = with_bar((20.4, 20.5, 19.5, 1000.0))                 # +2 percent, range 1.0 against 0.6, same volume as the day before
    value = run(bars).value
    assert value["triggers"] == {"percent": False, "dollar": False, "range": True}
    assert value["signal"]


def test_a_burst_without_more_volume_is_not_a_percent_trigger():
    bars = with_bar((20.84, 20.85, 20.3, 1000.0, 20.3))        # same gain, volume equal to the day before (not more)
    assert run(bars).value["triggers"]["percent"] is False


def test_the_range_trigger_needs_the_day_before_not_extended():
    rows = {33: (19.2, 19.5, 18.9, 1000.0, 19.4), 34: (20.0, 20.1, 19.5, 1000.0, 19.6)}      # day before: +4.2 percent, range 0.6
    bars = with_bar((20.4, 20.5, 19.5, 1000.0), edit=rows)
    assert run(bars).value["triggers"]["range"] is False


def test_failure_filters_remove_the_signal_but_keep_the_trigger():
    run_up = {31: (19.7, 20.0, 19.4, 1000.0), 32: (19.8, 20.0, 19.4, 1000.0), 33: (19.9, 20.1, 19.5, 1000.0), 34: (20.0, 20.2, 19.6, 1000.0)}
    value = run(with_bar(UP, edit=run_up)).value
    assert value["trigger"] and value["filters"]["run_of_up_days"] and value["signal"] is False
    drop = {34: (19.2, 20.15, 19.0, 1000.0)}                                       # a fall of 4.5 percent the day before the burst
    value = run(with_bar((20.04, 20.05, 19.5, 1100.0, 19.5), edit=drop)).value
    assert value["trigger"] and value["filters"]["recent_drop"] and value["signal"] is False
    assert run(with_bar(UP)).value["signal"] is True                               # the same burst with a clean base is a signal


def test_close_far_from_the_high_is_no_signal():
    value = run(with_bar((20.84, 21.4, 20.3, 1100.0, 20.3))).value
    assert value["trigger"] and value["filters"]["near_high"] is False and value["signal"] is False


def test_trigger_and_signal_dates_over_the_history():
    rows = prefix()
    rows.append(UP)                                                                # burst on bar 35
    rows += [(20.8, 21.1, 20.5, 900.0)] * 3                                        # quiet bars
    bars = frame(rows)
    value = run(bars).value
    assert value["trigger_dates"] == [str(IDX[35].date())] and value["signal_dates"] == [str(IDX[35].date())]


def test_entry_is_the_next_open_plus_one_tick_and_the_stop_is_the_trigger_low():
    value = run(with_bar(UP)).value
    assert value["entry_rule"] == "next_open_plus_1_tick" and value["tick"] == 0.01
    assert value["stop"] == 20.3 and value["stop_rule"] == "trigger_bar_low_exact"
    assert value["entry_is_estimate"] is True and value["entry_estimate"] == pytest.approx(20.85)
    assert value["same_bar_priority"] == "stop_first"
    assert _common.entry_fill(20.10, 0.01) == 20.11
    assert _common.entry_fill(19.99, 0.01) == 20.0
    assert _common.stop_first(next_low=19.9, stop=20.0, entry=20.11) is True
    assert _common.stop_first(next_low=20.05, stop=20.0, entry=20.11) is False


def test_the_dollar_threshold_depends_on_the_price_scale_and_the_result_says_so():
    near = run(with_bar(UP))
    assert not any("dollar_threshold_price_scale" in w for w in near.warnings)
    far = run(with_bar((62.5, 62.6, 61.0, 1100.0, 61.0), base=60.0))
    assert any("dollar_threshold_price_scale" in w for w in far.warnings)
    assert far.value["dollar_threshold_share_of_close"] == pytest.approx(0.9 / 62.5)


def test_only_closed_bars_are_used():
    closed = with_bar(UP)
    rows = prefix()
    rows += [UP, (23.0, 23.1, 20.8, 5000.0)]                                       # the last bar is still open at as_of
    open_bar = frame(rows)
    at = stamp(open_bar, 35)                                                     # just after the close of bar 35, before bar 36 closes
    res_open = mb.momentum_burst(open_bar, as_of=at)
    res_closed = mb.momentum_burst(closed, as_of=at)
    assert res_open.value == res_closed.value and res_open.params["dropped_open_bars"] == 1
    nan_open = open_bar.copy()
    nan_open.iloc[-1] = np.nan                                                    # an open bar can be empty: it is dropped before validation
    assert mb.momentum_burst(nan_open, as_of=at).value == res_closed.value


def test_a_date_only_as_of_does_not_close_that_days_bar():
    bars = with_bar(UP)
    day = str(IDX[35].date())
    assert mb.momentum_burst(bars, as_of=day).value["bar_date"] == str(IDX[34].date())
    assert mb.momentum_burst(bars, as_of=day + "T16:00").value["bar_date"] == day
    assert mb.momentum_burst(bars, as_of=day + "T15:59").value["bar_date"] == str(IDX[34].date())
    flagged = bars.assign(closed=True)
    flagged.loc[flagged.index[-1], "closed"] = False
    assert mb.momentum_burst(flagged, as_of=day + "T17:00").value["bar_date"] == str(IDX[34].date())


def test_a_stale_last_bar_gives_no_value():
    bars = with_bar(UP)
    res = mb.momentum_burst(bars, as_of="2024-06-03T17:00")
    assert res.value is None and "stale" in res.warnings[0]
    assert mb.momentum_burst(bars, as_of=stamp(bars, 35), max_bar_age=0).ok


def test_without_as_of_every_bar_is_taken_as_closed_and_the_result_warns():
    res = mb.momentum_burst(with_bar(UP))
    assert res.ok and any("as_of not given" in w for w in res.warnings)


def test_empty_nan_short_and_inconsistent_bars_give_no_value():
    empty = pd.DataFrame({c: [] for c in ("open", "high", "low", "close", "volume")}, index=pd.DatetimeIndex([]), dtype=float)
    assert mb.momentum_burst(empty, as_of="2024-03-01").value is None
    bars = with_bar(UP)
    nan = bars.copy()
    nan.iloc[10, nan.columns.get_loc("close")] = np.nan
    assert run(nan).value is None
    assert run(bars.iloc[:20]).value is None
    bad = bars.copy()
    bad.iloc[12, bad.columns.get_loc("high")] = 1.0
    assert run(bad).value is None


def test_thresholds_out_of_domain_give_no_value_and_wrong_types_raise():
    bars = with_bar(UP)
    assert run(bars, pct_threshold=-0.01).value is None
    assert run(bars, close_near_high=1.5).value is None
    assert run(bars, base_bars=0).value is None
    with pytest.raises(BadInput):
        run(bars, pct_threshold="x")
    with pytest.raises(BadInput):
        run(bars, base_bars=2.5)
    with pytest.raises(BadInput):
        mb.momentum_burst([1, 2, 3])
    with pytest.raises(BadInput):
        mb.momentum_burst(bars.reset_index(drop=True))


def test_registered_and_called_through_the_registry():
    spec = registry.describe("indicators.screens.momentum_burst")
    assert spec.piece_ids == ("CTS-004",) and spec in registry.list("indicators")
    bars = with_bar(UP)
    res = registry.call("indicators.screens.momentum_burst", bars=bars, as_of=stamp(bars, 35))
    assert res.value["signal"] is True


def test_on_the_fixed_daily_bars_the_scan_is_consistent(spy):
    res = mb.momentum_burst(spy, as_of=stamp(spy, len(spy) - 1))
    assert res.ok
    assert set(res.value["signal_dates"]) <= set(res.value["trigger_dates"])
    table = mb.scan(spy, pct_threshold=0.04, dollar_threshold=0.9, range_lookback=3, extended_pct=0.04, close_near_high=0.7,
                    base_bars=5, base_lookback=20, contraction_ratio=0.8, run_bars=3, drop_pct=0.04, drop_bars=3)
    gain = spy["close"] / spy["close"].shift(1) - 1
    pct_days = spy.index[(gain >= 0.04 - 1e-9) & (spy["volume"] > spy["volume"].shift(1))]
    assert set(table.index[table["trigger_pct"]]) == set(pct_days)
