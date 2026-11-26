"""Continuous futures: roll rules, adjustment modes, chain validation and sizing (SPEC-020: L-22, L-23, N-33, P-06).
The bars are small series written by hand; the expected values are computed by hand next to each case."""
from datetime import date
from decimal import Decimal as D

import pandas as pd
import pytest

from hokma.backtest import continuous
from hokma.backtest.continuous import Adjustment, ChainError, ContractBars, RollRule
from hokma.quant.errors import BadInput

SEP, DEC, MAR = "FUT:NQ:2025-09", "FUT:NQ:2025-12", "FUT:NQ:2026-03"


def daily(closes: dict[str, float], volume: float = 1000.0) -> pd.DataFrame:
    """Daily bars labelled at 00:00 UTC; open, high and low around the close."""
    index = pd.DatetimeIndex([pd.Timestamp(day, tz="UTC") for day in closes])
    close = pd.Series(list(closes.values()), index=index, dtype=float)
    return pd.DataFrame({"open": close - 5, "high": close + 10, "low": close - 10, "close": close, "volume": volume})


# NQ September 2025 expires on Friday 2025-09-19; 8 days before is Thursday 2025-09-11, the day December takes over.
A = daily({"2025-09-08": 24000, "2025-09-09": 24100, "2025-09-10": 24200})
B = daily({"2025-09-08": 24050, "2025-09-09": 24160, "2025-09-10": 24260, "2025-09-11": 24300, "2025-09-12": 24350, "2025-09-15": 24400})


def chain():
    return [ContractBars(SEP, A), ContractBars(DEC, B)]


def test_the_expiry_of_a_contract_comes_from_the_rules():
    assert ContractBars(SEP, A).expiry_day == date(2025, 9, 19)
    assert ContractBars(DEC, B).expiry_day == date(2025, 12, 19)


def test_eight_days_before_expiry_nq_december_rolls_to_march_on_the_written_date():
    dec = daily({"2025-12-08": 26000, "2025-12-09": 26100, "2025-12-10": 26200})
    mar = daily({"2025-12-08": 26040, "2025-12-09": 26140, "2025-12-10": 26240, "2025-12-11": 26300, "2025-12-12": 26350})
    series = continuous.build([ContractBars(MAR, mar), ContractBars(DEC, dec)], RollRule.days_before(8), Adjustment.RAW)
    (roll,) = series.rolls
    assert (roll.from_contract, roll.to_contract, roll.day) == (DEC, MAR, date(2025, 12, 11))      # 19 December minus 8 days
    assert list(series.frame["contract"]) == [DEC] * 3 + [MAR] * 2
    assert series.frame.index[-1] == pd.Timestamp("2025-12-12", tz="UTC")


def test_difference_adjustment_removes_the_jump_of_the_roll_and_the_raw_series_keeps_it():
    adjusted = continuous.build(chain(), RollRule.days_before(8), Adjustment.DIFFERENCE)
    raw = continuous.build(chain(), RollRule.days_before(8), Adjustment.RAW)
    (roll,) = adjusted.rolls
    assert (roll.old_price, roll.new_price, roll.gap) == (24200.0, 24260.0, 60.0)                  # both contracts on 2025-09-10
    assert list(adjusted.frame["close"]) == [24060.0, 24160.0, 24260.0, 24300.0, 24350.0, 24400.0]
    assert list(raw.frame["close"]) == [24000.0, 24100.0, 24200.0, 24300.0, 24350.0, 24400.0]
    assert adjusted.frame["close"].iloc[3] - adjusted.frame["close"].iloc[2] == 40.0               # the move of December itself: 24300 - 24260
    assert raw.frame["close"].iloc[3] - raw.frame["close"].iloc[2] == 100.0                        # the move plus the 60 point gap
    assert list(adjusted.frame["raw_close"]) == list(raw.frame["close"])
    assert adjusted.frame["high"].iloc[0] == 24000.0 + 10 + 60 and adjusted.frame["low"].iloc[2] == 24200.0 - 10 + 60


