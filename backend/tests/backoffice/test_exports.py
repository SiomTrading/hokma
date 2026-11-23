"""Spreadsheet exports of the book and of a reconciliation (SPEC-018: SKL-015)."""
import inspect
from decimal import Decimal

import pytest
from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma.backoffice import book as book_module
from hokma.backoffice import exports, recon
from hokma.backoffice.exports import ExportError
from hokma.backoffice.recon import Statement

D = Decimal


def real_clock(monkeypatch):
    """The `ready` fixture freezes time.time at 1970 for the simulated broker; a zip file cannot carry that date."""
    monkeypatch.setattr("time.time", lambda: 1_767_225_600.0)


def fill_book(r):
    kw = dict(db=r.db, source="import", desk_id=r.desk_id)
    book_module.append(r.account_id, kind="deposit", amount=D("5000.10"), source_ref="X1", occurred_at="2026-03-02 15:00:00", note="=HYPERLINK(1)", **kw)
    book_module.append(r.account_id, kind="fill", amount=D("-840000"), source_ref="X2", occurred_at="2026-03-03 15:00:00", contract_id="FUT:NQ:2026-12", qty=D(2),
                       price=D("21000.25"), multiplier=D(20), **kw)
    book_module.append(r.account_id, kind="commission", amount=D("-5"), source_ref="X2", occurred_at="2026-03-03 15:00:00", contract_id="FUT:NQ:2026-12", **kw)


def expected(rows):
    return [[exports.safe_cell(v) for v in row] for row in rows]


def test_the_book_in_xlsx_and_csv_reads_back_cell_by_cell(ready, tmp_path, monkeypatch):
    real_clock(monkeypatch)
    fill_book(ready)
    files = exports.export_book(ready.account_id, tmp_path, db=ready.db)
    assert set(files) == {"xlsx", "csv:book"} and all(p.exists() for p in files.values())
    items = book_module.entries(ready.account_id, db=ready.db, since_reset=False)
    want = expected(exports.book_sheet(items))
    assert len(want) == 4 and want[0] == exports.BOOK_HEAD
    got = exports.read_back(files["xlsx"])["book"]
    assert [list(r) for r in got] == want                                                    # same values in every cell, numbers as their exact text
    csv_rows = exports.read_back(files["csv:book"])[files["csv:book"].stem]
    assert csv_rows == [["" if v is None else str(v) for v in row] for row in want]
    assert got[2][7] == "-840000" and got[2][5] == "21000.25" and got[1][11] == "'=HYPERLINK(1)"     # no float, and a formula text is made inert


def test_an_export_never_replaces_or_restructures_an_existing_file(ready, tmp_path, monkeypatch):
    real_clock(monkeypatch)
    fill_book(ready)
    existing = tmp_path / f"book-{ready.account_id}.xlsx"
    existing.write_bytes(b"customer file")
    with pytest.raises(ExportError):
        exports.export_book(ready.account_id, tmp_path, formats=("xlsx",), db=ready.db)
    assert existing.read_bytes() == b"customer file"
    exports.export_book(ready.account_id, tmp_path, formats=("csv",), db=ready.db)
    with pytest.raises(ExportError):
        exports.export_book(ready.account_id, tmp_path, formats=("csv",), db=ready.db)


def test_the_reconciliation_exports_its_breaks_in_order(ready, tmp_path, monkeypatch):
    real_clock(monkeypatch)
    fill_book(ready)
    result = recon.reconcile(ready.account_id, statement=Statement.from_rows([], as_of="2026-03-31"), db=ready.db)
    files = exports.export_reconciliation(result.recon_id, tmp_path, db=ready.db)
    rows = exports.read_back(files["xlsx"])["breaks"]
    assert rows[0] == exports.BREAK_HEAD and len(rows) == 1 + len(result.breaks) == 4
    assert [r[2] for r in rows[1:]] == [b.key for b in result.breaks]
    assert rows[1][1] == "one_side" and rows[1][3] == "duplicate_or_missing"


def test_names_formats_and_cells_are_checked(tmp_path):
    with pytest.raises(ExportError):
        exports.write_table(tmp_path, "../escape", {"a": [["x"]]})
    with pytest.raises(ExportError):
        exports.write_table(tmp_path, "ok", {"a": [["x"]]}, formats=("pdf",))
    assert exports.safe_cell("=1+1") == "'=1+1" and exports.safe_cell("-5") == "-5" and exports.safe_cell("-cmd") == "'-cmd"
    assert exports.safe_cell(D("1E+2")) == "100" and exports.safe_cell(None) is None and exports.safe_cell(7) == 7
    assert list(inspect.signature(exports.export_book).parameters)[:2] == ["account_id", "folder"]         # the folder is the code's; there is no file name to choose
