"""The backtest of one structure from one day (SPEC-020, section 4.9, step 3): prices at the bad side of the quote, the commission of the fee table,
the margin and the early assignment of the options module, and the exits. Every expected value is worked out by hand in the comments.

The chains here are made by hand, day by day, for one credit put spread: spot 100, the short put at 95 and the long put at 90, expiring 30 days
after the first day. The rules are the real ones of `hokma.quant.options` (margin, assignment)."""
import dataclasses
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest
from backtest.calibration_support import bars_frame, quote

from hokma.backtest.calibration import gate as gate_mod
from hokma.backtest.calibration import premium
from hokma.backtest.calibration.premium import ChainDay, ChainHistory

EXPIRY = date(2024, 2, 1)
CAPITAL = Decimal(100_000)
SPREAD = "credit_put_spread"


def gate_of(**changes):
    return dataclasses.replace(gate_mod.load_default(), structures=(SPREAD,), margin_use_pct=50.0, **changes)


def day(d, spot, p95, p90):
    """A day of the hand made chain: the put at 95 and the put at 90, each as (bid, ask), or None when the quote is missing."""
    quotes = [quote(EXPIRY, "put", strike, *prices) for strike, prices in ((95, p95), (90, p90)) if prices is not None]
    return ChainDay(d, Decimal(spot), tuple(quotes))


def history(days):
    closes = [float(d.spot) for d in days]
    return ChainHistory("SPY", tuple(days), bars_frame([d.day for d in days], closes))


def sim(days, gate=None, capital=CAPITAL):
    return premium.Simulation(history(days), gate or gate_of(), premium.default_rules(), capital)


D = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8), date(2024, 1, 9)]
ENTRY = ((2.00, 2.20), (0.50, 0.70))


def profit_take_days():
    return [day(D[0], 100, *ENTRY), day(D[1], 100, *ENTRY), day(D[2], 100, (0.95, 1.05), (0.15, 0.25)), day(D[3], 100, (0.40, 0.50), (0.05, 0.15)),
            day(D[4], 100, (0.35, 0.45), (0.04, 0.12)), day(D[5], 100, *ENTRY)]


def test_the_entry_sells_at_the_bid_buys_at_the_ask_and_sizes_by_the_margin_of_the_options_module():
    """Fill on day 1: sell the 95 at the bid 2.00 and buy the 90 at the ask 0.70, a credit of 1.30 a share.
    Margin of one set from the module: the width 5 x 100 = 500 minus the credit 130 = 370. Contracts: 100,000 x 50 percent / 370 = 135.13, so 135.
    Commission of the table ibkr_options (0.65 a contract, 1.00 at least an order): 0.65 x 135 = 87.75 a leg, 175.50 in all.
    Cash in: 1.30 x 100 x 135 = 17,550.00 minus 175.50 = 17,374.50."""
    trade = sim(profit_take_days()).candidate(SPREAD, 0)
    assert (trade.contracts, trade.margin_usd, trade.fill_idx, trade.signal_idx) == (135, 370.0 * 135, 1, 0)
    # marked at the mid on the fill day: 95 at 2.10 and 90 at 0.60, 1.50 to close: 17,374.50 - 1.50 x 100 x 135 = -2,875.50 (the half spreads and the commission)
    assert trade.path[0] == pytest.approx(-2875.50)


def test_a_half_the_credit_kept_closes_the_next_day_at_the_bad_side_with_both_commissions():
    """Day 2 marks 1.00 and 0.20: 0.80 to close, 0.50 kept, below the 0.65 (half of 1.30). Day 3 marks 0.45 and 0.10: 0.35 to close, 0.95 kept: the exit
    is decided. Day 4 carries it out at the bad side: buy the 95 back at the ask 0.45 and sell the 90 at the bid 0.04, 0.41 x 100 x 135 = 5,535.00,
    plus 175.50 of commission: 17,374.50 - 5,535.00 - 175.50 = 11,664.00."""
    trade = sim(profit_take_days()).candidate(SPREAD, 0)
    assert (trade.exit_reason, trade.finished, trade.exit_idx) == ("profit_take", True, 4)
    assert trade.path == pytest.approx((-2875.50, 6574.50, 12649.50, 11664.00))      # 17,374.50 - 0.80 x 13,500, then - 0.35 x 13,500, then the exit
    assert trade.net_pnl == pytest.approx(11664.00) and trade.net_pnl / trade.margin_usd == pytest.approx(11664.00 / 49950.0)


