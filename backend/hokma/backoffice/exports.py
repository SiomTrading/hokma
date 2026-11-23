"""Spreadsheet exports of the book and of a reconciliation (SPEC-018: SKL-015), in xlsx and CSV. Create and read only: an export always makes a new
file and refuses to touch one that exists, so no sheet of an earlier file is ever restructured. The spreadsheet library is reached through
`backoffice/_lib.py`. The folder is given by the code that calls (never by an agent) and the file name is built here from the account or the run.

Numbers are written as their exact text (a Decimal never goes through a float), so opening the file again gives the same value cell by cell. A text
cell that a spreadsheet would read as a formula gets a leading apostrophe.
"""
from __future__ import annotations

import csv
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from hokma.backoffice import _lib, book as book_module
from hokma.backoffice import recon as recon_module
from hokma.db import Database, get_database
from hokma.exec.money import text

BOOK_HEAD = ["id", "occurred_at", "kind", "contract_id", "qty", "price", "multiplier", "amount", "source", "source_ref", "desk_id", "note"]
BREAK_HEAD = ["id", "bucket", "key", "cause", "diff", "impact", "action", "owner", "due_at", "explanation"]
FORMATS = ("xlsx", "csv")
MAX_ROWS = 1_000_000
_FORMULA_START = ("=", "+", "@", "\t", "\r")


class ExportError(ValueError):
    code = "bad_input"


def safe_cell(value: Any) -> Any:
    """The value of one cell: None stays empty, a Decimal is its exact text, and a text that would run as a formula is prefixed with an apostrophe."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return text(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return value
    s = str(value)
    if s.startswith(_FORMULA_START) or (s.startswith("-") and not _is_number(s)):
        return "'" + s
    return s


def _is_number(s: str) -> bool:
    try:
        Decimal(s)
    except Exception:
        return False
    return True


def _check_stem(stem: str) -> str:
    if not stem or len(stem) > 80 or not all(c.isalnum() or c in "-_" for c in stem):
        raise ExportError("the file name is letters, digits, hyphen and underscore")
    return stem


def write_table(folder: Path, stem: str, sheets: dict[str, list[list[Any]]], formats: Sequence[str] = FORMATS) -> dict[str, Path]:
    """Write the sheets as one xlsx (a sheet each) and as one CSV per sheet. Existing files are never changed or replaced."""
    stem = _check_stem(stem)
    unknown = [f for f in formats if f not in FORMATS]
    if unknown:
        raise ExportError(f"unknown format {unknown[0]}")
    if sum(len(rows) for rows in sheets.values()) > MAX_ROWS:
        raise ExportError("too many rows for one export")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    clean = {name: [[safe_cell(v) for v in row] for row in rows] for name, rows in sheets.items()}
    targets: dict[str, Path] = {}
    if "xlsx" in formats:
        targets["xlsx"] = folder / f"{stem}.xlsx"
    if "csv" in formats:
        for name in clean:
            targets[f"csv:{name}"] = folder / f"{stem}-{_check_stem(name)}.csv"
    existing = [p.name for p in targets.values() if p.exists()]
    if existing:
        raise ExportError(f"{existing[0]} already exists: an export never replaces a file")
    if "xlsx" in targets:
        _lib.write_xlsx(targets["xlsx"], clean)
    for key, path in targets.items():
        if key.startswith("csv:"):
            with path.open("x", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerows([["" if v is None else v for v in row] for row in clean[key[4:]]])
    return targets


def read_back(path: Path) -> dict[str, list[list[Any]]]:
    """Read an exported file again: an xlsx gives its sheets, a CSV gives one sheet named after the file."""
    path = Path(path)
    if path.suffix == ".xlsx":
        return _lib.read_xlsx(path)
    with path.open(newline="", encoding="utf-8") as handle:
        return {path.stem: [row for row in csv.reader(handle)]}


def book_sheet(entries: Sequence[book_module.BookEntry]) -> list[list[Any]]:
    return [BOOK_HEAD, *([e.id, e.occurred_at, e.kind, e.contract_id, e.qty, e.price, e.multiplier, e.amount, e.source, e.source_ref, e.desk_id, e.note] for e in entries)]  # type: ignore[list-item]


def breaks_sheet(breaks: Sequence[recon_module.Break]) -> list[list[Any]]:
    return [BREAK_HEAD, *([b.id, b.bucket, b.key, b.cause, b.diff, b.impact, b.action, b.owner, b.due_at, b.explanation] for b in breaks)]  # type: ignore[list-item]


def export_book(account_id: int, folder: Path, *, formats: Sequence[str] = FORMATS, db: Database | None = None) -> dict[str, Path]:
    items = book_module.entries(account_id, db=db or get_database(), since_reset=False)
    return write_table(folder, f"book-{int(account_id)}", {"book": book_sheet(items)}, formats)


def export_reconciliation(recon_id: int, folder: Path, *, formats: Sequence[str] = FORMATS, db: Database | None = None) -> dict[str, Path]:
    breaks = recon_module.load_breaks(int(recon_id), db=db)
    return write_table(folder, f"reconciliation-{int(recon_id)}", {"breaks": breaks_sheet(breaks)}, formats)


__all__ = ["ExportError", "book_sheet", "breaks_sheet", "export_book", "export_reconciliation", "read_back", "safe_cell", "write_table"]
