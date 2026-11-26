"""The book of the mixed fixture against the hand calculation (SPEC-018: AH-23, FT-103, FT-105, FT-106, OA-37, OA-49, OA-50).

The account opens with 1,000,000. The fixture rows and what they do, worked out by hand:

AAPL (multiplier 1): buy 100 @ 200 and buy 50 @ 212 give 150 at (20000 + 10600) / 150 = 204. Sell 60 @ 220 realizes (220 - 204) * 60 = 960 and leaves 90 at 204.
SPY (ETF, 1): buy 20 @ 500. Sell 50 @ 510 closes the 20 (realized 10 * 20 = 200) and crosses to short 30 at 510. Buy 10 @ 505 covers 10 of the short:
  realized (510 - 505) * 10 = 50. Total 250, short 20 at 510.
Option (multiplier 100): buy 2 @ 5 and 3 @ 6 give 5 at (10 + 18) / 5 = 5.6. Sell 4 @ 7 realizes (7 - 5.6) * 4 * 100 = 560, leaves 1 at 5.6.
NQ future (multiplier 20): buy 1 @ 20000, sell 1 @ 20050.25 realizes 50.25 * 20 = 1005. Sell 2 @ 20100 opens a short; buy 1 @ 20080 realizes
  (20100 - 20080) * 20 = 400. Total 1405, short 1 at 20100.
Realized: 960 + 250 + 560 + 1405 = 3175. Commissions: AAPL 1 + 0.5 + 0.6 = 2.1, SPY 1 + 1 + 0.5 = 2.5, option 1.3 + 1.95 + 2.6 = 5.85,
NQ 2.5 + 2.5 + 5 + 2.5 = 12.5; total 22.95. Dividend 23.
Cash of the fills: AAPL -20000 - 10600 + 13200 = -17400; SPY -10000 + 25500 - 5050 = 10450; option -1000 - 1800 + 2800 = 0;
NQ -400000 + 401005 + 804000 - 401600 = 403405. Total 396455. Cash = 1,000,000 + 396455 - 22.95 + 23 = 1,396,455.05.
"""
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from backoffice_support import AAPL, NQ, OPT, SPY, db, desks_db, env, load_mixed, ready  # noqa: F401

from hokma.backoffice import book, pnl, provenance, snapshot, valuation, views
from hokma.backoffice.cost_basis import reconstruct
from hokma.backoffice.book import Book
from account_links_support import unlink_all

MARKS = {AAPL: D("210"), SPY: D("505"), OPT: D("6.5"), NQ: D("20120")}


@pytest.fixture
def loaded(ready):
    report = load_mixed(ready)
    assert report.inserted == 27 and report.duplicates == []          # 13 fills, 13 commissions and the dividend
    return ready


def test_the_book_rebuilds_positions_with_the_multiplier_of_the_contract_sheet_FT_103(loaded):
    b = Book.load(loaded.account_id, db=loaded.db)
    pos = b.cost_positions()
    assert {k: (v.qty, v.avg_price, v.multiplier) for k, v in pos.items()} == {
        AAPL: (D(90), D(204), D(1)), SPY: (D(-20), D(510), D(1)), OPT: (D(1), D("5.6"), D(100)), NQ: (D(-1), D(20100), D(20))}
    flat = b.cost_positions(include_flat=True)
    assert {k: v.realized for k, v in flat.items()} == {AAPL: D(960), SPY: D(250), OPT: D(560), NQ: D(1405)}
    assert {k: v.commissions for k, v in flat.items()} == {AAPL: D("2.1"), SPY: D("2.5"), OPT: D("5.85"), NQ: D("12.5")}
    assert b.realized() == D(3175) and b.commissions() == D("22.95")
    assert b.other_cash(("dividend",)) == D(23)
    summary = b.summary()
    assert summary["realized"] == "3175" and summary["commissions"] == "22.95" and summary["net_realized"] == "3152.05"
    assert summary["positions"][OPT] == {"qty": "1", "avg_price": "5.6", "multiplier": "100"}


def test_the_cash_of_the_account_is_the_ledger_plus_the_entries_FT_103(loaded):
    assert book.cash(loaded.account_id, db=loaded.db) == D("1396455.05")


