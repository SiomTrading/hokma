"""Reconciliation of the book against the statement of the broker (SPEC-018: AFS-001, AFS-002).

`reconcile` joins the lines of the book and the lines of the statement on a common key (kind, contract, reference) with a full outer join and puts
every difference in one bucket:

* `quantity`: the same line with another quantity (tolerance 0);
* `amount`: the same line with another amount (tolerance 0.01);
* `timing`: the same line on another day;
* `one_side`: a line that only one side has.

Every break carries one probable cause (timing, currency, mapping, duplicate_or_missing, commission or data_quality). The breaks come ordered by the
absolute money they put at stake. One more check is made when the statement carries the value of the account: the sum of the capital of the desks
that use the account against that value. Its difference is a break like any other.

`trace` explains one break in one sentence (who did what and why) with an owner, an expected date and an action (watch, adjust, ticket, suppress). It only
diagnoses: it receives a read-only reader of the book and has no way to write to it. `verify_break` is the critic: it checks again each break
against the book. Adjustments are never written here; an `adjust` action is a proposal for the owner.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol

from hokma.backoffice import book as book_module
from hokma.backoffice.book import BookEntry
from hokma.db import Database, get_database
from hokma.exec.money import ZERO, MoneyError, dec, dec_or_none, text

TOLERANCE_AMOUNT = Decimal("0.01")
TOLERANCE_QTY = ZERO
MATERIAL_AMOUNT = Decimal("1.00")
LATE_DAYS = 2                         # a line this close to the end of the statement may simply not be posted yet
MAX_LINES = 200_000
BUCKETS = ("quantity", "amount", "timing", "one_side")
CAUSES = ("timing", "currency", "mapping", "duplicate_or_missing", "commission", "data_quality")
ACTIONS = ("watch", "adjust", "ticket", "suppress")
COMPARED_KINDS = ("fill", "commission", "fee", "deposit", "withdrawal", "dividend", "interest")   # accruals and adjustments are internal: never on a statement
NAV_KEY = "nav|capital|account"


class ReconError(ValueError):
    """The statement cannot be reconciled (another account, no lines, too many lines)."""


@dataclass(frozen=True)
class StatementLine:
    ref: str
    kind: str
    contract_id: str | None
    qty: Decimal | None
    amount: Decimal
    date: str
    currency: str = "USD"
    price: Decimal | None = None

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> "StatementLine":
        try:
            ref, kind = str(row["ref"]).strip(), str(row["kind"]).strip().lower()
            if not ref or kind not in COMPARED_KINDS:
                raise ReconError(f"a statement line needs a reference and a kind from {', '.join(COMPARED_KINDS)}")
            contract = (row.get("contract_id") or None) and str(row["contract_id"]).strip()
            return cls(ref[:128], kind, contract, dec_or_none(row.get("qty") if row.get("qty") not in ("", None) else None, "qty"), dec(row["amount"], "amount"),
                       str(row["date"])[:10], str(row.get("currency") or "USD").upper(), dec_or_none(row.get("price") if row.get("price") not in ("", None) else None, "price"))
        except KeyError as exc:
            raise ReconError(f"a statement line is missing {exc.args[0]}") from exc
        except MoneyError as exc:
            raise ReconError(str(exc)) from exc


@dataclass(frozen=True)
class Statement:
    """What the broker says for a period. Amounts are cash effects with the sign of the book (a buy is negative); quantities are signed (a sell is
    negative). `account_value` is the value of the account at `as_of` when the statement has it."""
    as_of: str
    lines: tuple[StatementLine, ...]
    account_id: int | None = None
    period_start: str | None = None
    account_value: Decimal | None = None

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]], *, as_of: str, account_id: int | None = None, period_start: str | None = None,
                  account_value: Any = None) -> "Statement":
        lines = tuple(StatementLine.of(r) for r in rows)
        if len(lines) > MAX_LINES:
            raise ReconError("too many statement lines")
        return cls(str(as_of)[:10], lines, account_id, period_start and str(period_start)[:10], dec_or_none(account_value, "account_value"))


@dataclass(frozen=True)
class Line:
    ref: str
    kind: str
    contract_id: str | None
    qty: Decimal | None
    amount: Decimal
    date: str
    currency: str = "USD"
    price: Decimal | None = None
    multiplier: Decimal | None = None
    entry_id: int | None = None
    source: str | None = None
    desk_id: int | None = None

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.contract_id or '-'}|{self.ref}"

    def to_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "kind": self.kind, "contract_id": self.contract_id, "qty": text(self.qty), "amount": text(self.amount), "date": self.date,
                "currency": self.currency, "price": text(self.price), "multiplier": text(self.multiplier), "entry_id": self.entry_id, "source": self.source,
                "desk_id": self.desk_id}


def _from_entry(e: BookEntry) -> Line:
    return Line(e.source_ref, e.kind, e.contract_id, e.qty, e.amount, e.occurred_at[:10], "USD", e.price, e.multiplier, e.id, e.source, e.desk_id)


def _from_statement(s: StatementLine) -> Line:
    return Line(s.ref, s.kind, s.contract_id, s.qty, s.amount, s.date, s.currency, s.price)


@dataclass(frozen=True)
class Break:
    bucket: str
    key: str
    local: dict[str, Any] | None
    broker: dict[str, Any] | None
    diff: Decimal | None                # in the unit of the bucket: quantity, amount or days
    impact: Decimal                     # money at stake, never negative: the ordering key
    cause: str
    id: int | None = None
    explanation: str | None = None
    owner: str | None = None
    due_at: str | None = None
    action: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.bucket not in BUCKETS or self.cause not in CAUSES:
            raise ReconError("a break has a bucket and a cause from the lists of this module")

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Break":
        def load(v: Any) -> dict[str, Any] | None:
            return None if v in (None, "") else (v if isinstance(v, dict) else json.loads(v))

        local, broker = load(row.get("local_json")), load(row.get("broker_json"))
        diff = dec_or_none(row.get("diff"))
        impact = _impact_of(row["bucket"], local, broker, diff)
        return cls(row["bucket"], row["key"], local, broker, diff, impact, row.get("cause") or "data_quality", row.get("id"), row.get("explanation"),
                   row.get("owner"), None if row.get("due_at") is None else str(row["due_at"])[:10], row.get("action"))

    @property
    def is_nav(self) -> bool:
        return self.key.startswith(NAV_KEY)

    @property
    def material(self) -> bool:
        if self.bucket in ("quantity", "one_side", "timing") or self.is_nav:
            return True
        return self.bucket == "amount" and self.impact >= MATERIAL_AMOUNT

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "bucket": self.bucket, "key": self.key, "local": self.local, "broker": self.broker, "diff": text(self.diff), "impact": text(self.impact),
                "cause": self.cause, "explanation": self.explanation, "owner": self.owner, "due_at": self.due_at, "action": self.action}


def _impact_of(bucket: str, local: Mapping[str, Any] | None, broker: Mapping[str, Any] | None, diff: Decimal | None) -> Decimal:
    """Money at stake of a stored break, recomputed from its two sides (the table keeps the diff, not the impact)."""
    def d(side: Mapping[str, Any] | None, name: str) -> Decimal | None:
        return None if side is None or side.get(name) in (None, "") else Decimal(str(side[name]))

    amount_l, amount_b = d(local, "amount"), d(broker, "amount")
    if bucket == "timing":
        return ZERO
    if bucket == "amount":
        return abs(diff) if diff is not None else abs((amount_b or ZERO) - (amount_l or ZERO))
    if bucket == "quantity":
        qty = abs(diff) if diff is not None else ZERO
        price = d(local, "price") or d(broker, "price")
        mult = d(local, "multiplier") or d(broker, "multiplier") or Decimal(1)
        if price is not None:
            return qty * price * mult
        return abs((amount_b or ZERO) - (amount_l or ZERO)) or qty
    return abs(amount_l if amount_l is not None else (amount_b or ZERO))


@dataclass(frozen=True)
class Reconciliation:
    account_id: int
    as_of: str
    status: str
    summary: dict[str, Any]
    breaks: tuple[Break, ...]
    recon_id: int | None = None


def _qty_equal(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= TOLERANCE_QTY


def _mk(bucket: str, local: Line | None, broker: Line | None, cause: str, detail: str, *, key: str | None = None, repeat: bool = False) -> Break:
    l, b = local and local.to_dict(), broker and broker.to_dict()
    if repeat:                                   # the other side holds the same reference: this is the extra copy, not a line that is missing
        for side in (l, b):
            if side is not None:
                side["repeat"] = True
    if bucket == "quantity":
        diff = (broker.qty or ZERO) - (local.qty or ZERO)                              # type: ignore[union-attr]
    elif bucket == "amount":
        diff = broker.amount - local.amount                                            # type: ignore[union-attr]
    elif bucket == "timing":
        diff = Decimal((date.fromisoformat(broker.date) - date.fromisoformat(local.date)).days)   # type: ignore[union-attr]
    else:
        diff = None
    out = Break(bucket, key or (local or broker).key, l, b, diff, ZERO, cause, detail=detail)       # type: ignore[union-attr]
    return replace(out, impact=_impact_of(bucket, l, b, diff))


def _compare(l: Line, b: Line, commission_by_ref: Mapping[str, Decimal]) -> Break | None:
    if b.currency != "USD":
        return replace(_mk("amount", l, b, "currency", "the statement line is not in USD"), impact=abs(l.amount))
    if not _qty_equal(l.qty, b.qty):
        return _mk("quantity", l, b, "duplicate_or_missing", "the same line with another quantity: a partial fill or a repeated line")
    gap = b.amount - l.amount
    if abs(gap) > TOLERANCE_AMOUNT:
        if l.kind in ("commission", "fee"):
            return _mk("amount", l, b, "commission", "the charge differs from the one in the book")
        bundled = commission_by_ref.get(l.ref)
        if l.kind == "fill" and bundled is not None and abs(abs(gap) - abs(bundled)) <= TOLERANCE_AMOUNT:
            return _mk("amount", l, b, "commission", "the difference equals the commission the book holds apart: the broker bundles it into the fill")
        return _mk("amount", l, b, "data_quality", "same line, another amount: a price or a cash figure differs")
    if l.date != b.date:
        return _mk("timing", l, b, "timing", "the same line on another day")
    return None


def _late(line: Line, as_of: str) -> bool:
    return date.fromisoformat(line.date) >= date.fromisoformat(as_of) - timedelta(days=LATE_DAYS)


def compare(entries: Sequence[BookEntry], statement: Statement, *, desk_capital_sum: Decimal | None = None) -> tuple[list[Break], dict[str, Any]]:
    """The full join of the book and the statement. Pure: no database. Returns the breaks (ordered by impact) and a summary."""
    if len(entries) > MAX_LINES or len(statement.lines) > MAX_LINES:
        raise ReconError("too many lines to reconcile")
    as_of = statement.as_of
    local = [_from_entry(e) for e in entries if e.kind in COMPARED_KINDS and e.occurred_at[:10] <= as_of
             and (statement.period_start is None or e.occurred_at[:10] >= statement.period_start)]
    broker = [_from_statement(s) for s in statement.lines]
    commission_by_ref: dict[str, Decimal] = {}
    for x in local:
        if x.kind == "commission":
            commission_by_ref[x.ref] = commission_by_ref.get(x.ref, ZERO) + x.amount
    groups: dict[str, tuple[list[Line], list[Line]]] = {}
    for x in local:
        groups.setdefault(x.key, ([], []))[0].append(x)
    for y in broker:
        groups.setdefault(y.key, ([], []))[1].append(y)
    breaks: list[Break] = []
    only_local: list[tuple[Line, str]] = []
    only_broker: list[tuple[Line, str]] = []
    matched = 0
    for key in sorted(groups):
        ls, bs = sorted(groups[key][0], key=lambda z: (z.date, z.amount)), sorted(groups[key][1], key=lambda z: (z.date, z.amount))
        for l, b in zip(ls, bs):
            found = _compare(l, b, commission_by_ref)
            if found is None:
                matched += 1
            else:
                breaks.append(found)
        dup = bool(ls and bs)
        only_local += [(x, "duplicate" if dup else "missing") for x in ls[len(bs):]]
        only_broker += [(x, "duplicate" if dup else "missing") for x in bs[len(ls):]]
    # one line on each side that are the same thing under another contract id or reference: a mapping problem, reported on both sides
    left_over_broker = list(only_broker)
    for l, why in only_local:
        partner = next((p for p in left_over_broker if p[0].kind == l.kind and _qty_equal(p[0].qty, l.qty) and abs(p[0].amount - l.amount) <= TOLERANCE_AMOUNT), None)
        if partner is not None:
            left_over_broker.remove(partner)
            breaks.append(_mk("one_side", l, None, "mapping", f"the book has it as {l.key} and the statement as {partner[0].key}"))
            breaks.append(_mk("one_side", None, partner[0], "mapping", f"the statement has it as {partner[0].key} and the book as {l.key}"))
        elif l.kind in ("commission", "fee"):
            breaks.append(_mk("one_side", l, None, "commission", "the book holds a charge the statement does not show: the broker may bundle it"))
        elif _late(l, as_of):
            breaks.append(_mk("one_side", l, None, "timing", "the line is recent and the broker may not have posted it yet"))
        elif why == "duplicate":
            breaks.append(_mk("one_side", l, None, "duplicate_or_missing", "the book holds the same reference more times than the statement", repeat=True))
        else:
            breaks.append(_mk("one_side", l, None, "duplicate_or_missing", "the statement has no such line"))
    for b, why in left_over_broker:
        if b.currency != "USD":
            breaks.append(_mk("one_side", None, b, "currency", "the statement line is not in USD"))
        elif b.kind == "fill" and (b.contract_id is None or b.qty is None):
            breaks.append(_mk("one_side", None, b, "data_quality", "a fill line without its contract or quantity"))
        elif b.kind in ("commission", "fee"):
            breaks.append(_mk("one_side", None, b, "commission", "the statement charges something the book does not hold"))
        elif why == "duplicate":
            breaks.append(_mk("one_side", None, b, "duplicate_or_missing", "the statement repeats a reference the book holds fewer times", repeat=True))
        else:
            breaks.append(_mk("one_side", None, b, "duplicate_or_missing", "the book has no such line"))
    nav: dict[str, Any] = {"status": "skipped", "reason": "the statement has no account value"}
    if statement.account_value is not None:
        if desk_capital_sum is None:
            nav = {"status": "skipped", "reason": "no desk uses the account"}
        else:
            gap = statement.account_value - desk_capital_sum
            nav = {"status": "ok" if abs(gap) <= TOLERANCE_AMOUNT else "break", "desk_capital_sum": text(desk_capital_sum), "account_value": text(statement.account_value),
                   "difference": text(gap)}
            if abs(gap) > TOLERANCE_AMOUNT:
                breaks.append(Break("amount", f"{NAV_KEY}", {"desk_capital_sum": text(desk_capital_sum)}, {"account_value": text(statement.account_value)}, gap, abs(gap),
                                    "data_quality", detail="the sum of the capital of the desks is not the value of the account at the broker"))
    breaks.sort(key=lambda x: (-x.impact, x.key, x.bucket))
    counts = {name: sum(1 for x in breaks if x.bucket == name) for name in BUCKETS}
    summary = {"as_of": as_of, "local_lines": len(local), "statement_lines": len(broker), "matched": matched, "breaks": len(breaks), "by_bucket": counts,
               "tolerance_amount": text(TOLERANCE_AMOUNT), "tolerance_quantity": text(TOLERANCE_QTY), "nav_tieout": nav}
    return breaks, summary


def _capital_sum(session: Any, account_id: int) -> Decimal | None:
    rows = session.query("SELECT d.capital AS capital FROM desks d JOIN desk_accounts a ON a.desk_id = d.id WHERE a.account_id = :a", {"a": account_id})
    return sum((Decimal(str(r["capital"])) for r in rows), ZERO) if rows else None


def reconcile(account_id: int, *, statement: Statement, db: Database | None = None, persist: bool = True) -> Reconciliation:
    """Reconcile the book of an account against a statement and, unless `persist` is false, record the run in `reconciliations` and its breaks in
    `recon_breaks`. The book is only read."""
    database = db or get_database()
    if statement.account_id is not None and statement.account_id != account_id:
        raise ReconError("the statement belongs to another account")
    stamp = f"{date.fromisoformat(statement.as_of).isoformat()} 00:00:00"
    with database.read() as u:
        entries = book_module.entries_in(u.session, account_id)
        capital = _capital_sum(u.session, account_id)
    breaks, summary = compare(entries, statement, desk_capital_sum=capital)
    status = "breaks" if breaks else "clean"
    if not persist:
        return Reconciliation(account_id, statement.as_of, status, summary, tuple(breaks))
    saved: list[Break] = []
    with database.write() as u:
        recon_id = u.session.insert("reconciliations", {"account_id": account_id, "as_of": stamp, "status": status, "summary_json": json.dumps(summary, sort_keys=True)})
        for b in breaks:
            row_id = u.session.insert("recon_breaks", {
                "recon_id": recon_id, "bucket": b.bucket, "key": b.key[:200], "local_json": None if b.local is None else json.dumps(b.local, sort_keys=True),
                "broker_json": None if b.broker is None else json.dumps(b.broker, sort_keys=True), "diff": text(b.diff), "cause": b.cause})
            saved.append(replace(b, id=row_id))
    return Reconciliation(account_id, statement.as_of, status, summary, tuple(saved), recon_id)


def load_breaks(recon_id: int, *, db: Database | None = None) -> list[Break]:
    with (db or get_database()).read() as u:
        rows = u.session.query("SELECT * FROM recon_breaks WHERE recon_id = :r ORDER BY id", {"r": recon_id})
    out = [Break.from_row(r) for r in rows]
    return sorted(out, key=lambda x: (-x.impact, x.key, x.bucket))


# ------------------------------------------------------------------------------------------------ the read only reader, the trace and the critic

READ_ONLY_FORBIDDEN = ("append", "append_in", "record_fill", "record_fill_in", "write", "insert", "update", "delete", "execute", "session", "commit")


class NotReadOnly(TypeError):
    """The reader given to `trace` or `verify_break` has a way to write."""


class BookReader(Protocol):
    def find(self, *, source_ref: str | None = None, contract_id: str | None = None, kind: str | None = None) -> tuple[BookEntry, ...]: ...


class ReadOnlyBook:
    """A frozen copy of the entries of a book. It has no session and no method that writes: only reading."""

    __slots__ = ("_entries",)

    def __init__(self, entries: Iterable[BookEntry]) -> None:
        object.__setattr__(self, "_entries", tuple(entries))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("a read only book cannot be changed")

    @classmethod
    def load(cls, account_id: int, *, db: Database | None = None) -> "ReadOnlyBook":
        return cls(book_module.entries(account_id, db=db))

    def entries(self) -> tuple[BookEntry, ...]:
        return self._entries

    def find(self, *, source_ref: str | None = None, contract_id: str | None = None, kind: str | None = None) -> tuple[BookEntry, ...]:
        return tuple(e for e in self._entries if (source_ref is None or e.source_ref == source_ref) and (contract_id is None or e.contract_id == contract_id)
                     and (kind is None or e.kind == kind))


def _assert_read_only(reader: Any) -> None:
    if not callable(getattr(reader, "find", None)):
        raise NotReadOnly("the reader must offer find()")
    bad = [name for name in READ_ONLY_FORBIDDEN if hasattr(reader, name)]
    if bad:
        raise NotReadOnly(f"the reader has a way to write: {', '.join(bad)}")


@dataclass(frozen=True)
class Trace:
    sentence: str
    owner: str
    expected_date: str
    action: str
    material: bool
    cause: str

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ReconError("an action is watch, adjust, ticket or suppress")


WHY = {
    "timing": "the book and the statement post it on different days",
    "currency": "the line is not in USD and cannot be compared without a rate",
    "mapping": "the contract or the reference differs between the two sides",
    "duplicate_or_missing": "one side holds a line or a quantity the other does not",
    "commission": "the charge differs or the broker bundles it into the fill",
    "data_quality": "a field of the line is missing or does not add up",
}
PLAN = {                     # cause -> (owner, action when material, business days to resolve)
    "timing": ("back_office", "watch", 2),
    "currency": ("owner", "ticket", 5),
    "mapping": ("back_office", "adjust", 3),
    "duplicate_or_missing": ("owner", "adjust", 3),
    "commission": ("owner", "adjust", 3),
    "data_quality": ("owner", "ticket", 5),
}


def add_business_days(start: date, days: int) -> date:
    current = start
    while days > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days -= 1
    return current


def _what(side: Mapping[str, Any]) -> str:
    parts = [str(side.get("kind", "line"))]
    if side.get("qty"):
        parts.append(str(side["qty"]))
    if side.get("contract_id"):
        parts.append(str(side["contract_id"]))
    parts.append(f"for {side.get('amount')}")
    return " ".join(parts)


def trace(break_row: Break | Mapping[str, Any], book_reader: BookReader, *, today: date | None = None, materiality: Decimal = MATERIAL_AMOUNT) -> Trace:
    """One sentence for one break: who did what and why, with owner, expected date and action. It reads the book through `book_reader` (read only) and
    returns the diagnosis; it has no path to write an entry or an adjustment."""
    _assert_read_only(book_reader)
    row = break_row if isinstance(break_row, Break) else Break.from_row(break_row)
    base = today or datetime.now(timezone.utc).date()
    material = row.material if materiality == MATERIAL_AMOUNT else (row.bucket in ("quantity", "one_side", "timing") or row.is_nav or row.impact >= materiality)
    if row.is_nav:
        sentence = (f"The desks hold {row.local and row.local.get('desk_capital_sum')} of capital while the broker values the account at "
                    f"{row.broker and row.broker.get('account_value')}, a gap of {text(row.diff)} from trading results or flows that no desk holds yet; only the owner changes capital.")
        return Trace(sentence, "owner", add_business_days(base, 5).isoformat(), "watch", True, row.cause)
    local, broker = row.local, row.broker
    if local is not None:
        origin = f"desk {local['desk_id']}" if local.get("desk_id") else ("the owner" if local.get("source") == "owner" else f"the {local.get('source') or 'book'} feed")
        did = f"{origin} booked {_what(local)} on {local['date']}"
        same = book_reader.find(source_ref=local["ref"], kind=local["kind"])
        if len(same) > 1:
            did += f" ({len(same)} entries share that reference)"
        did += "; the statement " + (f"shows {_what(broker)} on {broker['date']}" if broker else "has no such line")
    elif broker is not None:
        known = book_reader.find(source_ref=broker["ref"])
        did = f"the broker posted {_what(broker)} on {broker['date']} and the book " + (f"holds {len(known)} other entry with that reference" if known else "never recorded it")
    else:
        did = "a break without either side"
    owner, action, days = PLAN[row.cause]
    if not material and row.bucket == "amount":
        action = "suppress"
    sentence = " ".join(f"{did}, because {WHY[row.cause]}.".split())[:420]
    return Trace(sentence, owner, add_business_days(base, days).isoformat(), action, material, row.cause)


@dataclass(frozen=True)
class Verdict:
    key: str
    status: str                 # confirmed, stale or unverifiable
    reason: str


def verify_break(break_row: Break | Mapping[str, Any], book_reader: BookReader) -> Verdict:
    """The critic: look again at the book for one break. A break whose local side is no longer in the book as stored, or whose missing line is now
    there, is `stale`; the capital tie-out cannot be checked from the entries and is `unverifiable`."""
    _assert_read_only(book_reader)
    row = break_row if isinstance(break_row, Break) else Break.from_row(break_row)
    if row.is_nav:
        return Verdict(row.key, "unverifiable", "the capital of the desks is not in the entries of the book")
    if row.local is None and row.broker is not None:
        present = book_reader.find(source_ref=row.broker["ref"], kind=row.broker["kind"], contract_id=row.broker.get("contract_id"))
        if present and not row.broker.get("repeat"):
            return Verdict(row.key, "stale", "the book now holds a line with that reference")
        return Verdict(row.key, "confirmed", "the book holds fewer lines than the statement" if present else "the book still has no such line")
    local = row.local or {}
    found = book_reader.find(source_ref=local.get("ref"), kind=local.get("kind"), contract_id=local.get("contract_id"))
    if not found:
        return Verdict(row.key, "stale", "the entry is no longer in the book")
    stored = Decimal(str(local["amount"]))
    if not any(e.amount == stored for e in found):
        return Verdict(row.key, "stale", "the amount of the entry in the book changed since the break was found")
    return Verdict(row.key, "confirmed", "the entry is in the book as stored")


def apply_traces(traces: Mapping[int, Trace], *, db: Database | None = None) -> int:
    """Store the explanation, owner, date and action of each break in `recon_breaks` (the break table only; the book is not touched)."""
    n = 0
    with (db or get_database()).write() as u:
        for break_id, t in traces.items():
            n += u.session.update("recon_breaks", {"id": break_id},
                                  {"explanation": t.sentence[:600], "owner": t.owner, "due_at": f"{t.expected_date} 00:00:00", "action": t.action})
    return n


__all__ = ["ACTIONS", "BUCKETS", "Break", "CAUSES", "NotReadOnly", "ReadOnlyBook", "Reconciliation", "ReconError", "Statement", "StatementLine", "Trace", "Verdict",
           "add_business_days", "apply_traces", "compare", "load_breaks", "reconcile", "trace", "verify_break"]
