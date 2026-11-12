"""PDF, xlsx and xls readers (SPEC-009 P3): sections, scanned PDFs, protected and damaged files, limits."""
import datetime
from pathlib import Path

import pytest
from docs_support import make_encrypted_pdf, make_pdf, make_xlsx

from hokma.docs import NO_TEXT_NOTICE, UnreadableDocument, detect_format, read_document, supported_extensions

FIX = Path(__file__).parent / "fixtures" / "docs"


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def sections(result):
    return [s for s, _ in result.passages]


# ------------------------------------------------------------------------------------------------------- pdf

def test_formats_are_registered():
    assert {".pdf", ".xlsx", ".xlsm", ".xls"} <= supported_extensions()
    assert [detect_format(n) for n in ("a.PDF", "a.xlsm", "a.xls")] == ["pdf", "xlsx", "xls"]


def test_pdf_sections_are_pages():
    result = read_document(FIX / "sample.pdf")
    assert result.format == "pdf"
    assert sections(result) == ["page 1", "page 2", "page 3"]
    assert result.passages[0][1] == "Gamma exposure overview"
    assert result.detail is None


def test_pdf_pages_without_text_are_skipped_but_keep_their_number(tmp_path):
    path = write(tmp_path, "mixed.pdf", make_pdf(["first", None, "third"]))
    assert sections(read_document(path)) == ["page 1", "page 3"]


def test_scanned_pdf_is_ready_with_a_notice_and_is_found_by_name(tmp_path):
    path = write(tmp_path, "Scanned Report.pdf", make_pdf([None, "", None]))
    result = read_document(path)
    assert result.notice == NO_TEXT_NOTICE == result.detail
    assert result.passages == [("document", "Scanned Report")]
    assert result.truncated is None


def test_pdf_with_text_has_no_notice():
    assert read_document(FIX / "sample.pdf").notice is None


def test_pdf_page_limit_keeps_what_was_read(tmp_path):
    path = write(tmp_path, "long.pdf", make_pdf([f"page text {n}" for n in range(1, 8)]))
    result = read_document(path, max_pages=3)
    assert sections(result) == ["page 1", "page 2", "page 3"]
    assert result.truncated.kind == "pages" and result.truncated.reached == 3


def test_pdf_empty_pages_count_toward_the_page_limit(tmp_path):
    path = write(tmp_path, "blank.pdf", make_pdf([None] * 6))
    result = read_document(path, max_pages=2)
    assert result.truncated.kind == "pages"
    assert result.notice is None  # truncated documents do not get the "no text" notice


def test_pdf_with_user_password_is_unreadable(tmp_path):
    path = write(tmp_path, "locked.pdf", make_encrypted_pdf(["secret"], "pw"))
    with pytest.raises(UnreadableDocument, match="password"):
        read_document(path)


@pytest.mark.parametrize("data", [b"", b"not a pdf at all", b"%PDF-1.4\n garbage without objects", b"\x00" * 64])
def test_pdf_corrupt_is_unreadable(tmp_path, data):
    with pytest.raises(UnreadableDocument):
        read_document(write(tmp_path, "bad.pdf", data))