def test_the_option_and_the_future_use_their_own_multiplier_FT_103(loaded):
    rows = {e.contract_id: e.multiplier for e in book.entries(loaded.account_id, db=loaded.db, kinds=["fill"])}
    assert rows[OPT] == D(100) and rows[NQ] == D(20) and rows[AAPL] == D(1) and rows[SPY] == D(1)


def test_cost_basis_partial_buys_partial_sell_cross_short_and_multiplier_OA_49():
    def fill(cid, qty, price, mult=1):
        return {"contract_id": cid, "qty": D(qty), "price": D(price), "multiplier": D(mult), "commission": D(0)}

    partial = reconstruct([fill("A", 100, 200), fill("A", 50, 212)])["A"]
    assert (partial.qty, partial.avg_price) == (D(150), D(204))
    sold = reconstruct([fill("A", 100, 200), fill("A", 50, 212), fill("A", -60, 220)])["A"]
    assert (sold.qty, sold.avg_price, sold.realized) == (D(90), D(204), D(960))        # the average does not move on a sale
    cross = reconstruct([fill("S", 20, 500), fill("S", -50, 510)])["S"]
    assert (cross.qty, cross.avg_price, cross.realized) == (D(-30), D(510), D(200))    # the rest opens the short at the fill price
    short = reconstruct([fill("S", -30, 510), fill("S", 10, 505)])["S"]
    assert (short.qty, short.avg_price, short.realized) == (D(-20), D(510), D(50))
    future = reconstruct([fill("N", 1, 20000, 20), fill("N", -1, "20050.25", 20)])["N"]
    assert future.qty == 0 and future.avg_price == 0 and future.realized == D(1005)
    option = reconstruct([fill("O", 2, 5, 100), fill("O", 3, 6, 100), fill("O", -4, 7, 100)])["O"]
    assert (option.qty, option.avg_price, option.realized) == (D(1), D("5.6"), D(560))
    back_to_long = reconstruct([fill("X", -10, 50), fill("X", 25, 48)])["X"]           # a short covered and crossed to long: 10 * 2 = 20 realized
    assert (back_to_long.qty, back_to_long.avg_price, back_to_long.realized) == (D(15), D(48), D(20))


def test_pnl_of_the_period_by_desk_account_and_symbol_with_commissions_subtracted_FT_106(loaded):
    whole = pnl.for_account(loaded.account_id, start="2026-03-01 00:00:00", end="2026-03-31 00:00:00", db=loaded.db)
    assert (whole.realized, whole.commissions, whole.other, whole.net) == (D(3175), D("22.95"), D(23), D("3175.05"))
    assert whole.by_symbol[NQ] == {"realized": D(1405), "commissions": D("12.5"), "net": D("1392.5")}
    assert whole.by_symbol[AAPL]["net"] == D("957.9") and whole.by_symbol[SPY]["net"] == D("247.5") and whole.by_symbol[OPT]["net"] == D("554.15")
    by_desk = pnl.pnl(loaded.desk_id, start="2026-03-01 00:00:00", end="2026-03-31 00:00:00", db=loaded.db)
    assert by_desk.net == whole.net and by_desk.desk_id == loaded.desk_id
    window = pnl.for_account(loaded.account_id, start="2026-03-06 00:00:00", end="2026-03-06 23:59:59", db=loaded.db)      # NQ close 1005 - 2.5, SPY cross 200 - 1
    assert (window.realized, window.commissions, window.net) == (D(1205), D("3.5"), D("1201.5"))
    assert window.to_dict()["net"] == "1201.5"


def test_a_desk_without_account_has_an_empty_pnl_FT_106(ready):
    with ready.db.write() as u:
        unlink_all(u, ready.desk_id)
    report = pnl.pnl(ready.desk_id, start="2026-03-01 00:00:00", end="2026-03-31 00:00:00", db=ready.db)
    assert report.account_id is None and report.net == 0