def test_ratio_adjustment_scales_the_older_contract():
    series = continuous.build(chain(), RollRule.days_before(8), Adjustment.RATIO)
    factor = 24260 / 24200
    assert list(series.frame["close"][:3]) == pytest.approx([24000 * factor, 24100 * factor, 24260.0])
    assert list(series.frame["close"][3:]) == [24300.0, 24350.0, 24400.0]
    assert series.rolls[0].ratio == pytest.approx(factor)


def test_three_contracts_accumulate_their_gaps_from_the_last_one():
    mar = daily({"2025-12-08": 26500, "2025-12-09": 26600, "2025-12-10": 26700, "2025-12-11": 26800})
    dec = daily({"2025-09-11": 24300, "2025-09-12": 24350, "2025-09-15": 24400, **{d.strftime("%Y-%m-%d"): 24400.0 for d in pd.bdate_range("2025-09-16", "2025-12-10")},
                 "2025-12-08": 26000, "2025-12-09": 26100, "2025-12-10": 26200})
    sep = daily({"2025-09-10": 24200})
    sep_overlap = daily({"2025-09-10": 24260})
    dec = pd.concat([sep_overlap, dec])
    series = continuous.build([ContractBars(SEP, sep), ContractBars(DEC, dec), ContractBars(MAR, mar)], RollRule.days_before(8), Adjustment.DIFFERENCE)
    first, second = series.rolls
    assert (first.gap, second.gap) == (60.0, 26700.0 - 26200.0)
    assert series.frame["close"].iloc[0] == 24200.0 + 60.0 + 500.0                                  # the oldest bar carries both gaps
    assert series.frame["close"].iloc[-1] == 26800.0                                                # the newest bar is the real price


def test_a_volume_rule_rolls_the_day_the_next_contract_trades_more():
    a = daily({"2025-09-08": 24000, "2025-09-09": 24100, "2025-09-10": 24200, "2025-09-11": 24250})
    b = B.copy()
    a["volume"], b["volume"] = [1000.0, 900.0, 700.0, 500.0], [200.0, 400.0, 800.0, 900.0, 950.0, 990.0]
    series = continuous.build([ContractBars(SEP, a), ContractBars(DEC, b)], RollRule.by_volume(), Adjustment.RAW)
    assert series.rolls[0].day == date(2025, 9, 10)                                                 # 800 beats 700 on the 10th
    assert list(series.frame["contract"])[:3] == [SEP, SEP, DEC] and series.rolls[0].reference_at == pd.Timestamp("2025-09-09", tz="UTC")


def test_an_open_interest_rule_needs_the_open_interest():
    interest = {SEP: {date(2025, 9, 8): 500, date(2025, 9, 9): 400, date(2025, 9, 10): 300}, DEC: {date(2025, 9, 8): 100, date(2025, 9, 9): 350, date(2025, 9, 10): 600}}
    series = continuous.build([ContractBars(SEP, A, interest[SEP]), ContractBars(DEC, B, interest[DEC])], RollRule.by_open_interest(), Adjustment.RAW)
    assert series.rolls[0].day == date(2025, 9, 10)                                                 # 350 < 400 on the 9th; 600 > 300 on the 10th
    with pytest.raises(ChainError) as error:
        continuous.build(chain(), RollRule.by_open_interest())
    assert error.value.code == "open_interest_missing"


def test_a_chain_with_a_missing_contract_fails_with_the_date_of_the_hole():
    c = daily({"2026-03-02": 26000, "2026-03-03": 26100})
    with pytest.raises(ChainError) as error:
        continuous.build([ContractBars(SEP, A), ContractBars("FUT:NQ:2026-03", c)], RollRule.days_before(8))
    assert error.value.code == "missing_contract" and error.value.day == date(2025, 9, 11) and "2025-12" in str(error.value)


