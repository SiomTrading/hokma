"""Margin, margin call and buying power (SPEC-020: B-11, L-11, L-19, L-20, N-07). Expected values are written by hand."""
import ast
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from hokma.backtest import margin
from hokma.backtest.margin import DayTradeLedger, IntradayMargin, Rejections

HERE = Path(margin.__file__).parent
NQ_END_OF_SERIES = date(2022, 1, 21)             # NQ margin: 16500 initial, 15000 maintenance
NOW = datetime(2022, 1, 21, 15, 0, tzinfo=timezone.utc)


def test_an_order_without_enough_equity_is_refused_and_counted():
    nq = margin.futures_margin("NQ", NQ_END_OF_SERIES)
    assert (nq.initial, nq.maintenance, nq.source) == (D(16500), D(15000), "series")
    refused = margin.check_order(nq, 1, equity=D(10000))
    assert (refused.ok, refused.code, refused.required, refused.available) == (False, "insufficient_margin", D(16500), D(10000))
    log = Rejections()
    log.record(refused, "FUT:NQ:2022-03", NOW)
    assert log.counts() == {"insufficient_margin": 1} and log.entries[0].required == D(16500)
    accepted = margin.check_order(nq, 1, equity=D(20000))
    assert (accepted.ok, accepted.code) == (True, None)


def test_margin_already_used_and_the_size_of_the_order_count():
    nq = margin.futures_margin("NQ", NQ_END_OF_SERIES)
    assert not margin.check_order(nq, 1, equity=D(20000), used_margin=D(5000)).ok       # 15000 free, 16500 needed
    assert margin.check_order(nq, 2, equity=D(33000)).ok and not margin.check_order(nq, 2, equity=D(32999)).ok
    assert margin.check_order(nq, -2, equity=D(33000)).ok                                # a short needs the same margin
    with pytest.raises(ValueError):
        Rejections().record(margin.check_order(nq, 1, equity=D(20000)), "FUT:NQ:2022-03", NOW)


def test_a_date_after_the_series_carries_the_warning_into_the_requirement():
    assert margin.futures_margin("NQ", date(2025, 11, 10)).warnings == ("historical_margin_missing",)
    assert margin.futures_margin("NQ", date(2021, 6, 1)).warnings == ()


def test_intraday_margin_lets_the_order_in_during_the_session_and_leaves_the_account_under_maintenance_at_the_close():
    # NQ on 2021-06-01: 17600 initial and 16000 maintenance. The funding account asks 500 per contract while the session is open.
    day, rule = date(2021, 6, 1), IntradayMargin(D(500))
    overnight = margin.futures_margin("NQ", day)
    assert not margin.check_order(overnight, 1, equity=D(10000)).ok                      # without the intraday rule: refused
    in_session = margin.futures_margin("NQ", day, intraday=rule, in_session=True)
    assert (in_session.mode, in_session.initial, in_session.maintenance, in_session.source) == ("intraday", D(500), D(500), "intraday")
    assert margin.check_order(in_session, 1, equity=D(10000)).ok and in_session.initial_for(2) == D(1000)
    at_close = margin.futures_margin("NQ", day, intraday=rule, in_session=False)
    assert (at_close.mode, at_close.maintenance) == ("overnight", D(16000))
    call = margin.margin_call(equity=D(10000), maintenance=at_close.maintenance_for(1), position_qty=1, bar_index=7)
    assert call is not None and (call.tag, call.fill_bar) == ("margin_call", 8)


def test_the_leveraged_model_divides_the_notional():
    assert margin.leveraged_margin(D(2000), 2, D(1), D(20)) == D(200)
    assert margin.leveraged_margin(D("1.10"), 100000, D(1), D(30)) == D("3666.666666666666666666666667")
    with pytest.raises(ValueError):
        margin.leveraged_margin(D(2000), 1, D(1), D("0.5"))
    with pytest.raises(ValueError):
        IntradayMargin(D(0))


def test_a_margin_call_closes_the_position_at_the_next_open():
    assert margin.margin_call(equity=D(15000), maintenance=D(15000), position_qty=1, bar_index=4) is None      # at the limit: no call
    call = margin.margin_call(equity=D(14999), maintenance=D(15000), position_qty=D(1), bar_index=4)
    assert (call.tag, call.trigger_bar, call.fill_bar, call.qty, call.equity) == ("margin_call", 4, 5, D(1), D(14999))
    assert margin.margin_call(equity=D(100), maintenance=D(15000), position_qty=-2, bar_index=9).qty == D(2)   # a short is closed too
    assert margin.margin_call(equity=D(100), maintenance=D(15000), position_qty=0, bar_index=9) is None        # nothing to liquidate


def test_buying_power_by_account_and_hours():
    assert margin.buying_power(D(20000)) == D(40000)                                 # under 25000: 2x
    assert margin.buying_power(D(30000)) == D(120000)                                # day trading account: 4x
    assert margin.buying_power(D(30000), regular_hours=False) == D(60000)
    assert margin.buying_power(D(20000), account="cash") == D(20000)
    assert margin.buying_power(D(-5)) == D(0)


def _ledger_with_three_day_trades():
    ledger = DayTradeLedger()
    for day in (date(2025, 11, 3), date(2025, 11, 4), date(2025, 11, 5)):           # Monday, Tuesday, Wednesday
        ledger.record(day)
    return ledger


def test_the_fourth_day_trade_in_five_business_days_is_refused_under_25000():
    ledger, thursday = _ledger_with_three_day_trades(), date(2025, 11, 6)
    refused = ledger.check(thursday, D(20000))
    assert (refused.ok, refused.code) == (False, "intraday_trading_rule")
    assert ledger.check(thursday, D(30000)).ok                                       # 25000 or more: the rule does not apply
    assert ledger.check(thursday, D(25000)).ok
    log = Rejections()
    log.record(refused, "EQ:AAPL", NOW)
    assert log.counts() == {"intraday_trading_rule": 1}


def test_the_window_is_five_business_days_and_holidays_do_not_count():
    ledger = _ledger_with_three_day_trades()
    assert ledger.check(date(2025, 11, 7), D(20000)).code == "intraday_trading_rule"    # window Nov 3 to 7: three trades inside
    assert ledger.check(date(2025, 11, 10), D(20000)).ok                              # window Nov 4 to 10: two inside
    with_holiday = DayTradeLedger(holidays={date(2025, 11, 7)}, days=list(ledger.days))
    assert with_holiday.check(date(2025, 11, 10), D(20000)).code == "intraday_trading_rule"    # window Nov 3 to 10 without the 7th
    assert DayTradeLedger().check(date(2025, 11, 6), D(20000)).ok                     # no day trades yet


def test_the_modules_of_this_block_have_no_path_to_a_broker():
    forbidden = ("hokma.exec.submit", "hokma.exec.approve", "hokma.broker.service", "hokma.broker.schwab", "hokma.broker.sim")
    for name in ("margin.py", "instruments.py", "continuous.py", "data_feed.py", "catalog.py"):
        tree = ast.parse((HERE / name).read_text(encoding="utf-8"))
        imported = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module] + [
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not [m for m in imported if m.startswith(forbidden)], name
