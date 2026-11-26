"""Statements of every broker and the student's own CSV, read into one book schema (SPEC-018: FT-104, FT-107).

The column mapping of each broker is in `mapping.yaml`. A statement is read whole before anything is written: one bad row refuses the file
and says which row and which column, so the book never holds half of a statement. The multiplier of a row comes from the contract sheet
(a multiplier column in the statement is only checked against it), the currency must be USD (conversion is off, see the mapping),
and a row that was imported before is recognised by its reference (or by its content) and is not written twice."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from hokma.backoffice import desk_owner
from hokma.backoffice import book as book_module
from hokma.backoffice import provenance
from hokma.contracts import identity
from hokma.db import Database, get_database
from hokma.exec.money import ZERO, MoneyError, dec, text

MAPPING_PATH = Path(__file__).with_name("mapping.yaml")
MAX_BYTES = 5_000_000
MAX_ROWS = 50_000
MAX_CELL = 400
MAX_ERRORS = 50
KINDS_WITH_AMOUNT = ("deposit", "withdrawal", "dividend", "interest", "fee")
_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%Y%m%d;%H%M%S",
                 "%Y%m%d %H%M%S", "%Y%m%d", "%Y-%m-%d, %H:%M:%S", "%Y%m%d;%H%M")
_SCHWAB_OPTION = re.compile(r"^([A-Z][A-Z0-9.]{0,9})\s+(\d{2})/(\d{2})/(\d{4})\s+([0-9]+(?:\.[0-9]+)?)\s+([CP])$")


class StatementError(ValueError):
    """A statement cannot be read. `errors` has one readable line per problem, each naming the row and the column."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors[:MAX_ERRORS]
        extra = f" (and {len(errors) - MAX_ERRORS} more)" if len(errors) > MAX_ERRORS else ""
        super().__init__("; ".join(self.errors) + extra)


@dataclass(frozen=True)
class BookRow:
    """One normalized line of the book. `qty` is signed (a buy is positive). `amount` is the cash the line moved (a buy is negative)."""
    kind: str
    occurred_at: str
    contract_id: str | None
    qty: Decimal | None
    price: Decimal | None
    multiplier: Decimal | None
    amount: Decimal
    ref: str
    note: str | None = None
    row_number: int = 0


@dataclass
class ImportReport:
    broker: str
    rows: int = 0
    inserted: int = 0
    duplicates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"broker": self.broker, "rows": self.rows, "inserted": self.inserted, "duplicates": list(self.duplicates)}


@lru_cache(maxsize=1)
def load_mapping() -> dict[str, Any]:
    """The mapping of `mapping.yaml`. The conversion of currency is OFF: a file that turns it on is refused, not obeyed."""
    data = yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("brokers"), dict):
        raise StatementError(["mapping.yaml is not a version 1 mapping"])
    currency = data.get("currency") or {}
    if currency.get("convert") is not False or list(currency.get("accepted") or []) != ["USD"]:
        raise StatementError(["mapping.yaml: currency conversion must stay off and the book accepts USD only"])
    return data


def brokers() -> tuple[str, ...]:
    return tuple(load_mapping()["brokers"])


def _norm(header: str) -> str:
    return " ".join(str(header).replace("\ufeff", "").strip().lower().split())


def _money(value: str, what: str) -> Decimal | None:
    """A number as a statement writes it: `$1,234.50`, `(12.00)`, `-$5`. Empty is None."""
    raw = value.strip()
    if raw in ("", "--", "N/A", "n/a"):
        return None
    negative = raw.startswith("(") and raw.endswith(")") or raw.startswith("-") or raw.startswith("$-") or raw.startswith("-$")
    cleaned = re.sub(r"[()$,\s+-]", "", raw)
    try:
        number = dec(cleaned, what)
    except MoneyError as exc:
        raise ValueError(f"{what} {raw[:30]!r} is not a number") from exc
    return -number if negative else number


