"""Every entry of the book points to its origin (SPEC-018: OA-37). `source` says where it came from (the simulated broker, a real one,
an imported statement, the owner) and `source_ref` is the id of the fill, of the statement line or of the adjustment. An entry with no
origin is not written, and the book is append only: nothing in this package updates or deletes a row of `book_entries`."""
from __future__ import annotations

SOURCES = ("sim", "schwab", "alpaca", "ibkr", "crypto", "import", "owner")
KINDS = ("fill", "commission", "fee", "deposit", "withdrawal", "dividend", "interest", "accrual", "adjustment")


class MissingProvenance(ValueError):
    """An entry came without its origin."""


def check(source: str | None, source_ref: str | None) -> tuple[str, str]:
    if source not in SOURCES:
        raise MissingProvenance(f"source must be one of {', '.join(SOURCES)}")
    if not isinstance(source_ref, str) or not 1 <= len(source_ref.strip()) <= 128:
        raise MissingProvenance("source_ref is required: the id of the fill, the statement line or the adjustment")
    return source, source_ref.strip()
