"""Shared pieces of the back office tests (SPEC-018, B8): the mixed book fixture loaded into the book of a simulated account."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from exec_support import Ready, db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma.backoffice import importers

FIXTURES = Path(__file__).parent / "backoffice" / "fixtures"
MIXED = FIXTURES / "book_mixed.csv"
AAPL, SPY, OPT, NQ = "EQ:AAPL", "ETF:SPY", "OPT:SPY:2026-12-18:C:600", "FUT:NQ:2026-12"
D = Decimal


def load_mixed(r: Ready) -> importers.ImportReport:
    """The fourteen rows of `book_mixed.csv` written into the book of the simulated account of the desk."""
    return importers.import_into(r.account_id, "student", MIXED.read_text(encoding="utf-8"), db=r.db)