def test_the_real_gap_of_2025_10_17_in_the_december_contract_fails_with_its_date():
    """The shape of the case of the data check of 2025-10-17: December has no bars on a business day it is the active contract."""
    days = [d for d in pd.bdate_range("2025-09-08", "2025-12-12") if d != pd.Timestamp("2025-10-17")]
    dec = daily({d.strftime("%Y-%m-%d"): 24000.0 + i for i, d in enumerate(days)})
    mar = daily({d.strftime("%Y-%m-%d"): 24100.0 + i for i, d in enumerate(pd.bdate_range("2025-12-01", "2025-12-20"))})
    with pytest.raises(ChainError) as error:
        continuous.build([ContractBars(SEP, A), ContractBars(DEC, dec), ContractBars(MAR, mar)], RollRule.days_before(8))
    assert error.value.code == "data_gap" and error.value.day == date(2025, 10, 17) and "2025-10-17" in str(error.value)


def test_a_roll_with_no_price_of_the_new_contract_that_day_fails_with_that_day():
    b = B.drop(pd.Timestamp("2025-09-10", tz="UTC"))
    with pytest.raises(ChainError) as error:
        continuous.build([ContractBars(SEP, A), ContractBars(DEC, b)], RollRule.days_before(8))
    assert error.value.code == "no_overlap" and error.value.day == date(2025, 9, 10)


def test_a_segment_without_bars_and_other_bad_chains_are_refused():
    early = daily({"2025-09-01": 23900})
    with pytest.raises(ChainError) as error:
        continuous.build([ContractBars(SEP, early), ContractBars(DEC, B)], RollRule.days_before(8))
    assert (error.value.code, error.value.day) == ("data_gap", date(2025, 9, 2))                    # 1 September is a holiday: the first business day with no bars
    for bad in ([], [ContractBars(SEP, A), ContractBars(SEP, A)], [ContractBars(SEP, A), ContractBars("FUT:ES:2025-12", B)]):
        with pytest.raises(ChainError) as error:
            continuous.validate_chain(bad, RollRule.days_before(8))
        assert error.value.code == "bad_chain"
    with pytest.raises(ChainError):
        continuous.build([ContractBars(SEP, A.reset_index(drop=True)), ContractBars(DEC, B)], RollRule.days_before(8))
    with pytest.raises(ChainError):
        RollRule("sometimes")
    with pytest.raises(ChainError):
        RollRule.days_before(-1)


def test_the_size_uses_the_real_price_of_the_contract_and_not_the_adjusted_one():
    old = daily({"2025-09-08": 100.0, "2025-09-09": 101.0, "2025-09-10": 102.0})
    new = daily({"2025-09-08": 200.0, "2025-09-09": 201.0, "2025-09-10": 202.0, "2025-09-11": 203.0, "2025-09-12": 204.0, "2025-09-15": 205.0})
    series = continuous.panama([ContractBars(SEP, old), ContractBars(DEC, new)], RollRule.days_before(8))
    at = pd.Timestamp("2025-09-10", tz="UTC")
    assert series.frame["close"].loc[at] == 202.0 and continuous.real_price(series, at) == 102.0       # gap of 100 points
    assert continuous.contracts_for_budget(series, at, budget=D(10200), multiplier=D(1)) == 100        # 10200 / 102, not 10200 / 202 = 50
    assert continuous.contracts_for_budget(series, at, budget=D(20400), multiplier=D(2)) == 100
    assert continuous.panama([ContractBars(SEP, old), ContractBars(DEC, new)], RollRule.days_before(8)).adjustment is Adjustment.DIFFERENCE


def test_the_chain_is_limited_by_the_cells_of_the_quant_limits(monkeypatch):
    monkeypatch.setattr(continuous.limits, "MAX_INPUT_CELLS", 10)
    with pytest.raises(BadInput):
        continuous.build(chain(), RollRule.days_before(8))
