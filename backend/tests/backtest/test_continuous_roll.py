"""Roll regressions (SPEC-020: L-24): nine cases, three bar sizes (minute, hour, day) by three positions around the roll (the move before
the roll, across it and after it). In every case the adjusted price does not jump at the roll and the raw price does.

The old contract (NQ 2025-09) ends on 2025-09-10 and the rule 8 days before expiry hands over to NQ 2025-12 on 2025-09-11, which starts at
00:00 New York time (04:00 UTC in September). Hand computed values: the old closes are 100, 101, 102; at the last old bar the new contract
closes 107, so the gap is 5 and the old bars adjust to 105, 106, 107; the new contract then closes 109, 110, 112."""
import pandas as pd
import pytest

from hokma.backtest import continuous
from hokma.backtest.continuous import Adjustment, ContractBars, RollRule

OLD, NEW = "FUT:NQ:2025-09", "FUT:NQ:2025-12"


def frame(stamps, closes):
    close = pd.Series(closes, index=pd.DatetimeIndex(stamps, tz="UTC"), dtype=float)
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 100.0})


RESOLUTIONS = {
    "minute": (["2025-09-11 03:57", "2025-09-11 03:58", "2025-09-11 03:59"], ["2025-09-11 04:00", "2025-09-11 04:01", "2025-09-11 04:02"]),
    "hour": (["2025-09-11 01:00", "2025-09-11 02:00", "2025-09-11 03:00"], ["2025-09-11 04:00", "2025-09-11 05:00", "2025-09-11 06:00"]),
    "day": (["2025-09-08", "2025-09-09", "2025-09-10"], ["2025-09-11", "2025-09-12", "2025-09-15"]),
}


def series_for(resolution, adjustment):
    before, after = RESOLUTIONS[resolution]
    old = frame(before, [100.0, 101.0, 102.0])
    new = frame(before + after, [105.0, 106.0, 107.0, 109.0, 110.0, 112.0])
    return continuous.build([ContractBars(OLD, old), ContractBars(NEW, new)], RollRule.days_before(8), adjustment)


# position: (index of the earlier bar, adjusted move, raw move)
POSITIONS = {"before the roll": (0, 1.0, 1.0), "across the roll": (2, 2.0, 7.0), "after the roll": (3, 1.0, 1.0)}


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("resolution", RESOLUTIONS)
def test_the_adjusted_price_does_not_jump_at_the_roll_and_the_raw_price_does(resolution, position):
    first, adjusted_move, raw_move = POSITIONS[position]
    adjusted, raw = series_for(resolution, Adjustment.DIFFERENCE), series_for(resolution, Adjustment.RAW)
    assert list(adjusted.frame["close"]) == [105.0, 106.0, 107.0, 109.0, 110.0, 112.0]
    assert list(raw.frame["close"]) == [100.0, 101.0, 102.0, 109.0, 110.0, 112.0]
    a, r = adjusted.frame["close"], raw.frame["close"]
    assert a.iloc[first + 1] - a.iloc[first] == adjusted_move
    assert r.iloc[first + 1] - r.iloc[first] == raw_move
    assert (adjusted.rolls[0].gap, adjusted.rolls[0].old_price, adjusted.rolls[0].new_price) == (5.0, 102.0, 107.0)
    assert list(adjusted.frame["contract"]) == [OLD] * 3 + [NEW] * 3
