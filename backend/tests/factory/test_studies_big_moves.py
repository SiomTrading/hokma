"""Study of big movers (SPEC-020: CTS-018): returns from the next open plus one tick, MFE and MAE with the multiplier, cohorts and the survivorship flag."""
import json
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from hokma.factory.studies import _h3, big_moves as bm
from hokma.quant.errors import BadInput

FIXTURE = Path(__file__).resolve().parents[1] / "backtest" / "fixtures" / "nq_session.json"
START = date(2025, 3, 3)


def bars(rows):
    """rows are (open, high, low, close) in text; the days are consecutive."""
    return [bm.DailyBar(START + timedelta(days=i), D(o), D(h), D(l), D(c)) for i, (o, h, l, c) in enumerate(rows)]


UP = [("100", "101", "99", "100"), ("100", "125", "100", "125"),                      # day 1 closes 25 percent above day 0
      ("126", "130", "124", "128"), ("128", "133", "127", "132"), ("132", "134", "120", "121"),
      ("122", "123", "118", "119"), ("119", "121", "117", "118")]
SHARE = bm.Instrument("XYZ", D("0.01"))


def test_the_event_and_its_returns_start_at_the_next_open_plus_one_tick_by_hand():
    series = bars(UP)
    assert bm.find_events(series) == [(1, "up", D("0.25"))]
    entry, outcomes = bm.measure(series, 1, "up", SHARE, (1, 3, 5, 10))
    assert entry == D("126.01")                                   # open of day 2 plus one tick, not the close of the event day (125)
    h1, h3, h5, h10 = outcomes
    assert h1.return_pct == D("1.99") / D("126.01") and h1.mfe_usd == D("3.99") and h1.mae_usd == D("-2.01") and h1.continued is True
    # day 4 closes 121: (121 - 126.01) / 126.01. Window days 2 to 4: highest high 134, lowest low 120.
    assert h3.return_pct == D("-5.01") / D("126.01") and h3.mfe_usd == D("7.99") and h3.mae_usd == D("-6.01") and h3.continued is False
    # day 6 closes 118. Window days 2 to 6: highest high 134, lowest low 117.
    assert h5.return_pct == D("-8.01") / D("126.01") and h5.mfe_usd == D("7.99") and h5.mae_usd == D("-9.01")
    assert h10 == bm.Outcome(10, None, None, None, None)           # the window is not complete: nothing is filled in


def test_a_down_move_is_followed_short_with_the_tick_against():
    down = bars([("200", "201", "199", "200"), ("200", "200", "150", "150"), ("149", "152", "145", "146"), ("146", "148", "140", "141")])
    (index, direction, move), = bm.find_events(down)
    assert (index, direction, move) == (1, "down", D("-0.25"))
    entry, (h1, h2, h3) = bm.measure(down, 1, direction, bm.Instrument("DWN", D("0.01")), (1, 2, 3))
    assert entry == D("148.99")                                    # a short sells one tick below the open
    assert h1.return_pct == D("2.99") / D("148.99") and h1.mfe_usd == D("3.99") and h1.mae_usd == D("-3.01")
    assert h2.return_pct == D("7.99") / D("148.99") and h3.return_pct is None
    assert bm.measure(down, 3, "down", bm.Instrument("DWN", D("0.01")), (1,)) == (None, (bm.Outcome(1, None, None, None, None),))


def test_mfe_and_mae_carry_the_multiplier_of_the_contract():
    nq = bm.Instrument("NQ", D("0.25"), D("20"))
    series = bars([("20000", "20010", "19990", "20000"), ("20000", "25000", "20000", "25000"), ("25000", "25100", "24900", "25050")])
    entry, (h1,) = bm.measure(series, 1, "up", nq, (1,))
    assert entry == D("25000.25")
    assert h1.mfe_usd == D("99.75") * 20 and h1.mae_usd == D("-100.25") * 20      # (25100 - 25000.25) * 20 and (24900 - 25000.25) * 20


def test_mfe_is_never_below_zero_and_mae_never_above_zero():
    series = bars([("100", "101", "99", "100"), ("100", "130", "100", "130"), ("131", "131", "131", "131"), ("131", "131", "131", "131")])
    _, (h1, h2) = bm.measure(series, 1, "up", SHARE, (1, 2))
    # Entry 131.01; the best high of the window is 131, so the raw best gain is -0.01 and is floored at zero; the worst low gives -0.01.
    assert h1.mfe_usd == 0 and h2.mfe_usd == 0 and h1.mae_usd == D("-0.01") and h2.mae_usd == D("-0.01")


def test_the_cooldown_keeps_two_events_from_overlapping():
    series = bars([("100", "100", "100", "100"), ("100", "125", "100", "125"), ("125", "160", "125", "160"), ("160", "161", "159", "160")])
    assert [i for i, _, _ in bm.find_events(series, cooldown=0)] == [1, 2]
    assert [i for i, _, _ in bm.find_events(series, cooldown=20)] == [1]


