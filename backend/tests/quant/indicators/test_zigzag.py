"""L-57: ZigZag. Hand computed pivots, a turn confirmed only after the pullback, and no repaint when bars are added."""
from __future__ import annotations

import pandas as pd

from hokma.quant import registry
from hokma.quant.indicators import catalog, zigzag

from .helpers import bars_from_closes, stamp

CLOSES = [100.0, 105.0, 110.0, 104.0, 99.0, 95.0, 100.0, 106.0, 112.0, 108.0]
THRESHOLD = 0.1


def run(closes, threshold=THRESHOLD):
    bars = bars_from_closes(closes)
    return bars, zigzag.registered_zigzag(bars, threshold=threshold, as_of=stamp(bars, len(closes) - 1))


def test_pivots_by_hand():
    bars, res = run(CLOSES)
    d = lambda k: str(bars.index[k])  # noqa: E731
    # 100 -> 110 is +10 percent: the low at bar 0 is confirmed on bar 2. 110 -> 99 is -10 percent: the high at bar 2 is confirmed on bar 4.
    # 95 -> 106 is +11.6 percent: the low at bar 5 is confirmed on bar 7. Bar 8 (112) is the running extreme of the up leg: not a pivot yet.
    assert res.value["pivots"] == [
        {"index": d(0), "price": 100.0, "kind": "low", "confirmed_at": d(2), "bars_to_confirm": 2},
        {"index": d(2), "price": 110.0, "kind": "high", "confirmed_at": d(4), "bars_to_confirm": 2},
        {"index": d(5), "price": 95.0, "kind": "low", "confirmed_at": d(7), "bars_to_confirm": 2},
    ]
    assert res.value["leg"] == {"direction": "up", "extreme_index": d(8), "extreme_price": 112.0}
    assert res.value["n_pivots"] == 3 and res.value["bars"] == 10 and res.piece_ids == ("L-57",)


def test_a_turn_is_confirmed_only_when_the_pullback_has_happened():
    _, before = run(CLOSES[:4])                                       # the close of 104 is a 5.5 percent pullback from 110: the high is not confirmed
    assert [p["kind"] for p in before.value["pivots"]] == ["low"]
    assert before.value["leg"]["extreme_price"] == 110.0              # it is only the running extreme
    _, at = run(CLOSES[:5])                                           # 99 is exactly 10 percent below 110
    assert [(p["kind"], p["price"]) for p in at.value["pivots"]] == [("low", 100.0), ("high", 110.0)]
    _, almost = run(CLOSES[:4] + [99.5])                              # 99.5 is 9.5 percent below 110: not yet
    assert [p["kind"] for p in almost.value["pivots"]] == ["low"]


def test_the_result_on_the_first_bars_is_the_start_of_the_result_on_all_bars():
    bars, full = run(CLOSES)
    position = {str(i): n for n, i in enumerate(bars.index)}
    for k in range(3, len(CLOSES) + 1):
        part = zigzag.registered_zigzag(bars.iloc[:k], threshold=THRESHOLD, as_of=stamp(bars, k - 1)).value["pivots"]
        assert part == [p for p in full.value["pivots"] if position[p["confirmed_at"]] < k], k


def test_a_downward_start_and_a_rising_series_with_no_pivot():
    _, down = run([100.0, 95.0, 90.0, 94.0])
    assert [(p["kind"], p["price"], p["bars_to_confirm"]) for p in down.value["pivots"]] == [("high", 100.0, 2)]      # 90 is -10 percent from 100
    assert down.value["leg"]["direction"] == "down" and down.value["leg"]["extreme_price"] == 90.0
    _, drift = run([100.0, 101.0, 102.0, 103.0])
    assert drift.value["pivots"] == [] and drift.value["leg"] is None


def test_the_unconfirmed_leg_follows_the_extreme_and_the_open_bar_is_ignored():
    bars = bars_from_closes(CLOSES + [60.0])                           # an open candle with a crash: the day after the last closed bar
    res = zigzag.registered_zigzag(bars, threshold=THRESHOLD, as_of=stamp(bars, 9))
    assert res.value["bars"] == 10 and res.value["n_pivots"] == 3 and res.params["dropped_open_bars"] == 1
    assert zigzag.registered_zigzag(bars, threshold=THRESHOLD, as_of=stamp(bars, 10)).value["pivots"][-1]["kind"] == "high"   # once it has closed: 112 is confirmed


def test_threshold_domain_and_registration():
    bars = bars_from_closes(CLOSES)
    assert zigzag.registered_zigzag(bars, threshold=1.0, as_of=stamp(bars, 9)).value is None
    assert zigzag.registered_zigzag(bars, threshold=0.0, as_of=stamp(bars, 9)).value is None
    assert catalog.lag_of("indicators.zigzag") == 1.0 and registry.describe("indicators.zigzag").summary.endswith("Lag: 1 bar.")
    res = registry.call("indicators.zigzag", bars=bars, threshold=0.2, as_of=stamp(bars, 9))
    assert [p["kind"] for p in res.value["pivots"]] == []              # no 20 percent move in these closes: none of the legs is confirmed
    assert isinstance(pd.Timestamp(res.as_of), pd.Timestamp)


def test_the_registered_function_runs_in_a_worker_process_and_comes_back_whole(monkeypatch):
    monkeypatch.setattr(registry, "IN_PROCESS", False)                 # the registry runs package functions in a worker that can be killed
    bars = bars_from_closes(CLOSES)
    res = registry.call("indicators.zigzag", bars=bars, threshold=THRESHOLD, as_of=stamp(bars, 9))
    assert res.value["n_pivots"] == 3 and res.piece_ids == ("L-57",)
    doji = registry.call("indicators.candle_doji", bars=bars, as_of=stamp(bars, 9))
    assert doji.ok and doji.piece_ids == ("V-42",)