def test_a_loss_of_twice_the_credit_stops_the_position_the_next_day():
    """Day 2 marks 5.50 and 1.60: 3.90 to close, a loss of 3.90 - 1.30 = 2.60 = 2 x 1.30: the stop is decided. Day 3 buys the 95 back at the ask 6.00 and sells
    the 90 at the bid 1.80: 4.20 x 13,500 = 56,700.00 + 175.50: 17,374.50 - 56,875.50 = -39,501.00."""
    days = [day(D[0], 100, *ENTRY), day(D[1], 100, *ENTRY), day(D[2], 100, (5.40, 5.60), (1.50, 1.70)), day(D[3], 100, (5.80, 6.00), (1.80, 2.00)), day(D[4], 100, *ENTRY)]
    trade = sim(days).candidate(SPREAD, 0)
    assert (trade.exit_reason, trade.exit_idx) == ("stop_loss", 3)
    assert trade.net_pnl == pytest.approx(-39501.00)


def test_the_days_to_expiry_close_the_position_with_no_other_reason():
    """Constant quotes: the 95 at 1.40 and 1.60, the 90 at 0.30 and 0.50. Fill: 1.40 - 0.50 = 0.90 a share. Margin 500 - 90 = 410, contracts 50,000 / 410 = 121.
    Cash in 0.90 x 12,100 = 10,890.00 - 0.65 x 121 x 2 (157.30) = 10,732.70. On 18 January the days to expiry are 14, the rule of the gate: it is decided, and the next
    day (13 days) buys the 95 at 1.60 and sells the 90 at 0.30: 1.30 x 12,100 = 15,730.00 + 157.30. Net 10,732.70 - 15,887.30 = -5,154.60."""
    flat = ((1.40, 1.60), (0.30, 0.50))
    dates, cursor = [], date(2024, 1, 2)
    while cursor <= date(2024, 1, 19):
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor += timedelta(days=1)
    assert len(dates) == 14 and dates[12] == date(2024, 1, 18)
    trade = sim([day(d, 100, *flat) for d in dates]).candidate(SPREAD, 0)
    assert (trade.contracts, trade.exit_reason, trade.exit_idx, len(trade.path)) == (121, "close_at_dte", 13, 13)
    assert trade.net_pnl == pytest.approx(-5154.60)


def test_a_short_leg_with_no_extrinsic_value_is_assigned_at_the_next_day_at_its_intrinsic_value():
    """Day 2: spot 90 and the 95 put marks 4.95 (bid 4.90, ask 5.00): intrinsic 5.00, extrinsic -0.05, so the model of assignment marks it. Day 3, spot 91: the
    95 is exercised at its intrinsic value 4.00 (54,000.00) and the 90 is sold at the bid 1.10 (14,850.00): 39,150.00 to close, plus the exercise fee of the 95 and
    the commission of the 90, 87.75 each: 17,374.50 - 39,150.00 - 175.50 = -21,951.00."""
    days = [day(D[0], 100, *ENTRY), day(D[1], 100, *ENTRY), day(D[2], 90, (4.90, 5.00), (1.00, 1.20)), day(D[3], 91, (3.90, 4.10), (1.10, 1.30)), day(D[4], 91, *ENTRY)]
    trade = sim(days).candidate(SPREAD, 0)
    assert (trade.exit_reason, trade.exit_idx) == ("early_assignment", 3)
    assert trade.net_pnl == pytest.approx(-21951.00)


def test_a_position_that_cannot_be_filled_is_no_trade_and_nothing_is_invented():
    base = profit_take_days()
    missing_long = [base[0], day(D[1], 100, ENTRY[0], None), *base[2:]]                      # the 90 has no quote on the fill day
    assert sim(missing_long).candidate(SPREAD, 0) is None
    no_credit = [base[0], day(D[1], 100, (0.50, 0.60), (0.40, 0.60)), *base[2:]]             # sells at 0.50, buys at 0.60: a debit at the bad side
    assert sim(no_credit).candidate(SPREAD, 0) is None
    no_bid = [base[0], day(D[1], 100, (0.00, 0.10), (0.00, 0.05)), *base[2:]]                # nothing to sell to
    assert sim(no_bid).candidate(SPREAD, 0) is None
    assert sim(base).candidate(SPREAD, len(base) - 1) is None                                # the last day has no next day to fill on
    assert sim(base, capital=Decimal(100)).candidate(SPREAD, 0) is None                    # 100 x 50 percent / 370 is less than one contract