def test_views_agree_with_the_book_to_the_cent_FT_105(loaded):
    b = Book.load(loaded.account_id, db=loaded.db)
    rows = {r["contract_id"]: r for r in views.positions_overview(b, MARKS)}
    assert rows[AAPL]["market_value"] == D(18900) and rows[AAPL]["unrealized"] == D(540) and rows[AAPL]["cost"] == D(18360)
    assert rows[SPY]["market_value"] == D(-10100) and rows[SPY]["unrealized"] == D(100)
    assert rows[OPT]["market_value"] == D(650) and rows[OPT]["unrealized"] == D(90)
    assert rows[NQ]["market_value"] == D(-402400) and rows[NQ]["unrealized"] == D(-400)
    cash = book.cash(loaded.account_id, db=loaded.db)
    overview = views.portfolio_overview(b, MARKS, cash=cash)
    assert overview["positions_value"] == D(-392950) and overview["equity"] == D("1003505.05")
    assert overview["realized"] == b.realized() and overview["commissions"] == b.commissions() and overview["unrealized"] == D(330)
    assert overview["total_profit"] == D("3505.05") == overview["equity"] - D(1_000_000)
    perf = views.portfolio_performance(b, MARKS, start_capital=D(1_000_000), cash=cash)
    assert perf["return"] == D("0.00350505")
    ledger = views.transactions_overview(b)
    assert len(ledger) == 27 and sum(r["amount"] for r in ledger) == cash - D(1_000_000)
    by_symbol = views.transactions_performance(b)
    assert sum(v["realized"] for v in by_symbol.values()) == b.realized() and sum(v["commissions"] for v in by_symbol.values()) == b.commissions()
    assert by_symbol[AAPL]["trades"] == 3 and by_symbol[NQ]["trades"] == 4
    text = views.to_text(overview)
    assert text["equity"] == "1003505.05" and text["missing"] == {}


def test_a_position_without_a_price_is_an_error_never_zero_AH_23(loaded):
    b = Book.load(loaded.account_id, db=loaded.db)
    position = b.cost_positions()[NQ]
    with pytest.raises(valuation.Unpriced) as raised:
        valuation.value(position, None)
    assert raised.value.symbol == NQ and raised.value.code == "unpriced" and NQ in str(raised.value)
    partial = {k: v for k, v in MARKS.items() if k != NQ}
    valued, errors = valuation.value_all(b.cost_positions().values(), partial)
    assert errors == {NQ: "unpriced"} and {v.contract_id for v in valued} == {AAPL, SPY, OPT}      # the others are still valued
    overview = views.portfolio_overview(b, partial, cash=D(1))
    assert overview["positions_value"] is None and overview["equity"] is None and overview["total_profit"] is None
    assert overview["missing"][f"unpriced:{NQ}"]
    row = next(r for r in views.positions_overview(b, partial) if r["contract_id"] == NQ)
    assert row["market_value"] is None and row["error"] == "unpriced"


def test_the_snapshot_marks_an_unpriced_position_and_does_not_make_up_a_value_AH_23_OA_50(loaded):
    shot = snapshot.snapshot(loaded.account_id, db=loaded.db, prices={k: v for k, v in MARKS.items() if k != NQ})
    assert shot.positions_value is None and shot.equity is None and not shot.complete
    assert shot.missing[f"unpriced:{NQ}"] == "no market price"
    assert shot.cash == D("1396455.05")               # what is known stays known


def test_the_snapshot_of_the_account_cash_positions_value_and_day_result_OA_50(loaded):
    now = datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc)
    shot = snapshot.snapshot(loaded.account_id, db=loaded.db, prices=MARKS, now=now)
    assert shot.cash == D("1396455.05") and shot.positions_value == D(-392950) and shot.equity == D("1003505.05")
    assert shot.realized_pnl == D(3175) and shot.total_profit == D("3175.05")        # realized 3175 - commissions 22.95 + dividend 23
    assert shot.day_pnl == D("397.5")                 # 12 March: the NQ cover realized 400 and cost 2.5
    assert "day_unrealized" in shot.missing           # the move of the open positions today is not known, and the snapshot says so
    assert shot.open_contracts == D(112) and shot.best_day_pnl == D("1201.5")     # best day: 6 March, NQ 1005 + SPY 200 less 3.5 of commissions
    assert shot.peak_equity == shot.equity            # the equity of today is the highest the book has seen
    assert {p["contract_id"] for p in shot.positions} == {AAPL, SPY, OPT, NQ}


