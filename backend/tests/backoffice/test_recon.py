"""Reconciliation of the book against the statement and the trace of its breaks (SPEC-018: AFS-001, AFS-002). Values are worked out by hand."""
import inspect
from datetime import date
from decimal import Decimal

import pytest
from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma.backoffice import book as book_module
from hokma.backoffice import recon
from hokma.backoffice.book import BookEntry
from hokma.backoffice.recon import NotReadOnly, ReadOnlyBook, ReconError, Statement

D = Decimal
NQ = "FUT:NQ:2026-12"


def entry(i, kind, ref, amount, *, day="2026-03-02", contract=None, qty=None, price=None, mult=None, desk=7, source="schwab"):
    return BookEntry(i, 1, desk, kind, contract, None if qty is None else D(qty), None if price is None else D(price), None if mult is None else D(mult),
                     D(amount), source, ref, f"{day} 15:30:00")


def row(kind, ref, amount, *, day="2026-03-02", contract=None, qty=None, currency="USD", price=None):
    return {"kind": kind, "ref": ref, "amount": amount, "date": day, "contract_id": contract, "qty": qty, "currency": currency, "price": price}


AS_OF = "2026-03-10"


def run(entries, rows, **kw):
    return recon.compare(entries, Statement.from_rows(rows, as_of=AS_OF, **kw.pop("statement", {})), **kw)


def one(breaks, key_part):
    found = [b for b in breaks if key_part in b.key]
    assert len(found) == 1, [b.key for b in breaks]
    return found[0]


def test_identical_lines_match_and_leave_no_break():
    book = [entry(1, "fill", "F1", "-840000", contract=NQ, qty="2", price="21000", mult="20")]
    breaks, summary = run(book, [row("fill", "F1", "-840000", contract=NQ, qty="2")])
    assert breaks == [] and summary["matched"] == 1 and summary["local_lines"] == 1 and summary["statement_lines"] == 1


def test_quantity_has_tolerance_zero_and_is_valued_with_the_multiplier():
    book = [entry(1, "fill", "F2", "-420000", contract=NQ, qty="1", price="21000", mult="20", day="2026-03-03")]
    breaks, _ = run(book, [row("fill", "F2", "-420000", contract=NQ, qty="2", day="2026-03-03")])
    b = one(breaks, "F2")
    assert b.bucket == "quantity" and b.diff == D(1) and b.cause == "duplicate_or_missing"
    assert b.impact == D("420000")                                    # one contract * 21000 * multiplier 20


def test_amount_tolerance_is_one_cent_inclusive():
    book = [entry(1, "dividend", "D1", "50.00"), entry(2, "dividend", "D2", "10.00")]
    breaks, summary = run(book, [row("dividend", "D1", "50.01"), row("dividend", "D2", "10.02")])
    assert [b.key for b in breaks] == ["dividend|-|D2"] and summary["matched"] == 1
    assert breaks[0].bucket == "amount" and breaks[0].diff == D("0.02") and breaks[0].cause == "data_quality"


def test_commission_difference_has_the_commission_cause_and_is_not_material():
    book = [entry(1, "commission", "F3", "-2.50")]
    breaks, _ = run(book, [row("commission", "F3", "-2.60")])
    b = breaks[0]
    assert b.bucket == "amount" and b.cause == "commission" and b.diff == D("-0.10") and b.impact == D("0.10") and not b.material


def test_a_fill_amount_that_differs_by_the_commission_the_book_holds_apart_is_a_commission_break():
    book = [entry(1, "fill", "F5", "-1000.00", contract="EQ:SPY", qty="10", price="100", mult="1"), entry(2, "commission", "F5", "-1.00", contract="EQ:SPY")]
    breaks, _ = run(book, [row("fill", "F5", "-1001.00", contract="EQ:SPY", qty="10"), row("commission", "F5", "0", contract="EQ:SPY")][:1])
    amount = [b for b in breaks if b.bucket == "amount"]
    assert len(amount) == 1 and amount[0].cause == "commission" and amount[0].diff == D("-1.00")