def _moment(value: str) -> str:
    raw = value.strip().split(" as of ")[0].strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"date {raw[:30]!r} is not a date this importer reads") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _canonical(symbol: str, style: str | None) -> tuple[str, Decimal]:
    raw = symbol.strip().upper()
    if style == "schwab":
        match = _SCHWAB_OPTION.match(" ".join(raw.split()))
        if match:
            root, month, day, year, strike, right = match.groups()
            raw = f"OPT:{root}:{year}-{month}-{day}:{right}:{format(Decimal(strike).normalize(), 'f')}"
    try:
        sheet = identity.resolve(identity.canonical_id(identity.parse_symbol(raw)))
    except identity.NoContract as exc:
        raise ValueError(f"symbol {symbol[:40]!r} has no contract sheet ({exc.detail or 'unknown'})") from exc
    return sheet.contract_id, sheet.multiplier


def _read_table(content: str | bytes) -> tuple[list[str], list[dict[str, str]]]:
    raw = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    if len(raw) > MAX_BYTES:
        raise StatementError([f"the statement is larger than {MAX_BYTES} characters"])
    reader = csv.reader(io.StringIO(raw))
    try:
        header = next(reader)
    except StopIteration:
        raise StatementError(["the statement is empty: it needs a header row"]) from None
    keys = [_norm(h) for h in header]
    rows: list[dict[str, str]] = []
    for number, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        if len(rows) >= MAX_ROWS:
            raise StatementError([f"the statement has more than {MAX_ROWS} rows"])
        rows.append({"__row__": str(number), **{k: (cells[i] if i < len(cells) else "") for i, k in enumerate(keys)}})
    return keys, rows


def _columns(spec: dict[str, Any], header: list[str]) -> dict[str, list[str]]:
    """For each canonical column, the headers of the statement that carry it, in the order of the mapping."""
    found = {canonical: [h for h in (_norm(a) for a in aliases) if h in header] for canonical, aliases in spec["columns"].items()}
    missing = [c for c in spec.get("required", []) if not found.get(c)]
    if missing:
        raise StatementError([f"missing required column '{c}' (looked for: {', '.join(spec['columns'][c])})" for c in missing])
    return found


def _kind_of(word: str, spec: dict[str, Any]) -> str | None:
    wanted = _norm(word)
    return next((kind for kind, words in spec["actions"].items() if wanted in [_norm(w) for w in words]), None)


def parse(broker: str, content: str | bytes) -> list[BookRow]:
    """The lines of a statement in the book schema, in file order. Raises StatementError naming every row and column that is wrong."""
    return _parse(broker, content)[0]


def _parse(broker: str, content: str | bytes) -> tuple[list[BookRow], list[str]]:
    spec = load_mapping()["brokers"].get(broker)
    if spec is None:
        raise StatementError([f"unknown broker {broker!r}; known: {', '.join(brokers())}"])
    header, rows = _read_table(content)
    cols = _columns(spec, header)
    source = spec["source"]
    errors: list[str] = []
    duplicates: list[str] = []
    out: list[BookRow] = []
    seen: dict[str, str] = {}
    for row in rows:
        number = int(row["__row__"])

        def cell(name: str) -> str:
            """The first non-empty value among the headers that carry this column (a statement may split a kind over two of them)."""
            value = next((row[h].strip() for h in cols.get(name, []) if row.get(h, "").strip()), "")
            if len(value) > MAX_CELL:
                raise ValueError(f"column '{name}' is longer than {MAX_CELL} characters")
            return value

        try:
            lines = _row(spec, cell, number, source)
            for line in lines:
                key = f"{line.kind}|{line.ref}"
                if key in seen:
                    if seen[key] != _fingerprint(line):
                        raise ValueError(f"reference {line.ref!r} is used twice with different content")
                    duplicates.append(f"row {number}: {line.kind} {line.ref} repeats an earlier row of this file; it is read once (add a reference column to keep two identical trades)")
                    continue
                seen[key] = _fingerprint(line)
                out.append(line)
        except ValueError as exc:
            errors.append(f"row {number}: {exc}")
    if errors:
        raise StatementError(errors)
    return out, duplicates


def _fingerprint(line: BookRow) -> str:
    return "|".join(str(x) for x in (line.kind, line.occurred_at, line.contract_id, text(line.qty), text(line.price), text(line.amount)))


