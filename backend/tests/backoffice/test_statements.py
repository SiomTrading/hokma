"""Broker statements in PDF (SPEC-018: SKL-014). The PDFs of the fixture are built here, byte by byte, with no PDF library."""
import inspect
from decimal import Decimal

import pytest

from hokma.backoffice import recon, statements
from hokma.backoffice.recon import Statement
from hokma.backoffice.statements import StatementError

D = Decimal


def make_pdf(lines):
    """A one page PDF whose text is `lines`; with no lines the page holds only a drawn rectangle (what a scan looks like to a text reader)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    if lines:
        stream = "BT /F1 10 Tf 50 750 Td 14 TL " + " ".join(f"({esc(x)}) Tj T*" for x in lines) + " ET"
    else:
        stream = "0 0 100 100 re f"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
               f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


STATEMENT = ["Account statement March 2026", "Date Ref Type Symbol Qty Amount", "2026-03-02 F1 fill FUT:NQ:2026-12 2 -840000", "2026-03-03 F2 fill EQ:SPY 10 -6000.20",
             "2026-03-05 D1 dividend EQ:SPY 0 12.50", "Closing value 1005005.00"]


def test_text_and_tables_are_read_through_the_document_reader():
    r = statements.read_statement_pdf(make_pdf(STATEMENT))
    assert r.ok and r.code is None and "Account statement March 2026" in r.text and "Closing value 1005005.00" in r.text
    assert len(r.tables) == 1
    table = r.tables[0]
    assert table.header == ("date", "ref", "type", "symbol", "qty", "amount") and len(table.rows) == 3
    assert table.rows[0] == ("2026-03-02", "F1", "fill", "FUT:NQ:2026-12", "2", "-840000")
    assert table.records()[1] == {"date": "2026-03-03", "ref": "F2", "kind": "fill", "contract_id": "EQ:SPY", "qty": "10", "amount": "-6000.20"}


def test_the_rows_of_the_table_reconcile_against_the_book():
    from hokma.backoffice.book import BookEntry

    table = statements.read_statement_pdf(make_pdf(STATEMENT)).tables[0]
    stmt = Statement.from_rows(statements.statement_rows(table), as_of="2026-03-31")
    book = [BookEntry(1, 1, 7, "fill", "FUT:NQ:2026-12", D(2), D(21000), D(20), D(-840000), "schwab", "F1", "2026-03-02 15:00:00"),
            BookEntry(2, 1, 7, "fill", "EQ:SPY", D(10), D("600.02"), D(1), D("-6000.20"), "schwab", "F2", "2026-03-03 15:00:00")]
    breaks, summary = recon.compare(book, stmt)
    assert [b.key for b in breaks] == ["dividend|EQ:SPY|D1"] and summary["matched"] == 2          # only the dividend is missing from the book


def test_a_scanned_pdf_gives_scanned_pdf_and_no_text():
    r = statements.read_statement_pdf(make_pdf([]))
    assert not r.ok and r.code == "scanned_pdf" and r.text == "" and r.pages == () and r.tables == ()


def test_a_damaged_file_gives_the_code_of_the_reader():
    r = statements.read_statement_pdf(b"%PDF-1.4\nthis is not a pdf at all")
    assert not r.ok and r.code == "pdf_damaged" and r.text == ""


def test_input_is_checked_and_there_is_no_path_to_give():
    for bad in (b"", b"not a pdf", None, "a/path.pdf"):
        with pytest.raises(StatementError):
            statements.read_statement_pdf(bad)       # type: ignore[arg-type]
    with pytest.raises(StatementError):
        statements.read_statement_pdf(b"%PDF" + b"0" * statements.MAX_BYTES)
    assert list(inspect.signature(statements.read_statement_pdf).parameters) == ["data", "name"]


def test_the_pdf_library_is_not_imported_here():
    source = inspect.getsource(statements)
    assert "pypdf" not in source and "import PyPDF" not in source and "from hokma.docs import readers" in source


def test_text_that_is_not_a_table_makes_no_table():
    assert statements.parse_tables("hello world\nDate Symbol\n2026-03-01 A") == ()
    two = statements.parse_tables("Date Ref Type Symbol Qty Amount\n2026-03-02 F1 fill EQ:SPY 1 -5\n\nDate Ref Type Amount\n2026-03-03 F2 fee -1")
    assert len(two) == 2 and two[1].header == ("date", "ref", "type", "amount")