def test_same_line_on_another_day_is_a_timing_break():
    book = [entry(1, "interest", "I1", "5.00", day="2026-03-04")]
    breaks, _ = run(book, [row("interest", "I1", "5.00", day="2026-03-06")])
    assert breaks[0].bucket == "timing" and breaks[0].cause == "timing" and breaks[0].diff == D(2) and breaks[0].impact == 0


def test_a_line_only_one_side_has_is_one_side_with_a_cause():
    book = [entry(1, "deposit", "DEP9", "1000.00", day="2026-03-01")]
    breaks, _ = run(book, [row("fill", "F9", "-6000.20", contract="EQ:SPY", qty="10", day="2026-03-04")])
    local, broker = one(breaks, "DEP9"), one(breaks, "F9")
    assert (local.bucket, local.cause, local.impact, local.broker) == ("one_side", "duplicate_or_missing", D("1000"), None)
    assert (broker.bucket, broker.cause, broker.impact, broker.local) == ("one_side", "duplicate_or_missing", D("6000.20"), None)


def test_a_recent_line_missing_from_the_statement_is_timing_and_an_old_one_is_not():
    book = [entry(1, "fill", "F8", "-1000", contract="EQ:SPY", qty="1", day="2026-03-09"), entry(2, "fill", "F0", "-900", contract="EQ:SPY", qty="1", day="2026-02-20")]
    breaks, _ = run(book, [])
    assert one(breaks, "F8").cause == "timing" and one(breaks, "F0").cause == "duplicate_or_missing"


def test_the_same_trade_under_another_contract_is_a_mapping_break_on_both_sides():
    book = [entry(1, "fill", "F7", "-2500", contract="EQ:QQQ", qty="5")]
    breaks, _ = run(book, [row("fill", "F7", "-2500", contract="EQ:QQQ2", qty="5")])
    assert len(breaks) == 2 and {b.cause for b in breaks} == {"mapping"} and {b.bucket for b in breaks} == {"one_side"}
    assert sorted(b.impact for b in breaks) == [D("2500"), D("2500")]


def test_a_repeated_reference_in_the_book_leaves_one_duplicate():
    book = [entry(1, "dividend", "D3", "7.00"), entry(2, "dividend", "D3", "7.00")]
    breaks, summary = run(book, [row("dividend", "D3", "7.00")])
    assert len(breaks) == 1 and breaks[0].cause == "duplicate_or_missing" and breaks[0].local["entry_id"] == 2 and summary["matched"] == 1


def test_a_statement_line_in_another_currency_is_a_currency_break():
    book = [entry(1, "interest", "I2", "3.00")]
    breaks, _ = run(book, [row("interest", "I2", "3.00", currency="eur")])
    assert breaks[0].cause == "currency" and breaks[0].bucket == "amount" and breaks[0].impact == D("3")


def test_entries_after_the_statement_date_and_internal_kinds_are_not_compared():
    book = [entry(1, "fill", "LATE", "-5", contract="EQ:SPY", qty="1", day="2026-03-11"), entry(2, "accrual", "ACC", "1.00"), entry(3, "adjustment", "ADJ", "1.00")]
    breaks, summary = run(book, [])
    assert breaks == [] and summary["local_lines"] == 0


def test_the_sum_of_desk_capital_against_the_account_value_is_one_more_check():
    breaks, summary = run([], [], statement={"account_value": "1010000"}, account_capital=D("1000000"))
    b = breaks[0]
    assert b.is_nav and b.bucket == "amount" and b.diff == D("10000") and b.impact == D("10000")
    assert summary["nav_tieout"]["status"] == "break" and summary["nav_tieout"]["difference"] == "10000"
    ok, summary = run([], [], statement={"account_value": "1000000.01"}, account_capital=D("1000000"))
    assert ok == [] and summary["nav_tieout"]["status"] == "ok"