def study(includes_delisted, **kw):
    second = list(UP)
    second[2] = ("126", "130", "124", "124")                         # the same story, but the next day closes below the entry
    universe = {"XYZ": (SHARE, bars(UP)), "XY2": (bm.Instrument("XY2", D("0.01")), bars(second))}
    notes = {("XYZ", START + timedelta(days=1)): ("earnings", "gap"), ("XY2", START + timedelta(days=1)): ("earnings", "gap")}
    return bm.run_study(universe, includes_delisted=includes_delisted, min_sample=2, annotations=notes, horizons=(1, 3), **kw)


def test_a_cohort_with_enough_events_has_statistics_by_hand():
    result = study(True)
    assert [e.symbol for e in result.events] == ["XY2", "XYZ"] and result.flags == ()
    (cohort,) = result.cohorts
    assert cohort.key == ("up", "earnings", "gap") and cohort.n_events == 2 and not cohort.insufficient_sample
    h1 = cohort.horizons[1]
    # day 2 closes 128 for XYZ and 124 for XY2, against the same entry 126.01: returns 1.99 / 126.01 and -2.01 / 126.01.
    assert h1["n"] == 2 and h1["mean_return_pct"] == (D("1.99") / D("126.01") + D("-2.01") / D("126.01")) / 2
    assert h1["median_return_pct"] == h1["mean_return_pct"] and h1["continuation_rate"] == D("0.5")
    assert h1["mean_mfe_usd"] == D("3.99") and h1["mean_mae_usd"] == D("-2.01")      # both windows are day 2: high 130, low 124, entry 126.01
    assert h1["significance"] is None


def test_a_cohort_below_the_minimum_sample_gives_no_statistics():
    result = bm.run_study({"XYZ": (SHARE, bars(UP))}, includes_delisted=True, min_sample=2, horizons=(1,))
    (cohort,) = result.cohorts
    assert cohort.insufficient_sample and cohort.n_events == 1 and cohort.key == ("up", None, None)
    assert cohort.horizons[1]["mean_return_pct"] is None and cohort.horizons[1]["n"] == 1


def test_a_universe_without_delisted_names_is_flagged():
    assert study(False).flags == (bm.SURVIVORSHIP_FLAG,) == ("CURRENT_UNIVERSE_BACKFILL_SURVIVORSHIP_BIAS",)
    with pytest.raises(BadInput):
        bm.run_study({"XYZ": (SHARE, bars(UP))}, includes_delisted=None, min_sample=2)       # the caller must say what the universe is


def test_the_significance_comes_from_the_injected_h3_function():
    seen = []

    def fake(values, seed):
        seen.append((values, seed))
        return {"t": 1.5, "ci_low": -0.2, "ci_high": 0.4}

    result = study(True, event_stats=fake, significance=True, seed=7)
    h1 = result.cohorts[0].horizons[1]
    assert h1["significance"] == {"t": 1.5, "ci_low": -0.2, "ci_high": 0.4}
    assert seen[0][1] == 7 and sorted(seen[0][0]) == pytest.approx(sorted([1.99 / 126.01, -2.01 / 126.01]))


def test_without_h3_the_significance_request_fails_with_a_stable_error(monkeypatch):
    monkeypatch.setattr(_h3, "ALLOWED_PREFIX", "nowhere.")
    with pytest.raises(_h3.H3Unavailable, match="hokma.quant.stats.events is not a quant module"):
        study(True, significance=True)


def test_on_real_nq_daily_bars_every_event_matches_its_close_ratio():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))["daily"]["bars"]
    series = [bm.DailyBar(date.fromisoformat(b["ts"][:10]), D(b["open"]), D(b["high"]), D(b["low"]), D(b["close"])) for b in raw]
    events = bm.find_events(series, threshold=D("0.012"), window=1, cooldown=0)
    assert events
    for index, direction, move in events:
        assert move == series[index].close / series[index - 1].close - 1 and abs(move) >= D("0.012")
        assert direction == ("up" if move > 0 else "down")
    expected = [i for i in range(1, len(series)) if abs(series[i].close / series[i - 1].close - 1) >= D("0.012")]
    assert [i for i, _, _ in events] == expected
    nq = bm.Instrument("NQ", D("0.25"), D("20"))
    entry, (h1, *_rest) = bm.measure(series, events[0][0], events[0][1], nq, bm.HORIZONS)
    index = events[0][0]
    assert entry == series[index + 1].open + (D("0.25") if events[0][1] == "up" else D("-0.25"))
    assert h1.mae_usd <= 0 <= h1.mfe_usd


def test_bad_inputs_raise_bad_input():
    with pytest.raises(BadInput):
        bm.DailyBar(START, D("100"), D("99"), D("98"), D("100"))                     # the high is below the open
    with pytest.raises(BadInput):
        bm.run_study({"X": (SHARE, list(reversed(bars(UP))))}, includes_delisted=True, min_sample=2)
    with pytest.raises(BadInput):
        bm.find_events(bars(UP), threshold=D("-1"))
    with pytest.raises(BadInput):
        bm.summarize([], min_sample=1)
    with pytest.raises(BadInput):
        bm.summarize([], min_sample=2, group_by=("sector",))
