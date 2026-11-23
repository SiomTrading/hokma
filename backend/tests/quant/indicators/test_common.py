"""Shared plumbing of the indicators module: bar validation, the closed bar gate, line scaling and the decorator."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators import _common as c

from .helpers import tiny


def test_bars_frame_accepts_clean_bars_and_names_the_problem_otherwise():
    frame, warns = c.bars_frame(tiny(), ("open", "high", "low", "close", "volume"))
    assert frame is not None and warns == ()
    upper = tiny().rename(columns=str.upper)
    assert c.bars_frame(upper, ("close",))[0] is not None                                     # column names are case insensitive
    cases = {
        "NaN": tiny().assign(close=lambda d: d["close"].where(d.index != d.index[2])),
        "positive": tiny().assign(low=-1.0), "negative": tiny().assign(volume=-1.0), "inconsistent": tiny().assign(high=1.0),
    }
    for key, bars in cases.items():
        got, why = c.bars_frame(bars, ("open", "high", "low", "close", "volume"))
        assert got is None and key in why[0], key
    assert c.bars_frame(tiny().iloc[::-1], ("close",))[0] is None                              # a decreasing index
    assert c.bars_frame(tiny().iloc[:1], ("close",), min_bars=2)[0] is None
    with pytest.raises(BadInput):
        c.bars_frame(tiny(), ("close", "vwap"))
    with pytest.raises(BadInput):
        c.bars_frame(tiny().astype(str).assign(close="x"), ("close",))
    with pytest.raises(BadInput):
        c.bars_frame(np.zeros(3), ("close",))


def test_a_daily_bar_is_closed_at_16_00_new_york():
    bars = tiny()[["close"]]
    day = bars.index[4]
    kept, dropped, warns = c.closed_bars(bars, f"{day.date()}T15:59")
    assert len(kept) == 4 and dropped == 6 and warns == ()
    assert len(c.closed_bars(bars, f"{day.date()}T16:00")[0]) == 5
    assert len(c.closed_bars(bars, str(day.date()))[0]) == 4                                   # a date alone is the start of that day
    assert len(c.closed_bars(bars, pd.Timestamp(f"{day.date()} 21:00", tz="UTC"))[0]) == 5     # 16:00 in New York in winter is 21:00 UTC
    assert len(c.closed_bars(bars, None)[0]) == 10 and "as_of not given" in c.closed_bars(bars, None)[2][0]


def test_weekly_bars_close_on_their_friday_and_start_from_daily_bars():
    daily = pd.DataFrame({"open": range(1, 11), "high": range(2, 12), "low": range(0, 10), "close": range(1, 11), "volume": [1] * 10},
                         index=pd.bdate_range("2024-01-01", periods=10), dtype=float)
    weekly = c.to_weekly(daily)
    assert list(weekly.index) == [pd.Timestamp("2024-01-05"), pd.Timestamp("2024-01-12")]
    assert weekly.iloc[0].to_dict() == {"open": 1.0, "high": 6.0, "low": 0.0, "close": 5.0, "volume": 5.0}
    assert len(c.closed_weekly(weekly, "2024-01-12T15:00")[0]) == 1
    assert len(c.closed_weekly(weekly, "2024-01-12T17:00")[0]) == 2


def test_sessions_between_counts_business_days():
    assert c.sessions_between("2024-01-05", "2024-01-05") == 0 and c.sessions_between("2024-01-05", "2024-01-08") == 1
    assert c.sessions_between("2024-01-08", "2024-01-05") == -1


def test_fills_use_exact_decimal_arithmetic():
    assert c.entry_fill(100.1, 0.01) == 100.11 and c.entry_fill(0.29, 0.01) == 0.3
    with pytest.raises(ValueError):
        c.entry_fill(-1.0, 0.01)
    assert c.stop_first(9.0, 10.0, 11.0) is True and c.stop_first(10.5, 10.0, 11.0) is False


def test_line_scaling_only_touches_windows_and_an_explicit_value_wins():
    defaults = {"window": 20, "signal_window": 9, "multiplier": 3.0, "name": "x"}
    assert c.resolve_params("DT", defaults, {})["window"] == 10 and c.resolve_params("DT", defaults, {})["signal_window"] == 5
    assert c.resolve_params("LP", defaults, {})["window"] == 40 and c.resolve_params("LP", defaults, {})["multiplier"] == 3.0
    assert c.resolve_params("SWC", defaults, {})["window"] == 20 and c.resolve_params(None, defaults, {})["window"] == 20
    assert c.resolve_params("DT", defaults, {"window": 7})["window"] == 7
    assert c.scale_window(50, "DT") == 25 and c.scale_window(2, "DT") == 2
    with pytest.raises(BadInput):
        c.resolve_params("XX", defaults, {})
    with pytest.raises(BadInput):
        c.resolve_params("SWO", defaults, {"bogus": 1})
    with pytest.raises(BadInput):
        c.resolve_params("SWO", defaults, {"window": True})
    with pytest.raises(BadInput):
        c.resolve_params("SWO", defaults, {"multiplier": "x"})
    with pytest.raises(BadInput):
        c.resolve_params("SWO", defaults, {"name": 3})


def test_domain_problem_and_results():
    assert c.domain_problem({"window": 0}) and c.domain_problem({"multiplier": float("nan")}) and c.domain_problem({"a": 3, "b": 2.0}) is None
    assert c.empty("x", "u", "m", "why").warnings == ("why",)
    assert c.build("x", {}, unit="u", method="m", params={}, piece_ids=(), inputs=()).value is None
    assert c.build("x", {"state": None}, unit="u", method="m", params={}, piece_ids=(), inputs=()).value == {"state": None}      # a state of None is a result
    assert c.series_value(pd.Series([np.nan, np.inf]), "s", 5) is None
    assert c.series_value(pd.Series([1.0, 2.0, 3.0]), "s", 2)["index"] == ["1", "2"]
    with pytest.raises(BadInput):
        c.check_last_n(0)
    with pytest.raises(BadInput):
        c.check_last_n(1.5)


def test_every_indicator_function_is_registered_in_the_indicators_module_with_a_summary():
    specs = registry.list("indicators")
    assert len(specs) >= 60
    for spec in specs:
        assert spec.module == "indicators" and spec.name.startswith("indicators.") and spec.summary and spec.piece_ids
        assert spec.name in registry.describe(spec.name).name
