"""Daily settlement of futures to market (SPEC-020: L-18, L-27). Values worked out by hand.

The account opens with 1,000,000. A long of 1 NQ (multiplier 20) is bought at 20000: the book pays the notional 400000, so its cash is 600000.
Closes: 20010, 20005 and 20030. The cash moves (20010 - 20000) * 20 = +200, (20005 - 20010) * 20 = -100 and (20030 - 20005) * 20 = +500, so the
futures cash is 1,000,200, then 1,000,100, then 1,000,600. The book at 20030 is 600000 + 20030 * 20 = 1,000,600 as well.
"""
from datetime import date
from decimal import Decimal as D

import pytest
from backoffice_support import db, desks_db, env, ready  # noqa: F401

from hokma.backoffice import book, settlement, snapshot, valuation

NQ, ES, SPY = "FUT:NQ:2026-03", "FUT:ES:2026-03", "ETF:SPY"
DAYS = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)]


def fill(r, ref, contract, qty, price, mult, at="2026-03-01 15:00:00"):
    cash = -qty * price * mult
    assert book.append(r.account_id, db=r.db, kind="fill", amount=cash, source="sim", source_ref=ref, occurred_at=at, contract_id=contract, qty=qty, price=price,
                       multiplier=mult) is not None


def test_a_long_nq_settled_three_days_moves_the_cash_by_the_difference_times_20_L_18(ready):
    fill(ready, "F1", NQ, D(1), D(20000), D(20))
    assert book.cash(ready.account_id, db=ready.db) == D(600000)
    days = settlement.settle_account(ready.account_id, [(DAYS[0], {NQ: D(20010)}), (DAYS[1], {NQ: D(20005)}), (DAYS[2], {NQ: D(20030)})], db=ready.db)
    assert [s.moves[NQ] for s in days] == [D(200), D(-100), D(500)]
    assert [s.cash_move for s in days] == [D(200), D(-100), D(500)]
    assert [s.cash_usd for s in days] == [D(1000200), D(1000100), D(1000600)] == [s.nav_usd for s in days]
    # the book of H2 balances: its own equity at the last price is the same number
    snap = snapshot.snapshot(ready.account_id, db=ready.db, prices={NQ: D(20030)})
    assert snap.equity == D(1000600) == days[-1].nav_usd


def test_a_short_and_a_long_settle_with_their_own_sign_L_18(ready):
    fill(ready, "F1", NQ, D(1), D(20000), D(20))
    fill(ready, "F2", ES, D(-1), D(5000), D(50))                  # short 1 ES: the book receives 250000
    days = settlement.settle_account(ready.account_id, [(DAYS[0], {NQ: D(20010), ES: D(4990)}), (DAYS[1], {NQ: D(19990), ES: D(5010)})], db=ready.db)
    # day 1: NQ +10 * 20 = +200; ES short, price down 10: +10 * 50 = +500. Day 2: NQ -20 * 20 = -400; ES short, price up 20: -20 * 50 = -1000
    assert [dict(s.moves) for s in days] == [{NQ: D(200), ES: D(500)}, {NQ: D(-400), ES: D(-1000)}]
    assert [s.cash_usd for s in days] == [D(1000700), D(999300)]
    assert snapshot.snapshot(ready.account_id, db=ready.db, prices={NQ: D(19990), ES: D(5010)}).equity == D(999300)


def test_only_futures_are_settled_and_other_positions_stay_in_the_book_L_18(ready):
    fill(ready, "F1", NQ, D(1), D(20000), D(20))
    fill(ready, "F2", SPY, D(10), D(600), D(1))
    days = settlement.settle_account(ready.account_id, [(DAYS[0], {NQ: D(20010)})], other_prices={SPY: D(605)}, db=ready.db)
    # cash of the book 1,000,000 - 400000 - 6000 = 594000; the notional is added back (994000) and the day moves +200
    assert set(days[0].moves) == {NQ} and days[0].cash_usd == D(994200)
    assert snapshot.snapshot(ready.account_id, db=ready.db, prices={NQ: D(20010), SPY: D(605)}).equity == D(994200) + D(6050)   # the book: 594000 + 400200 + 6050
    with pytest.raises(valuation.Unpriced):                                  # the ETF has no price: never worth zero
        settlement.settle_account(ready.account_id, [(DAYS[0], {NQ: D(20010)})], db=ready.db)


def test_a_missing_settlement_price_or_a_bad_order_of_closes_is_an_error_L_27(ready):
    fill(ready, "F1", NQ, D(1), D(20000), D(20))
    with pytest.raises(valuation.Unpriced):
        settlement.settle_account(ready.account_id, [(DAYS[0], {ES: D(5000)})], db=ready.db)
    for closes in ([], [(DAYS[1], {NQ: D(1)}), (DAYS[0], {NQ: D(1)})], [(DAYS[0], {NQ: D(1)}), (DAYS[0], {NQ: D(1)})]):
        with pytest.raises(settlement.SettlementError) as e:
            settlement.settle_account(ready.account_id, closes, db=ready.db)
        assert e.value.code == "no_closes"


def test_an_account_without_a_ledger_cannot_be_settled_L_27(ready):
    with ready.db.write() as u:
        other = u.broker.create_account(ready.broker_id, "Real", 100)
        u.session.execute("DELETE FROM account_ledger WHERE account_id = :a", {"a": other.id})
    with pytest.raises(settlement.SettlementError) as e:
        settlement.settle_account(other.id, [(DAYS[0], {NQ: D(1)})], db=ready.db)
    assert e.value.code == "no_ledger"


def test_settle_days_on_plain_positions_matches_the_hand_values_L_18():
    from hokma.backoffice.cost_basis import CostPosition

    long_nq = CostPosition(NQ, D(1), D(20000), D(20), D(0), D(0))
    days = settlement.settle_days([long_nq], [(DAYS[0], {NQ: D(20010)}), (DAYS[1], {NQ: D(20005)}), (DAYS[2], {NQ: D(20030)})], cash_usd=D(1000000))
    assert [s.cash_usd for s in days] == [D(1000200), D(1000100), D(1000600)]
    assert settlement.variation(D(-2), D(5000), D(4990), D(50)) == D(1000)
    assert settlement.futures_positions([long_nq, CostPosition(SPY, D(10), D(600), D(1), D(0), D(0)), CostPosition(ES, D(0), D(0), D(50), D(0), D(0))]) == (long_nq,)