def test_the_output_is_ordered_by_absolute_money_and_every_break_has_one_cause():
    book = [entry(1, "fill", "F2", "-420000", contract=NQ, qty="1", price="21000", mult="20"), entry(2, "dividend", "D2", "10.00"),
            entry(3, "commission", "F3", "-2.50"), entry(4, "deposit", "DEP9", "1000.00", day="2026-03-01")]
    rows = [row("fill", "F2", "-420000", contract=NQ, qty="2"), row("dividend", "D2", "10.02"), row("commission", "F3", "-2.60"),
            row("fill", "F9", "-6000.20", contract="EQ:SPY", qty="10", day="2026-03-04")]
    breaks, summary = run(book, rows, account_capital=D("1000000"), statement={"account_value": "1010000"})
    assert [b.impact for b in breaks] == [D("420000"), D("10000"), D("6000.20"), D("1000"), D("0.10"), D("0.02")]
    assert all(b.cause in recon.CAUSES for b in breaks) and summary["breaks"] == 6
    assert sum(summary["by_bucket"].values()) == 6


def test_a_statement_of_another_account_is_refused(ready):
    other = Statement.from_rows([], as_of=AS_OF, account_id=ready.account_id + 99)
    with pytest.raises(ReconError):
        recon.reconcile(ready.account_id, statement=other, db=ready.db)


def test_reconcile_records_the_run_and_its_breaks_and_leaves_the_book_alone(ready):
    for ref, amount in (("DEP1", "1000"), ("DEP2", "500")):
        book_module.append(ready.account_id, db=ready.db, kind="deposit", amount=D(amount), source="import", source_ref=ref, occurred_at="2026-03-01 10:00:00")
    before = len(book_module.entries(ready.account_id, db=ready.db))
    statement = Statement.from_rows([row("deposit", "DEP1", "1000", day="2026-03-01")], as_of=AS_OF, account_id=ready.account_id, account_value="1000010")
    result = recon.reconcile(ready.account_id, statement=statement, db=ready.db)
    assert result.status == "breaks" and result.recon_id
    assert [b.key for b in result.breaks] == ["deposit|-|DEP2", "nav|capital|account"]           # 500 of money at stake, then 10
    with ready.db.read() as u:
        saved = u.session.query("SELECT * FROM reconciliations")
        rows = u.session.query("SELECT * FROM recon_breaks ORDER BY id")
    assert len(saved) == 1 and saved[0]["status"] == "breaks" and len(rows) == len(result.breaks) == 2
    nav = next(r for r in rows if r["key"].startswith("nav"))
    assert nav["bucket"] == "amount" and Decimal(nav["diff"]) == Decimal("10") and nav["cause"] == "data_quality"      # capital 1,000,000 against 1,000,010
    again = recon.load_breaks(result.recon_id, db=ready.db)
    assert [b.key for b in again] == [b.key for b in result.breaks]
    assert len(book_module.entries(ready.account_id, db=ready.db)) == before


def test_traces_are_stored_in_the_break_table_only(ready):
    book_module.append(ready.account_id, db=ready.db, kind="deposit", amount=D("500"), source="import", source_ref="DEP2", occurred_at="2026-03-01 10:00:00")
    result = recon.reconcile(ready.account_id, statement=Statement.from_rows([], as_of=AS_OF), db=ready.db)
    reader = ReadOnlyBook.load(ready.account_id, db=ready.db)
    traced = {b.id: recon.trace(b, reader, today=date(2026, 3, 10)) for b in result.breaks}
    before = len(book_module.entries(ready.account_id, db=ready.db))
    assert recon.apply_traces(traced, db=ready.db) == 1
    with ready.db.read() as u:
        stored = u.session.one("SELECT * FROM recon_breaks")
    assert stored["action"] == "adjust" and stored["owner"] == "owner" and stored["explanation"].endswith(".") and str(stored["due_at"]).startswith("2026-03-13")
    assert len(book_module.entries(ready.account_id, db=ready.db)) == before


# ---------------------------------------------------------------------------------------------------------------- trace and critic

TODAY = date(2026, 3, 10)           # a Tuesday