def _row(spec: dict[str, Any], cell: Any, number: int, source: str) -> list[BookRow]:
    currency = cell("currency").upper()
    if currency and currency not in load_mapping()["currency"]["accepted"]:
        raise ValueError(f"column 'currency' is {currency[:8]!r}: conversion is off, the book holds USD only")
    kind = _kind_of(cell("type"), spec)
    if kind is None:
        raise ValueError(f"column 'type' has {cell('type')[:30]!r}, which this broker mapping does not know")
    moment = _moment(cell("date"))
    reference, note = cell("reference"), cell("note")[:400] or None
    commission = _money(cell("commission"), "column 'commission'") if cell("commission") else None
    if commission is not None and spec.get("commission_sign") != "negative" and commission < 0:
        raise ValueError("column 'commission' is negative: write what was paid as a positive number")
    commission = abs(commission) if commission is not None else ZERO
    if kind in ("buy", "sell"):
        symbol = cell("symbol")
        if not symbol:
            raise ValueError("column 'symbol' is required for a trade")
        quantity, price = _money(cell("quantity"), "column 'quantity'"), _money(cell("price"), "column 'price'")
        if quantity is None or price is None:
            raise ValueError("columns 'quantity' and 'price' are required for a trade")
        if quantity < 0 and not spec.get("signed_quantity"):
            raise ValueError("column 'quantity' is negative: the type says buy or sell, write the quantity as a positive number")
        quantity = abs(quantity)
        if quantity == 0 or price <= 0:
            raise ValueError("a trade needs a quantity above zero and a price above zero")
        contract_id, multiplier = _canonical(symbol, spec.get("symbol_style"))
        stated = cell("multiplier")
        if stated and _money(stated, "column 'multiplier'") != multiplier:
            raise ValueError(f"column 'multiplier' is {stated} but the contract sheet of {contract_id} says {text(multiplier)}")
        signed = quantity if kind == "buy" else -quantity
        cash = quantity * price * multiplier
        ref = _reference(source, reference, [moment, kind, contract_id, text(quantity), text(price)], number)
        lines = [BookRow("fill", moment, contract_id, signed, price, multiplier, -cash if kind == "buy" else cash, ref, note, number)]
        if commission:
            lines.append(BookRow("commission", moment, contract_id, None, None, None, -commission, ref, note, number))
        return lines
    amount = _money(cell("amount"), "column 'amount'")
    if amount is None:
        raise ValueError(f"column 'amount' is required for a {kind}")
    if kind in ("deposit", "dividend") and amount < 0:
        raise ValueError(f"column 'amount' is negative for a {kind}: write it as a positive number")
    if kind == "withdrawal" or kind == "fee":
        amount = -abs(amount)
    contract_id = _canonical(cell("symbol"), spec.get("symbol_style"))[0] if cell("symbol") and kind == "dividend" else None
    ref = _reference(source, reference, [moment, kind, contract_id or "", text(amount)], number)
    return [BookRow(kind, moment, contract_id, None, None, None, amount, ref, note, number)]


def _reference(source: str, reference: str, parts: list[str], number: int) -> str:
    if reference:
        if len(reference) > 100:
            raise ValueError("column 'reference' is longer than 100 characters")
        return f"{source}:{reference}"
    return f"{source}:r" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def import_into(account_id: int, broker: str, content: str | bytes, *, desk_id: int | None = None, db: Database | None = None) -> ImportReport:
    """Write a statement into the book of an account, all or nothing. A line already in the book (same source, reference and kind) is a
    duplicate: it is not written again and the report says so."""
    lines, repeated = _parse(broker, content)
    source = load_mapping()["brokers"][broker]["source"]
    if source not in provenance.SOURCES:
        raise StatementError([f"the source {source!r} of {broker} is not a source of the book"])
    report = ImportReport(broker, rows=len({line.row_number for line in lines}) + len(repeated), duplicates=list(repeated))
    with (db or get_database()).write() as u:
        u.broker.get_account(account_id)
        owner = desk_id if desk_id is not None else desk_owner.sole_desk(u.session, account_id)
        for line in lines:
            made = book_module.append_in(u.session, account_id, kind=line.kind, amount=line.amount, source=source, source_ref=line.ref,
                                         occurred_at=line.occurred_at, desk_id=owner, contract_id=line.contract_id, qty=line.qty, price=line.price,
                                         multiplier=line.multiplier, note=line.note)
            if made is None:
                report.duplicates.append(f"row {line.row_number}: {line.kind} {line.ref}")
            else:
                report.inserted += 1
    return report
