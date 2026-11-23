"""CTS-028: distribution day monitor. Counts d5, d15, d25 and the risk level against hand built sessions."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators.screens import distribution_days as dd

from .helpers import stamp

IDX = pd.bdate_range("2024-01-02", periods=70)


def series(n: int, drops: dict[int, float], jump: dict[int, float] | None = None, low_volume: tuple[int, ...] = ()):
    """Closes rise 0.1 percent a day, volume falls by 10 a day. drops: position -> fall (fraction) with more volume than the day before.
    A position in low_volume falls with less volume (so it is not a distribution day)."""
    close, volume = [100.0], [10_000.0]
    for i in range(1, n):
        c = close[-1] * 1.001
        v = 10_000.0 - 10 * i
        if i in drops:
            c = close[-1] * (1 - drops[i])
            v = volume[-1] + 500 if i not in low_volume else volume[-1] - 50
        if jump and i in jump:
            c = jump[i]
        close.append(round(c, 4))
        volume.append(v)
    return pd.DataFrame({"close": close, "volume": volume}, index=IDX[:n])


def run(bars, **kw):
    return dd.distribution_days(bars, as_of=stamp(bars, len(bars) - 1), **kw)


def test_flags_by_hand_including_the_exact_threshold():
    close = pd.Series([100.0, 99.8, 99.81, 99.0, 98.0])
    volume = pd.Series([10.0, 20.0, 30.0, 25.0, 40.0])
    flags = dd.distribution_flags(close, volume, 0.002)
    assert list(flags) == [False, True, False, False, True]       # -0.2 percent exactly counts; 99.81 is up; 99.0 falls on less volume
    assert not dd.distribution_flags(pd.Series([100.0, 99.81]), pd.Series([1.0, 2.0]), 0.002).iloc[1]     # -0.19 percent does not
    assert not dd.distribution_flags(pd.Series([100.0, 99.0]), pd.Series([2.0, 2.0]), 0.002).iloc[1]      # equal volume is not more volume


def test_counts_and_status_by_hand():
    drops = {5: 0.005, 12: 0.002, 20: 0.0019, 25: 0.01, 30: 0.003, 36: 0.004, 38: 0.0025}
    bars = series(40, drops, jump={14: 110.0}, low_volume=(25,))
    value = run(bars).value
    found = {d["date"]: d for d in value["distribution_days"]}
    assert set(found) == {str(IDX[p].date()) for p in (5, 12, 30, 36, 38)}                      # 20 falls too little, 25 on less volume
    assert [found[str(IDX[p].date())]["status"] for p in (5, 12, 30, 36, 38)] == ["rallied_out", "rallied_out", "active", "active", "active"]
    assert (value["d5"], value["d15"], value["d25"]) == (2, 3, 3)                                # ages 3 and 1; 9, 3 and 1; the same three
    assert value["risk"] == "CAUTION" and value["base_risk"] == "CAUTION" and value["ma_filter"] == "skipped_under_50_bars"


def test_a_day_leaves_after_25_sessions():
    bars = series(40, {5: 0.005})
    at_24 = dd.distribution_days(bars.iloc[:30], as_of=stamp(bars, 29)).value                    # position 29: age 24
    at_25 = dd.distribution_days(bars.iloc[:31], as_of=stamp(bars, 30)).value                    # position 30: age 25
    assert at_24["d25"] == 1 and at_24["distribution_days"][0]["status"] == "active" and at_24["distribution_days"][0]["age"] == 24
    assert at_25["d25"] == 0 and at_25["distribution_days"][0]["status"] == "expired"
    assert at_24["d15"] == 0 and at_24["d5"] == 0


def test_a_day_leaves_with_a_5_percent_rally_from_its_close():
    bars = series(30, {5: 0.005})
    base = float(bars["close"].iloc[5])
    just_under = bars.copy()
    just_under.iloc[10, 0] = round(base * 1.0499, 4)
    assert run(just_under).value["d25"] == 1
    enough = bars.copy()
    enough.iloc[10, 0] = round(base * 1.0501, 4)
    value = run(enough).value
    assert value["d25"] == 0 and value["distribution_days"][0]["status"] == "rallied_out"


def test_risk_levels_and_the_moving_average_step_by_hand():
    levels = [(0, "NORMAL"), (2, "NORMAL"), (3, "CAUTION"), (4, "CAUTION"), (5, "HIGH"), (6, "HIGH"), (7, "SEVERE"), (12, "SEVERE")]
    for count, level in levels:
        assert dd.risk_level(count, 3, 5, 7, 0) == level
    assert dd.risk_level(5, 3, 5, 7, 1) == "SEVERE" and dd.risk_level(5, 3, 5, 7, -1) == "CAUTION"
    assert dd.risk_level(0, 3, 5, 7, -1) == "NORMAL" and dd.risk_level(9, 3, 5, 7, 1) == "SEVERE"


def test_the_ma_filter_moves_the_level_one_step():
    drops = {i: 0.003 for i in (45, 48, 52, 55, 58)}                       # five distribution days inside the last 25 sessions
    rising = series(60, drops)
    value = run(rising).value
    assert value["d25"] == 5 and value["base_risk"] == "HIGH"
    assert value["ma_filter"] == "above_both" and value["risk"] == "CAUTION"      # above the 21 day EMA and the 50 day SMA: one step down
    falling = rising.copy()
    falling.iloc[-1, 0] = float(falling["close"].min()) * 0.9
    value = run(falling).value
    assert value["ma_filter"] == "below_both" and value["risk"] == "SEVERE"
    off = run(rising, use_ma_filter=False).value
    assert off["ma_filter"] == "off" and off["risk"] == "HIGH"


def test_missing_volume_is_a_warning_and_never_a_distribution_day():
    bars = series(40, {30: 0.005})
    bars.iloc[30, 1] = np.nan
    res = run(bars)
    assert res.value["d25"] == 0 and any("volume missing on" in w for w in res.warnings)
    assert dd.distribution_flags(bars["close"], bars["volume"], 0.002).iloc[30] == False          # noqa: E712
    none = bars.assign(volume=np.nan)
    gone = run(none)
    assert gone.value is None and "volume is missing" in gone.warnings[0]
    assert run(bars[["close"]]).value is None


def test_the_result_says_the_thresholds_are_uncalibrated_and_the_etf_is_a_proxy():
    res = run(series(40, {}))
    assert any(w.startswith("thresholds_uncalibrated") for w in res.warnings)
    assert any(w.startswith("etf_proxy_not_index") for w in res.warnings)
    assert not any(w.startswith("etf_proxy_not_index") for w in run(series(40, {}), is_etf_proxy=False).warnings)


def test_only_closed_bars_are_used():
    bars = series(40, {30: 0.005})
    open_bar = pd.concat([bars, pd.DataFrame({"close": [80.0], "volume": [99_999.0]}, index=IDX[40:41])])      # a crash that is still open
    at = stamp(bars, 39)
    a, b = dd.distribution_days(open_bar, as_of=at), dd.distribution_days(bars, as_of=at)
    assert a.value == b.value and a.params["dropped_open_bars"] == 1
    assert dd.distribution_days(open_bar, as_of=stamp(open_bar, 40)).value["d5"] == 1


def test_nan_close_short_input_and_bad_thresholds_give_no_value():
    bars = series(40, {})
    nan = bars.copy()
    nan.iloc[20, 0] = np.nan
    assert run(nan).value is None
    assert run(bars.iloc[:10]).value is None
    assert dd.distribution_days(bars.iloc[:0], as_of="2024-03-01").value is None
    assert run(bars, drop_pct=-1.0).value is None
    assert run(bars, caution=6, high=5, severe=7).value is None
    assert run(bars, session_window=2).value is None
    with pytest.raises(BadInput):
        run(bars, session_window=2.5)
    with pytest.raises(BadInput):
        dd.distribution_days("x")
    with pytest.raises(BadInput):
        run(bars.assign(close=-1.0).drop(columns="volume").rename(columns={"close": "price"}))


def test_on_the_fixed_daily_bars_the_counts_match_a_direct_count(spy):
    res = dd.distribution_days(spy, as_of=stamp(spy, len(spy) - 1), is_etf_proxy=False)
    assert res.ok
    close, volume = spy["close"], spy["volume"]
    change = close / close.shift(1) - 1
    flags = ((change <= -0.002 + 1e-9) & (volume > volume.shift(1))).to_numpy()
    last = len(spy) - 1
    rally = [bool(close.iloc[p + 1:].max() >= close.iloc[p] * 1.05 - 1e-9) if p < last else False for p in range(len(spy))]
    active = [p for p in np.flatnonzero(flags) if last - p < 25 and not rally[p]]
    assert res.value["d25"] == len(active)
    assert res.value["d5"] == sum(1 for p in active if last - p < 5)


def test_registered_with_its_piece_id():
    assert registry.describe("indicators.screens.distribution_days").piece_ids == ("CTS-028",)
    bars = series(40, {5: 0.005})
    assert registry.call("indicators.screens.distribution_days", bars=bars, as_of=stamp(bars, 39)).ok
