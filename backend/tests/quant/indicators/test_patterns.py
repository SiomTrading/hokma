"""V-42: candlestick patterns (doji, hammer, engulfing) on hand written candles, closed candles only, each registered with a lag of one bar."""
from __future__ import annotations

import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.indicators import catalog, patterns

from .helpers import stamp

NAMES = {"doji": "indicators.candle_doji", "hammer": "indicators.candle_hammer", "engulfing": "indicators.candle_engulfing"}

# open, high, low, close of six daily candles written by hand. The sixth is the candle that is still open when it is not yet 16:00.
#  1 down candle                       2 bullish engulfing of 1                   3 doji (body 0.25 of a range of 4)
#  4 hammer (long lower shadow)        5 bearish engulfing of 4                   6 bullish engulfing of 5
CANDLES = [(20.0, 21.0, 17.0, 18.0), (17.5, 23.0, 17.0, 22.0), (22.0, 24.0, 20.0, 22.25), (21.0, 21.5, 19.0, 21.5), (22.0, 22.5, 20.0, 20.5),
           (20.25, 23.0, 20.0, 22.5)]


def candles() -> pd.DataFrame:
    return pd.DataFrame(CANDLES, columns=["open", "high", "low", "close"], index=pd.bdate_range("2024-01-02", periods=len(CANDLES)))


def column(result, name: str) -> list:
    assert result.ok, result.warnings
    return result.value["values"][name]


def test_doji_hammer_and_engulfing_on_hand_written_candles():
    bars = candles()
    as_of = stamp(bars, 5)
    assert column(patterns.candle_doji(bars, as_of=as_of), "candle_doji") == [0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    assert column(patterns.candle_hammer(bars, as_of=as_of), "candle_hammer") == [0.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    assert column(patterns.candle_engulfing(bars, as_of=as_of), "candle_engulfing") == [None, 1.0, 0.0, 0.0, -1.0, 1.0]


def test_the_thresholds_by_hand():
    bars = candles()
    as_of = stamp(bars, 5)
    # candle 4: body 0.5, range 2.5, lower shadow 2.0, upper shadow 0: a hammer while 2.0 >= 0.5 * wick_multiple, i.e. up to a multiple of 4
    assert column(patterns.candle_hammer(bars, as_of=as_of, wick_multiple=4.0), "candle_hammer")[3] == 1.0
    assert column(patterns.candle_hammer(bars, as_of=as_of, wick_multiple=4.5), "candle_hammer")[3] == 0.0
    # candle 4 has a body of 0.2 of its range: a doji only when body_ratio is at least 0.2; candle 3 stops being one under 0.0625
    assert column(patterns.candle_doji(bars, as_of=as_of, body_ratio=0.2), "candle_doji")[3] == 1.0
    assert column(patterns.candle_doji(bars, as_of=as_of, body_ratio=0.0625), "candle_doji")[2] == 1.0
    assert column(patterns.candle_doji(bars, as_of=as_of, body_ratio=0.06), "candle_doji")[2] == 0.0
    # candle 3 has an upper shadow of 1.75 of a range of 4 (0.4375): a hammer shape only when upper_ratio allows it, and then its body is not empty
    assert column(patterns.candle_hammer(bars, as_of=as_of, upper_ratio=0.45), "candle_hammer")[2] == 1.0


def test_an_engulfing_needs_a_larger_body_that_covers_the_previous_body():
    bars = pd.DataFrame([(20.0, 21.0, 17.0, 18.0), (18.5, 21.5, 18.0, 21.0), (21.0, 21.5, 19.0, 19.5), (19.0, 22.0, 19.0, 22.0)],
                        columns=["open", "high", "low", "close"], index=pd.bdate_range("2024-01-02", periods=4))
    as_of = stamp(bars, 3)
    # candle 2 opens at 18.5, above the previous close 18.0: it does not cover the previous body, so it is not an engulfing
    # candle 4 is a bullish candle after a down candle and covers 21.0 to 19.5 with a larger body: engulfing
    assert column(patterns.candle_engulfing(bars, as_of=as_of), "candle_engulfing") == [None, 0.0, 0.0, 1.0]


def test_a_pattern_is_never_read_on_the_open_candle():
    bars = candles()
    last_day = bars.index[5]
    closed = patterns.candle_engulfing(bars, as_of=stamp(bars, 4))                         # candle 6 is dated after this close: it is still to come
    assert column(closed, "candle_engulfing") == [None, 1.0, 0.0, 0.0, -1.0] and closed.value["latest"]["candle_engulfing"] == -1.0
    assert closed.params["dropped_open_bars"] == 1
    during = patterns.candle_engulfing(bars, as_of=f"{last_day.date()}T15:59")             # the day of candle 6, a minute before the close
    assert during.value["latest"]["candle_engulfing"] == -1.0 and during.params["dropped_open_bars"] == 1
    after = patterns.candle_engulfing(bars, as_of=f"{last_day.date()}T16:00")
    assert after.value["latest"]["candle_engulfing"] == 1.0 and after.params["dropped_open_bars"] == 0
    flagged = patterns.candle_engulfing(bars.assign(closed=[True] * 5 + [False]), as_of=stamp(bars, 5))        # the data layer says the last candle is open
    assert flagged.value["latest"]["candle_engulfing"] == -1.0 and flagged.params["dropped_open_bars"] == 1
    assert any("as_of not given" in w for w in patterns.candle_doji(bars).warnings)


def test_the_result_never_changes_when_a_later_candle_is_added():
    bars = candles()
    full = column(patterns.candle_engulfing(bars, as_of=stamp(bars, 5)), "candle_engulfing")
    for k in range(2, 6):
        part = column(patterns.candle_engulfing(bars.iloc[:k], as_of=stamp(bars, k - 1)), "candle_engulfing")
        assert part == full[:k]


def test_each_pattern_is_registered_with_a_lag_of_one_bar_V_42():
    registered = {s.name: s for s in registry.list("indicators")}
    for full in NAMES.values():
        assert full in registered, full
        assert registered[full].piece_ids == ("V-42",) and catalog.lag_of(full) == 1.0
        assert registered[full].summary.endswith("Lag: 1 bar.")
        assert registered[full].inputs["as_of"] == {"type": "str", "required": False}
    res = registry.call(NAMES["hammer"], bars=candles(), as_of=stamp(candles(), 5))
    assert res.piece_ids == ("V-42",) and res.value["latest"]["candle_hammer"] == 0.0 and res.as_of is not None


def test_bad_candles_give_no_value_and_a_wrong_input_is_refused():
    bad = candles().assign(high=1.0)                                                         # high below the open and the close
    assert patterns.candle_doji(bad, as_of=stamp(bad, 5)).value is None
    assert patterns.candle_doji(candles().iloc[:1], as_of=stamp(candles(), 0)).value is None
    with pytest.raises(BadInput):
        patterns.candle_doji(candles()[["open", "close"]])
    with pytest.raises(BadInput):
        patterns.candle_doji(candles().reset_index(drop=True))                               # a daily index is required
    with pytest.raises(BadInput):
        patterns.candle_hammer(candles(), wick_multiple="2")
    assert patterns.candle_hammer(candles(), as_of=stamp(candles(), 5), wick_multiple=-1.0).value is None
