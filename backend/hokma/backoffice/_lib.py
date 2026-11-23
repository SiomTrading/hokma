"""The only file of the back office that imports the portfolio functions of the fundamentals library and the spreadsheet library (SPEC-018:
CC-05, rule 4). Every other file of `hokma.backoffice` reaches them through here; a scan test fails if any other file imports them.

`portfolio_model` is the portfolio calculation module of the library: the back office keeps its own book (`book.py`) and uses the library only
to cross-check returns. `write_xlsx` and `read_xlsx` are the spreadsheet door of `exports.py` and `statements.py`."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
from financetoolkit.portfolio import portfolio_model

__all__ = ["portfolio_model", "read_xlsx", "write_xlsx"]


def write_xlsx(path: Path, sheets: dict[str, list[list[Any]]]) -> Path:
    """Create a workbook with one sheet per key; each sheet is a list of rows. Text, numbers and None are written as they are. Only creates."""
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(title=str(name)[:31])
        for row in rows:
            sheet.append([_cell(v) for v in row])
    workbook.save(path)
    return path


def read_xlsx(path: Path) -> dict[str, list[list[Any]]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return {sheet.title: [list(row) for row in sheet.iter_rows(values_only=True)] for sheet in workbook.worksheets}
    finally:
        workbook.close()


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str)):
        return value
    return str(value)          # Decimal and dates go in as their exact text