def test_the_snapshot_gives_none_with_a_reason_and_never_zeros_OA_50(ready):
    with ready.db.write() as u:
        u.session.execute("DELETE FROM account_ledger WHERE account_id = :a", {"a": ready.account_id})
    shot = snapshot.snapshot(ready.account_id, db=ready.db, prices={})
    assert shot.cash is None and shot.equity is None and shot.missing["cash"]
    assert shot.positions_value == 0 and shot.day_pnl == 0 and shot.positions == ()     # an empty book really has no positions and no result: those are facts, not gaps
    assert not shot.complete


def test_the_snapshot_without_a_price_for_an_open_position_keeps_the_gap_OA_50(loaded):
    shot = snapshot.snapshot(loaded.account_id, db=loaded.db, prices={})
    assert shot.equity is None and shot.positions_value is None
    assert sorted(k for k in shot.missing if k.startswith("unpriced:")) == sorted(f"unpriced:{c}" for c in (AAPL, SPY, OPT, NQ))


def test_every_entry_points_to_its_origin_and_an_entry_without_one_is_not_written_OA_37(loaded):
    entries = book.entries(loaded.account_id, db=loaded.db, since_reset=False)
    assert entries and all(e.source == "import" and e.source_ref.startswith("import:") for e in entries)
    for source, ref in ((None, "x"), ("nowhere", "x"), ("sim", ""), ("sim", "   "), ("import", None), ("owner", "z" * 129)):
        with pytest.raises(provenance.MissingProvenance):
            book.append(loaded.account_id, db=loaded.db, kind="deposit", amount=D(1), source=source, source_ref=ref)
    with pytest.raises(provenance.MissingProvenance):
        book.append(loaded.account_id, db=loaded.db, kind="gift", amount=D(1), source="owner", source_ref="g1")
    assert len(book.entries(loaded.account_id, db=loaded.db, since_reset=False)) == len(entries)              # nothing was written
    first = book.append(loaded.account_id, db=loaded.db, kind="deposit", amount=D(5), source="owner", source_ref="adj-1")
    again = book.append(loaded.account_id, db=loaded.db, kind="deposit", amount=D(5), source="owner", source_ref="adj-1")
    assert first is not None and again is None                                                            # the same origin is written once


def test_the_book_is_append_only_nothing_updates_or_deletes_an_entry_OA_37():
    import pathlib

    root = pathlib.Path(book.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        body = path.read_text(encoding="utf-8").upper()
        for verb in ("DELETE FROM BOOK_ENTRIES", "UPDATE BOOK_ENTRIES", 'UPDATE("BOOK_ENTRIES"', "DROP TABLE BOOK_ENTRIES"):
            if verb in body:
                offenders.append(f"{path.name}: {verb}")
        if "u.session.update(\"book_entries\"" in path.read_text(encoding="utf-8"):
            offenders.append(f"{path.name}: session.update on book_entries")
    assert offenders == []
    assert not [n for n in dir(book) if n.startswith(("delete", "update", "remove"))]


def test_entries_before_a_reset_are_history_not_position_FT_103(loaded):
    with loaded.db.write() as u:
        u.session.insert("book_entries", {"account_id": loaded.account_id, "kind": "adjustment", "amount": "0", "currency": "USD", "source": "owner",
                                          "source_ref": "reset:test", "occurred_at": "2026-03-20 00:00:00", "recorded_at": "2026-03-20 00:00:00", "note": "reset"})
    assert Book.load(loaded.account_id, db=loaded.db).cost_positions() == {}
    assert Book([e for e in book.entries(loaded.account_id, db=loaded.db, since_reset=False)]).realized() == D(3175)


def test_the_peak_equity_is_the_highest_the_book_has_seen_OA_50(loaded):
    low = {**MARKS, NQ: D("21000")}                      # the short loses 18000 at this mark: equity 985905.05, below the best realized moment
    shot = snapshot.snapshot(loaded.account_id, db=loaded.db, prices=low, now=datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc))
    assert shot.equity == D("985905.05")
    assert shot.peak_equity == D("1003175.05")           # 1,000,000 + 3175.05 once the last fill and its commission are both booked
