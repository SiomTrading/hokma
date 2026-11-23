"""Roll-forward of the capital of an account and of a desk (SPEC-018: AFS-016). Values by hand."""
from decimal import Decimal

from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma.backoffice import book as book_module
from hokma.backoffice import rollforward
from hokma.backoffice.book import BookEntry

D = Decimal
NQ = "FUT:NQ:2026-12"
START, END = "2026-03-01 00:00:00", "2026-03-31 23:59:59"


def e(i, kind, amount, day, *, contract=None, qty=None, price=None, mult=None, desk=7):
    return BookEntry(i, 1, desk, kind, contract, None if qty is None else D(qty), None if price is None else D(price), None if mult is None else D(mult), D(amount),
                     "import", f"R{i}", f"{day} 15:00:00")


MARCH = [e(1, "deposit", "5000", "2026-03-02"), e(2, "fill", "-840000", "2026-03-03", contract=NQ, qty="2", price="21000", mult="20"),
         e(3, "commission", "-5", "2026-03-03"), e(4, "fill", "422000", "2026-03-10", contract=NQ, qty="-1", price="21100", mult="20"),
         e(5, "commission", "-2.5", "2026-03-10"), e(6, "dividend", "12.50", "2026-03-12"), e(7, "interest", "3.00", "2026-03-15"),
         e(8, "withdrawal", "-2000", "2026-03-20")]


def test_the_lines_add_up_to_the_closing_balance_and_point_to_their_entries():
    r = rollforward.roll_forward(MARCH, opening=D(1000000), start=START, end=END)
    amounts = {x.name: x.amount for x in r.lines}
    # realized: sold 1 at 21100 against an average of 21000 with multiplier 20 = 2000
    assert amounts == {"deposits": D(5000), "realized_result": D(2000), "dividends_and_interest": D("15.5"), "accruals": 0, "adjustments": 0,
                       "commissions_and_fees": D("-7.5"), "withdrawals": D(-2000)}
    assert [x.name for x in r.lines][:2] == ["deposits", "realized_result"]
    assert r.closing_from_lines == D("1005008") == r.closing_actual and r.unexplained == 0 and r.balanced
    assert r.line("deposits").entry_ids == (1,) and r.line("realized_result").entry_ids == (2, 4) and r.line("commissions_and_fees").entry_ids == (3, 5)
    assert r.line("dividends_and_interest").entry_ids == (6, 7) and r.line("withdrawals").entry_ids == (8,)


def test_a_difference_is_shown_as_an_unexplained_line_and_never_absorbed():
    r = rollforward.roll_forward(MARCH, opening=D(1000000), start=START, end=END, reported_closing=D(1005003))
    assert not r.balanced and r.unexplained == D(-5) and r.closing_actual == D(1005003)
    assert r.lines[-1].name == "unexplained" and r.lines[-1].amount == D(-5) and r.lines[-1].entry_ids == ()
    assert r.closing_from_lines == D("1005008")                                         # the real lines are untouched
    assert r.line("deposits").amount == D(5000) and r.line("realized_result").amount == D(2000)
    assert r.to_dict()["unexplained"] == "-5" and r.to_dict()["balanced"] is False


def test_the_second_month_starts_from_the_balance_the_first_one_ended_with():
    april = [*MARCH, e(9, "fill", "-420500", "2026-04-02", contract=NQ, qty="1", price="21025", mult="20"), e(10, "commission", "-1", "2026-04-02")]
    opening = rollforward.capital_at(april, base=D(1000000), before="2026-04-01 00:00:00")
    assert opening == D("1005008")                                                       # the end of March
    r = rollforward.roll_forward(april, opening=opening, start="2026-04-01 00:00:00", end="2026-04-30 23:59:59")
    assert r.line("realized_result").amount == 0 and r.line("commissions_and_fees").amount == D(-1) and r.closing_from_lines == D("1005007") and r.balanced


def test_it_balances_for_a_desk_and_for_the_account_from_the_database(ready):
    def add(kind, amount, day, ref, **kw):
        book_module.append(ready.account_id, db=ready.db, kind=kind, amount=D(amount), source="import", source_ref=ref, occurred_at=f"{day} 15:00:00",
                           desk_id=ready.desk_id, **kw)

    add("deposit", "5000", "2026-03-02", "A1")
    add("fill", "-840000", "2026-03-03", "A2", contract_id=NQ, qty=D(2), price=D(21000), multiplier=D(20))
    add("commission", "-5", "2026-03-03", "A3")
    add("fill", "422000", "2026-03-10", "A4", contract_id=NQ, qty=D(-1), price=D(21100), multiplier=D(20))
    add("dividend", "12.5", "2026-03-12", "A5")
    account = rollforward.for_account(ready.account_id, start=START, end=END, db=ready.db)
    desk = rollforward.for_account(ready.account_id, start=START, end=END, desk_id=ready.desk_id, db=ready.db)
    for r in (account, desk):
        assert r.opening == D(1000000) and r.line("realized_result").amount == D(2000) and r.closing_from_lines == D("1007007.5") and r.balanced
    assert account.line("deposits").amount == D(5000)
