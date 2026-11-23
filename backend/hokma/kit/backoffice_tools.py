"""Back office tools for the desk agents and for Anna (SPEC-018, B10): the book of an account, its snapshot and profit and loss, the
reconciliation against a broker statement, the reports and the export. Every number is computed by `hokma.backoffice`; the tools only read and
format. They write nothing to the book: the only writes are the reconciliation record, the report and the export file, in a folder the code
picks (no path comes from an agent). Sizes are capped before anything runs. A desk chief reaches only the desk of its own project."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

from hokma.kit.tool_registry import ToolError, ToolSet

TOOLSET = ToolSet()
MAX_STATEMENT_LINES = 5_000
MAX_THESES = 500
MAX_DAYS = 3_660


def _i(desc: str = "") -> dict[str, Any]:
    return {"type": "integer", "description": desc}


def _s(desc: str = "", **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def _o(desc: str = "") -> dict[str, Any]:
    return {"type": "object", "description": desc}


def _dump(value: Any) -> str:
    def plain(v: Any) -> Any:
        if isinstance(v, Decimal):
            return format(v.normalize(), "f")
        if isinstance(v, dict):
            return {str(k): plain(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [plain(x) for x in v]
        return v

    return json.dumps(plain(value), ensure_ascii=False, default=str)


def _desk(c: Any, desk_id: Any) -> int:
    from hokma.kit import exec_tools

    return exec_tools._desk(c, desk_id)


def _account(desk_id: int):
    from hokma.db import get_database

    with get_database().read() as u:
        account = u.broker.account_of_desk(desk_id)
    if account is None:
        raise ToolError("no_data", "the desk has no account", ["desk_assign_account"])
    return account


def _day(value: Any, name: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ToolError("bad_input", f"{name} must be a date like 2026-01-31") from None


def _period(a: dict) -> tuple[date, date]:
    start, end = _day(a["start"], "start"), _day(a["end"], "end")
    if end < start or (end - start).days > MAX_DAYS:
        raise ToolError("bad_input", f"end must be after start by at most {MAX_DAYS} days")
    return start, end


def _prices(ids: list[str]) -> dict[str, Decimal]:
    from hokma.broker import sim_engine

    return dict(sim_engine.mark_source(ids))


@TOOLSET.tool("book_positions", "The open positions of the book of the desk account: quantity, average price, multiplier, mark and value. A position with no price has no value and says unpriced.", {
    "desk_id": _i("Desk id.")}, ["desk_id"])
def book_positions(u, c, a: dict) -> str:
    from hokma.backoffice import views
    from hokma.backoffice.book import Book

    account = _account(_desk(c, a["desk_id"]))
    book = Book.load(account.id)
    return _dump({"desk_id": a["desk_id"], "positions": views.to_text(views.positions_overview(book, _prices(list(book.cost_positions()))))})


@TOOLSET.tool("book_snapshot", "The snapshot of the desk account: cash, positions value, equity, day result and peak. A figure that is not known is null with its reason in missing; never zero.", {
    "desk_id": _i("Desk id.")}, ["desk_id"])
def book_snapshot(u, c, a: dict) -> str:
    from hokma.backoffice import book as book_module
    from hokma.backoffice import snapshot

    account = _account(_desk(c, a["desk_id"]))
    ids = list(book_module.Book.load(account.id).cost_positions())
    snap = snapshot.snapshot(account.id, prices=_prices(ids))
    return _dump({k: getattr(snap, k) for k in ("account_id", "as_of", "cash", "positions_value", "equity", "day_pnl", "peak_equity", "realized_pnl",
                                                 "total_profit", "open_contracts", "positions", "missing")})


@TOOLSET.tool("book_pnl", "Profit and loss of the desk between two dates, realized, with commissions taken out, by symbol.", {
    "desk_id": _i("Desk id."), "start": _s("Date."), "end": _s("Date.")}, ["desk_id", "start", "end"])
def book_pnl(u, c, a: dict) -> str:
    from hokma.backoffice import pnl

    start, end = _period(a)
    return _dump(pnl.pnl(_desk(c, a["desk_id"]), start=f"{start} 00:00:00", end=f"{end} 23:59:59").to_dict())


@TOOLSET.tool("recon_run", "Reconcile the book of the desk account against a broker statement you were given as rows {ref, kind, contract_id, qty, amount, date, price?}. Amounts carry the sign of the book (a buy is negative); quantities are signed. Returns the breaks, largest first, each with its probable cause.", {
    "desk_id": _i("Desk id."), "statement": _o("{as_of, lines:[...], account_value?} at most 5000 lines.")}, ["desk_id", "statement"], writes=True)
def recon_run(u, c, a: dict) -> str:
    from hokma.backoffice import recon

    account = _account(_desk(c, a["desk_id"]))
    body = a["statement"]
    lines = body.get("lines")
    if not isinstance(lines, list) or not lines or len(lines) > MAX_STATEMENT_LINES:
        raise ToolError("bad_input", f"the statement needs 1 to {MAX_STATEMENT_LINES} lines")
    try:
        statement = recon.Statement.from_rows(lines, as_of=_day(body.get("as_of"), "as_of").isoformat(), account_id=account.id, account_value=body.get("account_value"))
        result = recon.reconcile(account.id, statement=statement)
    except recon.ReconError as exc:
        raise ToolError("bad_input", str(exc)) from None
    return _dump({"recon_id": result.recon_id, "status": result.status, "summary": result.summary,
                  "breaks": [{"id": b.id, "bucket": b.bucket, "key": b.key, "cause": b.cause, "diff": b.diff, "impact": b.impact} for b in result.breaks[:200]]})


@TOOLSET.tool("recon_breaks", "The breaks of the last reconciliation of the desk account, largest first.", {"desk_id": _i("Desk id.")}, ["desk_id"])
def recon_breaks(u, c, a: dict) -> str:
    from hokma.backoffice import recon
    from hokma.db import get_database

    account = _account(_desk(c, a["desk_id"]))
    with get_database().read() as unit:
        row = unit.session.one("SELECT id, status, as_of FROM reconciliations WHERE account_id = :a ORDER BY id DESC LIMIT 1", {"a": account.id})
    if row is None:
        raise ToolError("no_data", "no reconciliation has been run for this account", ["recon_run"])
    return _dump({"recon_id": row["id"], "status": row["status"], "as_of": row["as_of"],
                  "breaks": [{"id": b.id, "bucket": b.bucket, "key": b.key, "cause": b.cause, "diff": b.diff, "impact": b.impact, "action": b.action}
                             for b in recon.load_breaks(row["id"])[:200]]})


@TOOLSET.tool("report_account", "The performance report of the desk account for a period: return for the period, the year and since the start against the benchmark the desk declared, net of commissions, with allocation, positions and activity. Numbers are computed, never written.", {
    "desk_id": _i("Desk id."), "start": _s("Date."), "end": _s("Date.")}, ["desk_id", "start", "end"])
def report_account(u, c, a: dict) -> str:
    from hokma.backoffice import benchmarks
    from hokma.backoffice import book as book_module
    from hokma.backoffice.reports import account_report

    desk_id = _desk(c, a["desk_id"])
    account = _account(desk_id)
    start, end = _period(a)
    ids = list(book_module.Book.load(account.id).cost_positions())
    report = account_report.build_report(account.id, start=start, end=end, benchmark=benchmarks.declared(desk_id), desk_id=desk_id, marks=_prices(ids))
    return _dump(report.to_dict())


@TOOLSET.tool("report_weekly", "The weekly digest of closed theses you give as rows (thesis_id, closed_on, side, entry_price, stop_price, qty, multiplier, gross_pnl, costs, mae_pct, mfe_pct): hit rate, expectation, profit factor, R only with a positive stop risk, MAE and MFE. At most 500 theses.", {
    "desk_id": _i("Desk id."), "start": _s("Date."), "end": _s("Date."),
    "theses": {"type": "array", "items": {"type": "object"}, "description": "Closed theses."}}, ["desk_id", "start", "end", "theses"])
def report_weekly(u, c, a: dict) -> str:
    from hokma.backoffice import weekly

    _desk(c, a["desk_id"])
    start, end = _period(a)
    if len(a["theses"]) > MAX_THESES:
        raise ToolError("bad_input", f"at most {MAX_THESES} theses")
    try:
        digest = weekly.build_digest([weekly.thesis_from_row(r) for r in a["theses"]], start=start, end=end)
    except weekly.DigestError as exc:
        raise ToolError("bad_input", str(exc)) from None
    return _dump(digest.to_dict() if hasattr(digest, "to_dict") else vars(digest))


@TOOLSET.tool("export_book", "Export the book of the desk account to a csv or xlsx file in the exports folder of Hokma (the folder and the name are chosen by Hokma). Returns the file names.", {
    "desk_id": _i("Desk id."), "format": _s("csv, xlsx or both.", enum=["csv", "xlsx", "both"])}, ["desk_id"], writes=True)
def export_book(u, c, a: dict) -> str:
    from hokma.backoffice import exports
    from hokma.core.config import hokma_data_dir

    desk_id = _desk(c, a["desk_id"])
    account = _account(desk_id)
    chosen = a.get("format") or "csv"
    formats = exports.FORMATS if chosen == "both" else (chosen,)
    folder = hokma_data_dir() / "exports" / f"desk-{desk_id}"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        files = exports.export_book(account.id, folder, formats=formats)
    except exports.ExportError as exc:
        raise ToolError("bad_input", str(exc)) from None
    return _dump({"desk_id": desk_id, "files": sorted(p.name for p in files.values())})


TOOLS = TOOLSET.tools