def test_the_spread_never_crosses_in_the_buyers_favour_a_locked_quote_gives_the_same_price_on_both_sides():
    locked = [day(D[0], 100, *ENTRY), day(D[1], 100, (2.00, 2.00), (0.60, 0.60)), day(D[2], 100, *ENTRY)]
    trade = sim(locked).candidate(SPREAD, 0)
    assert trade.contracts == int(50_000 / ((5 - 1.40) * 100)) == 138                      # credit 2.00 - 0.60 = 1.40, margin 360
    assert trade.path[0] == pytest.approx(1.40 * 100 * 138 - 0.65 * 138 * 2 - (2.00 - 0.60) * 100 * 138)    # marked at the mid, which is the fill price


def test_a_position_still_open_when_the_history_ends_is_unfinished_and_counts_in_no_statistic():
    days = profit_take_days()[:4]
    trade = sim(days).candidate(SPREAD, 0)
    assert (trade.finished, trade.exit_reason, trade.exit_idx) == (False, "history_ended", 3)
    assert premium.expectancy_per_margin_dollar([trade]) == {"n": 0, "mean": None, "ci_low": None, "ci_high": None}


def test_a_column_enters_when_the_metric_passes_the_threshold_and_waits_for_its_exit():
    """Signals on every day with a threshold of 0.5: the first trade fills on day 1 and leaves on day 4; the next signal is day 4 (the exit day), fills on day 5
    and has one day before the history ends. The result of each day is the change of the path: -2,875.50, then 9,450.00, 6,075.00 and -985.50, then -2,875.50 again."""
    engine = sim(profit_take_days())
    pnl, trades = engine.column(SPREAD, 0.5, np.ones(6))
    assert [(t.fill_idx, t.exit_idx, t.finished) for t in trades] == [(1, 4, True), (5, 5, False)]
    assert pnl.tolist() == pytest.approx([0.0, -2875.50, 9450.00, 6075.00, -985.50, -2875.50])
    pnl_high, no_trades = engine.column(SPREAD, 2.0, np.ones(6))
    assert no_trades == [] and not pnl_high.any()
    _pnl, from_day_one = engine.column(SPREAD, 0.5, np.array([np.nan, 1.0, 1.0, 1.0, 1.0, 1.0]))
    assert [t.signal_idx for t in from_day_one][:1] == [1]                                  # a day with no metric never signals


def test_the_legs_of_each_structure_sit_where_the_gate_says():
    gate = dataclasses.replace(gate_mod.load_default(), structures=("iron_condor", "short_strangle", "short_straddle", "credit_call_spread", "short_call"))
    expiry = date(2024, 2, 1)
    strikes = range(80, 125, 5)
    quotes = tuple(quote(expiry, kind, k, "1.00", "1.10") for kind in ("put", "call") for k in strikes)
    chain = ChainDay(date(2024, 1, 2), Decimal(100), quotes)
    assert [(l.kind, l.side, l.strike) for l in premium.legs_for("iron_condor", chain, gate)] == [
        ("put", "short", Decimal(95)), ("put", "long", Decimal(90)), ("call", "short", Decimal(105)), ("call", "long", Decimal(110))]
    assert [(l.kind, l.side, l.strike) for l in premium.legs_for("short_strangle", chain, gate)] == [("put", "short", Decimal(95)), ("call", "short", Decimal(105))]
    assert [(l.kind, l.side, l.strike) for l in premium.legs_for("short_straddle", chain, gate)] == [("call", "short", Decimal(100)), ("put", "short", Decimal(100))]
    assert [(l.kind, l.side, l.strike) for l in premium.legs_for("credit_call_spread", chain, gate)] == [("call", "short", Decimal(105)), ("call", "long", Decimal(110))]
    assert premium.legs_for("covered_call", chain, gate) is None
    far = ChainDay(date(2024, 1, 2), Decimal(100), (quote(date(2024, 6, 28), "put", 95, "1.00", "1.10"),))
    assert premium.legs_for("short_put", far, gate) is None                                  # no expiration inside 21 to 60 days: no legs, no trade


def test_the_expiration_is_the_one_nearest_the_middle_of_the_window():
    gate = gate_mod.load_default()                                                           # 21 to 60: the middle is 40.5
    quotes = tuple(quote(date(2024, 1, 2) + timedelta(days=n), "put", 95, "1.00", "1.10") for n in (10, 28, 56, 70))
    chain = ChainDay(date(2024, 1, 2), Decimal(100), quotes)
    assert premium._pick_expiry(chain, gate.dte) == date(2024, 1, 30)                       # 28 days is 12.5 away, 56 is 15.5 away
