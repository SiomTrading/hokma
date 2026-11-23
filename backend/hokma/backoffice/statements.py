"""Broker statements in PDF (SPEC-018: SKL-014). Reads the text and the tables of a statement through the document reader that Hokma already has
(`hokma.docs.readers`): the PDF library is not imported here. Read only: no forms, no decryption.

A PDF with no selectable text (a scan) comes back with the code `scanned_pdf` and no text at all; the reader never makes text up and an image is never
guessed at. A protected, encrypted or damaged file comes back with the reader's own code. The bytes are the input, never a path chosen by an agent: the file is
written to a private temporary folder for the reader and removed right after.
"""
from __future__ import annotations

import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from hokma.docs import readers

MAX_BYTES = 25 * 1024 * 1024
MAX_TABLE_ROWS = 20_000
COLUMN_WORDS = {"date", "symbol", "contract", "contract_id", "qty", "quantity", "price", "amount", "ref", "reference", "type", "kind", "currency", "description"}
ALIASES = {"quantity": "qty", "reference": "ref", "type": "kind", "symbol": "contract_id", "contract": "contract_id"}
_TOKEN = re.compile(r"\S+")


class StatementError(ValueError):
    """The input is not a statement the module will read (too large, not a PDF)."""

    code = "bad_input"


@dataclass(frozen=True)
class Table:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    def records(self) -> list[dict[str, str]]:
        names = [ALIASES.get(h, h) for h in self.header]
        return [dict(zip(names, row)) for row in self.rows]


@dataclass(frozen=True)
class StatementRead:
    ok: bool
    code: str | None                    # None when ok; scanned_pdf, pdf_password, pdf_encrypted, pdf_damaged, ...
    text: str = ""
    pages: tuple[str, ...] = field(default_factory=tuple)
    tables: tuple[Table, ...] = field(default_factory=tuple)
    truncated: str | None = None


def parse_tables(text: str) -> tuple[Table, ...]:
    """Tables of plain text: a header line that holds at least three known column words, and the lines under it with the same number of cells.
    Cells are split on white space, so a cell has no spaces (a symbol, a date, a number). A blank line or a line of another width ends the table."""
    tables: list[Table] = []
    header: tuple[str, ...] | None = None
    rows: list[tuple[str, ...]] = []

    def close() -> None:
        nonlocal header, rows
        if header is not None and rows:
            tables.append(Table(header, tuple(rows)))
        header, rows = None, []

    for line in text.splitlines():
        tokens = tuple(_TOKEN.findall(line))
        lowered = tuple(t.lower() for t in tokens)
        if len(tokens) >= 3 and sum(1 for t in lowered if t in COLUMN_WORDS) >= 3 and all(t in COLUMN_WORDS for t in lowered):
            close()
            header = lowered
        elif header is not None and len(tokens) == len(header):
            if len(rows) >= MAX_TABLE_ROWS:
                raise StatementError("a table has too many rows")
            rows.append(tokens)
        else:
            close()
    close()
    return tuple(tables)


def read_statement_pdf(data: bytes, *, name: str = "statement.pdf") -> StatementRead:
    """The text and the tables of a PDF statement given as bytes."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise StatementError("give the bytes of a PDF")
    if len(data) > MAX_BYTES:
        raise StatementError("the statement is larger than the limit")
    if not bytes(data[:1024]).lstrip().startswith(b"%PDF"):
        raise StatementError("the file is not a PDF")
    with tempfile.TemporaryDirectory(prefix="hokma-statement-") as folder:
        path = Path(folder) / "statement.pdf"
        path.write_bytes(bytes(data))
        try:
            result = readers.read_document(path)
        except readers.UnreadableDocument as exc:
            return StatementRead(False, exc.code)
    if result.notice == readers.NO_TEXT_NOTICE:
        return StatementRead(False, "scanned_pdf")
    pages: dict[str, list[str]] = {}
    for section, passage in result.passages:
        pages.setdefault(section, []).append(passage)
    page_texts = tuple("\n".join(parts) for parts in pages.values())
    full = "\n".join(page_texts)
    return StatementRead(True, None, full, page_texts, parse_tables(full), result.truncated.detail if result.truncated else None)


def statement_rows(table: Table, *, default_kind: str | None = None) -> list[Mapping[str, str]]:
    """The rows of a table as dicts ready for `recon.Statement.from_rows`. A row that lacks its kind takes `default_kind`; nothing else is filled in."""
    out = []
    for record in table.records():
        if "kind" not in record and default_kind:
            record["kind"] = default_kind
        out.append(record)
    return out


__all__ = ["StatementError", "StatementRead", "Table", "parse_tables", "read_statement_pdf", "statement_rows"]
