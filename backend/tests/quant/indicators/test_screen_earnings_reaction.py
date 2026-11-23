"""CTS-002: earnings reaction score. Five factors by hand, unvalidated weights flagged, closed bars only."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators.screens import earnings_reaction as er

from .helpers import stamp

R = 230


def bars(pre: float = 105.0, open_r: float = 110.0) -> pd.DataFrame:
    """260 flat sessions at 100; the close before the reaction is 105; the reaction opens at 110, closes at 112 on 4 million shares."""
    n = 260
    close = np.full(n, 100.0)
    close[R - 1] = pre
    close[R] = 112.0
    open_ = np.r_[close[0], close[:-1]]
    frame = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + 0.5, "low": np.minimum(open_, close) - 0.5, "close": close,
                          "volume": np.full(n, 1_000_000.0)}, index=pd.bdate_range("2023-01-02", periods=n))
    frame.iloc[R, frame.columns.get_loc("open")] = open_r
    frame.iloc[R, frame.columns.get_loc("high")] = max(open_r, 112.0) + 0.5
    frame.iloc[R, frame.columns.get_loc("low")] = min(open_r, 112.0) - 0.5
    frame.iloc[R, frame.columns.get_loc("volume")] = 4_000_000.0
    return frame


def run(frame=None, **kw):
    frame = bars() if frame is None else frame
    return er.earnings_reaction(frame, reaction_date=str(frame.index[R].date()), as_of=stamp(frame, len(frame) - 1), **kw)


def test_ramp_by_hand():
    assert er.ramp(0.05, 0.0, 0.10) == pytest.approx(50.0)
    assert er.ramp(-1.0, 0.0, 0.10) == 0.0 and er.ramp(9.0, 0.0, 0.10) == 100.0
    assert er.ramp(1.15, 0.8, 1.5) == pytest.approx(50.0)


def test_five_measures_and_the_score_by_hand():
    value = run().value
    m = value["measures"]
    assert m["gap"] == pytest.approx(110 / 105 - 1)
    assert m["prior_trend"] == pytest.approx(105 / 100 - 1)
    assert m["volume"] == pytest.approx((19 * 1e6 + 4e6) / 20 / ((59 * 1e6 + 4e6) / 60))             # 20 sessions against 60, reaction included
    assert m["ma200"] == pytest.approx(112 / ((198 * 100 + 105 + 112) / 200) - 1)
    assert m["ma50"] == pytest.approx(112 / ((48 * 100 + 105 + 112) / 50) - 1)
    f = value["factors"]
    assert f["gap"] == pytest.approx(100 * (110 / 105 - 1) / 0.10) and f["prior_trend"] == pytest.approx(50.0)
    assert f["volume"] == pytest.approx(100 * (m["volume"] - 0.8) / 0.7)
    assert f["ma200"] == pytest.approx(100 * m["ma200"] / 0.20) and f["ma50"] == 100.0
    expected = 0.25 * f["gap"] + 0.25 * 50.0 + 0.20 * f["volume"] + 0.15 * f["ma200"] + 0.15 * 100.0
    assert value["score"] == pytest.approx(expected) and 0.0 <= value["score"] <= 100.0
    assert value["feeds"] == "indicators.screens.pead"


def test_every_result_carries_weights_unvalidated():
    res = run()
    assert any(w.startswith("weights_unvalidated") for w in res.warnings) and res.value["weights_validated"] is False
    custom = run(weights={"gap": 1, "prior_trend": 1, "volume": 1, "ma200": 1, "ma50": 1})
    assert any(w.startswith("weights_unvalidated") for w in custom.warnings)
    assert custom.value["score"] == pytest.approx(sum(custom.value["factors"].values()) / 5)
    assert custom.value["weights"]["gap"] == pytest.approx(0.2)
    only_gap = run(weights={"gap": 1, "prior_trend": 0, "volume": 0, "ma200": 0, "ma50": 0})
    assert only_gap.value["score"] == pytest.approx(only_gap.value["factors"]["gap"])


def test_a_flat_reaction_scores_zero_on_the_gap_and_the_trend():
    f = run(bars(pre=100.0, open_r=100.0)).value["factors"]
    assert f["gap"] == 0.0 and f["prior_trend"] == 0.0


def test_only_closed_bars_and_nothing_after_the_reaction_is_read():
    frame = bars()
    open_bar = pd.concat([frame, frame.iloc[[-1]].set_axis([frame.index[-1] + pd.offsets.BDay(1)]).assign(open=100.0, close=300.0, high=301.0, volume=9e9)])
    at = stamp(frame, len(frame) - 1)
    a = er.earnings_reaction(open_bar, reaction_date=str(frame.index[R].date()), as_of=at)
    b = er.earnings_reaction(frame, reaction_date=str(frame.index[R].date()), as_of=at)
    assert a.value == b.value
    later = frame.copy()
    later.iloc[R + 1:, later.columns.get_loc("close")] = 50.0
    later.iloc[R + 1:, later.columns.get_loc("high")] = 51.0
    later.iloc[R + 1:, later.columns.get_loc("open")] = 50.0
    later.iloc[R + 1:, later.columns.get_loc("low")] = 49.0
    assert run(later).value["score"] == pytest.approx(run().value["score"])


def test_missing_history_or_reaction_gives_no_value_never_zero():
    frame = bars()
    short = er.earnings_reaction(frame.iloc[:150], reaction_date=str(frame.index[140].date()), as_of=stamp(frame, 149))
    assert short.value is None and any("not available" in w for w in short.warnings)
    assert er.earnings_reaction(frame, reaction_date="2030-01-01", as_of=stamp(frame, 259)).value is None
    early = er.earnings_reaction(frame, reaction_date=str(frame.index[100].date()), as_of=stamp(frame, 259))
    assert early.value is None and "before the reaction" in early.warnings[0]


def test_nan_empty_and_bad_weights():
    frame = bars()
    nan = frame.copy()
    nan.iloc[100, nan.columns.get_loc("volume")] = np.nan
    assert run(nan).value is None
    assert er.earnings_reaction(frame.iloc[:0], reaction_date="2024-01-02", as_of="2024-02-01").value is None
    assert run(weights={"gap": -1}).value is None
    assert run(weights={"gap": 0, "prior_trend": 0, "volume": 0, "ma200": 0, "ma50": 0}).value is None
    with pytest.raises(BadInput):
        run(weights={"unknown": 1})
    with pytest.raises(BadInput):
        run(weights=[1, 2])
    with pytest.raises(BadInput):
        er.earnings_reaction(frame, reaction_date=5)
    with pytest.raises(BadInput):
        er.earnings_reaction([1, 2], reaction_date="2024-01-02")


def test_on_the_fixed_daily_bars_and_through_the_registry(spy):
    day = str(spy.index[600].date())
    res = registry.call("indicators.screens.earnings_reaction", bars=spy, reaction_date=day, as_of=stamp(spy, len(spy) - 1))
    assert res.piece_ids == ("CTS-002",) and 0.0 <= res.value["score"] <= 100.0
    assert "weights_unvalidated" in " ".join(res.warnings)