def traced_breaks():
    book = [entry(1, "fill", "F2", "-420000", contract=NQ, qty="1", price="21000", mult="20", day="2026-03-03"),
            entry(2, "commission", "F3", "-2.50"), entry(3, "interest", "I1", "5.00", day="2026-03-04")]
    rows = [row("fill", "F2", "-420000", contract=NQ, qty="2", day="2026-03-03"), row("commission", "F3", "-2.60"), row("interest", "I1", "5.00", day="2026-03-06"),
            row("dividend", "D9", "12.50", contract="EQ:SPY", day="2026-03-05")]
    breaks, _ = run(book, rows, account_capital=D("1000000"), statement={"account_value": "1010000"})
    return ReadOnlyBook(book), {b.key.split("|")[-1]: b for b in breaks}


def test_each_material_break_gets_one_sentence_an_owner_a_date_and_an_action():
    reader, by = traced_breaks()
    q = recon.trace(by["F2"], reader, today=TODAY)
    assert q.material and q.action == "adjust" and q.owner == "owner" and q.expected_date == "2026-03-13"          # 3 business days after Tuesday
    assert "\n" not in q.sentence and "desk 7" in q.sentence and NQ in q.sentence and "because" in q.sentence and q.sentence.endswith(".")
    t = recon.trace(by["I1"], reader, today=TODAY)
    assert t.action == "watch" and t.owner == "back_office" and t.expected_date == "2026-03-12"
    d = recon.trace(by["D9"], reader, today=TODAY)
    assert d.material and d.action == "adjust" and "never recorded it" in d.sentence and "the broker posted" in d.sentence
    nav = recon.trace(by["account"], reader, today=TODAY)
    assert nav.owner == "owner" and nav.action == "watch" and "only the owner changes capital" in nav.sentence and nav.expected_date == "2026-03-17"


def test_a_small_amount_break_is_suppressed_but_still_explained():
    reader, by = traced_breaks()
    c = recon.trace(by["F3"], reader, today=TODAY)
    assert not c.material and c.action == "suppress" and c.sentence
    strict = recon.trace(by["F3"], reader, today=TODAY, materiality=D("0.05"))
    assert strict.material and strict.action == "adjust"


def test_the_business_day_count_skips_weekends():
    assert recon.add_business_days(date(2026, 3, 13), 2) == date(2026, 3, 17)       # Friday plus two business days
    assert recon.add_business_days(date(2026, 3, 14), 1) == date(2026, 3, 16)       # a Saturday


def test_trace_has_no_path_to_write_to_the_book():
    reader, by = traced_breaks()
    assert list(inspect.signature(recon.trace).parameters) == ["break_row", "book_reader", "today", "materiality"]       # no database, no session, no writer
    source = inspect.getsource(recon.trace) + inspect.getsource(recon.verify_break)
    for forbidden in ("append(", "record_fill", ".write", "session", "insert", "get_database"):
        assert forbidden not in source, forbidden
    assert not any(hasattr(reader, name) for name in recon.READ_ONLY_FORBIDDEN)
    with pytest.raises(AttributeError):
        reader.extra = 1

    class Writer(ReadOnlyBook):
        __slots__ = ()

        def append(self, *a):
            raise AssertionError("must not be reachable")

    with pytest.raises(NotReadOnly):
        recon.trace(by["F2"], Writer(reader.entries()), today=TODAY)
    with pytest.raises(NotReadOnly):
        recon.verify_break(by["F2"], Writer(reader.entries()))
    with pytest.raises(NotReadOnly):
        recon.trace(by["F2"], object(), today=TODAY)


def test_the_critic_checks_each_break_against_the_book_again():
    reader, by = traced_breaks()
    assert recon.verify_break(by["F2"], reader).status == "confirmed"
    assert recon.verify_break(by["D9"], reader).status == "confirmed"           # the book still has no such line
    assert recon.verify_break(by["account"], reader).status == "unverifiable"
    emptied = ReadOnlyBook([e for e in reader.entries() if e.source_ref != "F2"])
    assert recon.verify_break(by["F2"], emptied).status == "stale"
    changed = ReadOnlyBook([entry(1, "fill", "F2", "-1", contract=NQ, qty="1")])
    assert recon.verify_break(by["F2"], changed).status == "stale"
    filled = ReadOnlyBook([*reader.entries(), entry(9, "dividend", "D9", "12.50", contract="EQ:SPY")])
    assert recon.verify_break(by["D9"], filled).status == "stale"