def test_pdf_truncated_file_is_unreadable_or_partial_but_never_crashes(tmp_path):
    data = make_pdf(["alpha", "beta"])
    path = write(tmp_path, "cut.pdf", data[: len(data) // 2])
    try:
        result = read_document(path)
    except UnreadableDocument:
        return
    assert result.format == "pdf"


# ------------------------------------------------------------------------------------------------------ xlsx

def test_xlsx_sheets_blocks_and_values():
    result = read_document(FIX / "sample.xlsx")
    assert result.format == "xlsx"
    assert sections(result) == ['sheet "Ventas", rows 2-3', 'sheet "Notes", rows 2-2']
    assert result.passages[0][1] == "Item | Qty | Day | Active\nalpha | 3 | 2026-01-05 | TRUE\nbeta | 4.5 | 2026-02-06 | FALSE"
    assert result.passages[1][1] == "Topic\ngamma exposure"


def test_xlsx_repeats_header_in_every_block_of_fifty_rows(tmp_path):
    rows = [("id", "name")] + [(n, f"row{n}") for n in range(1, 121)]
    result = read_document(write(tmp_path, "big.xlsx", make_xlsx({"Data": rows})))
    assert sections(result) == ['sheet "Data", rows 2-51', 'sheet "Data", rows 52-101', 'sheet "Data", rows 102-121']
    assert all(text.startswith("id | name\n") for _, text in result.passages)


def test_xlsx_skips_blank_rows_and_trailing_empty_cells_and_keeps_real_row_numbers(tmp_path):
    rows = [("h1", "h2", None), (None, None, None), ("a", None, None), ("b", "c", None)]
    result = read_document(write(tmp_path, "gaps.xlsx", make_xlsx({"S": rows})))
    assert result.passages == [('sheet "S", rows 3-4', "h1 | h2\na\nb | c")]


def test_xlsx_header_only_and_empty_sheet(tmp_path):
    result = read_document(write(tmp_path, "h.xlsx", make_xlsx({"Only": [("a", "b")], "Nothing": []})))
    assert result.passages == [('sheet "Only", rows 1-1', "a | b")]


def test_xlsx_dates_numbers_and_booleans_read_as_text(tmp_path):
    rows = [("when", "n", "ok"), (datetime.datetime(2026, 3, 4, 10, 30), 7.0, False), (datetime.date(2026, 3, 5), 1e21, True)]
    text = read_document(write(tmp_path, "t.xlsx", make_xlsx({"S": rows}))).passages[0][1].splitlines()
    assert text[1] == "2026-03-04 10:30:00 | 7 | FALSE"
    assert text[2].startswith("2026-03-05 | 1e+21") and text[2].endswith("TRUE")


def test_xlsx_reads_cached_values_not_formulas(tmp_path):
    from io import BytesIO

    from openpyxl import Workbook
    book = Workbook()
    book.active.append(["a"])
    book.active.append(["=1+1"])
    out = BytesIO()
    book.save(out)
    # No application ever calculated the formula, so data_only gives no value: the formula text must not leak.
    result = read_document(write(tmp_path, "f.xlsx", out.getvalue()))
    assert "=1+1" not in result.passages[0][1]


def test_xlsm_is_read_like_xlsx(tmp_path):
    result = read_document(write(tmp_path, "m.xlsm", make_xlsx({"S": [("a",), ("b",)]})))
    assert result.format == "xlsx" and result.passages == [('sheet "S", rows 2-2', "a\nb")]


def test_xlsx_passage_limit(tmp_path):
    rows = [("h",)] + [("x" * 40,)] * 200
    result = read_document(write(tmp_path, "l.xlsx", make_xlsx({"S": rows})), max_passages=2, target=100)
    assert len(result.passages) == 2 and result.truncated.kind == "passages"


def test_xlsx_time_limit_with_a_fake_clock(tmp_path):
    rows = [("h",)] + [(n,) for n in range(1, 200)]
    ticks = iter(range(0, 1000))
    result = read_document(write(tmp_path, "c.xlsx", make_xlsx({"S": rows})), seconds=1.5, clock=lambda: next(ticks))
    assert result.truncated.kind == "time"


@pytest.mark.parametrize("data", [b"", b"plain text", b"PK\x03\x04 broken", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 encrypted container"])
def test_xlsx_corrupt_or_protected_is_unreadable(tmp_path, data):
    with pytest.raises(UnreadableDocument):
        read_document(write(tmp_path, "bad.xlsx", data))


def test_xlsx_password_container_says_password(tmp_path):
    with pytest.raises(UnreadableDocument, match="password"):
        read_document(write(tmp_path, "p.xlsx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100))


def test_xlsx_zip_that_is_not_a_workbook_is_unreadable(tmp_path):
    import io
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(UnreadableDocument):
        read_document(write(tmp_path, "z.xlsx", out.getvalue()))


def _rewrite(data, member, new):
    import io
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info.filename, new(src.read(info.filename)) if info.filename == member else src.read(info.filename))
    return out.getvalue()


def _with_doctype(raw):
    decl = b'<!DOCTYPE x [<!ENTITY e "boom">]>'
    if raw.startswith(b"<?xml"):
        end = raw.index(b"?>") + 2
        return raw[:end] + decl + raw[end:]
    return decl + raw


def test_xlsx_with_entity_declarations_is_refused(tmp_path):
    data = make_xlsx({"S": [("a",)]})
    hostile = _rewrite(data, "xl/workbook.xml", _with_doctype)
    with pytest.raises(UnreadableDocument, match="unsafe"):
        read_document(write(tmp_path, "h.xlsx", hostile))


def test_xlsx_with_an_oversized_part_is_refused(tmp_path, monkeypatch):
    from hokma.docs import readers
    monkeypatch.setattr(readers, "MAX_ZIP_MEMBER_BYTES", 10)
    with pytest.raises(UnreadableDocument, match="too large"):
        read_document(write(tmp_path, "o.xlsx", make_xlsx({"S": [("a",)]})))


# -------------------------------------------------------------------------------------------------------- xls

def test_xls_matches_xlsx_sections_and_values():
    result = read_document(FIX / "sample.xls")
    assert result.format == "xls"
    assert sections(result) == ['sheet "Ventas", rows 2-3', 'sheet "Notes", rows 2-2']
    assert result.passages[0][1] == "Item | Qty | Day | Active\nalpha | 3 | 2026-01-05 | TRUE\nbeta | 4.5 | 2026-02-06 | FALSE"
    assert read_document(FIX / "sample.xls").passages == read_document(FIX / "sample.xlsx").passages


@pytest.mark.parametrize("data", [b"", b"just text", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600])
def test_xls_corrupt_is_unreadable(tmp_path, data):
    with pytest.raises(UnreadableDocument):
        read_document(write(tmp_path, "bad.xls", data))


def test_xlsx_content_with_xls_name_is_unreadable_not_a_crash(tmp_path):
    with pytest.raises(UnreadableDocument):
        read_document(write(tmp_path, "wrong.xls", make_xlsx({"S": [("a",)]})))


def test_xls_passage_limit(tmp_path):
    result = read_document(FIX / "sample.xls", max_passages=1)
    assert len(result.passages) == 1 and result.truncated.kind == "passages"


# ------------------------------------------------------------------------------------------- pinned dependencies

def test_reader_dependencies_are_pinned_in_pyproject_and_locked():
    import tomllib
    root = Path(__file__).resolve().parents[2]
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    locked = {p["name"]: p["version"] for p in tomllib.loads((root / "uv.lock").read_text())["package"]}
    for name in ("pypdf", "openpyxl", "xlrd"):
        pins = [d for d in declared if d.startswith(name + "==")]
        assert len(pins) == 1, name
        assert locked[name] == pins[0].split("==")[1]
    assert "et-xmlfile" in locked  # openpyxl's only dependency


def test_blank_rows_cannot_defeat_the_time_limit(tmp_path):
    rows = [("h",)] + [(None,)] * 3000 + [("tail",)]
    ticks = iter(range(0, 100000))
    path = write(tmp_path, "blank.xlsx", make_xlsx({"S": rows}))
    result = read_document(path, seconds=3, clock=lambda: next(ticks))
    assert result.truncated is not None and result.truncated.kind == "time"
    assert result.passages == []
