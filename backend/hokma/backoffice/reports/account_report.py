"""Periodic report of an account (SPEC-018: AFS-043). It holds the return of the period, of the year and since the inception of the account, against a benchmark
that was fixed beforehand (never one chosen after the result is known), net of commissions; the allocation by asset class; the positions with their
return; the activity of the period; and the risk notices.

* Returns are money weighted (modified Dietz) on a realized basis: capital is cash plus the cost of what is open, so an unrealized move is not in
  the return. The report says so in its notices. A return that cannot be computed is None, never zero.
* Every figure is pure Decimal code over the book. No number is written by a model, and the report holds no brand and no comment on the market.
* `render_html` escapes every text it prints.
"""
from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from hokma.backoffice import book as book_module
from hokma.backoffice.book import Book, BookEntry
from hokma.backoffice.rollforward import capital_at
from hokma.contracts import identity
from hokma.db import Database, get_database
from hokma.exec.money import ZERO, dec, dec_or_none, text

HUNDRED = Decimal(100)
NOTICES = {
    "past_performance": "Past results do not guarantee future results.",
    "realized_basis": "Returns are on a realized basis: open positions are carried at cost, so unrealized moves are not part of them.",
    "net_of_costs": "Returns are net of commissions and fees.",
}


@dataclass(frozen=True)
class Benchmark:
    """The benchmark fixed beforehand and its returns over the three spans. A span with no return is None and is reported as missing."""
    name: str
    period: Decimal | None = None
    year: Decimal | None = None
    inception: Decimal | None = None
    allocation: Mapping[str, Decimal] | None = None      # weights by asset class
    fixed_on: str | None = None


def as_benchmark(value: Any) -> Benchmark | None:
    """A Benchmark, the symbol that `backoffice.benchmarks.declared` returns, a mapping or an object with the same names."""
    if value is None or isinstance(value, Benchmark):
        return value
    if isinstance(value, str):                     # the symbol that `benchmarks.declared` returns: its returns come from the caller or stay missing
        return Benchmark(value.strip()[:40]) if value.strip() else None
    get = value.get if isinstance(value, Mapping) else (lambda k, d=None: getattr(value, k, d))
    name = get("name") or get("symbol")
    if not name:
        return None
    alloc = get("allocation")
    return Benchmark(str(name), dec_or_none(get("period")), dec_or_none(get("year")), dec_or_none(get("inception")),
                     None if alloc is None else {str(k): dec(v) for k, v in alloc.items()}, None if get("fixed_on") is None else str(get("fixed_on"))[:10])


@dataclass(frozen=True)
class Returns:
    start: str
    end: str
    opening: Decimal
    closing: Decimal
    flows: Decimal
    net_result: Decimal
    costs: Decimal
    net_return: Decimal | None
    gross_return: Decimal | None


@dataclass(frozen=True)
class AccountReport:
    account_id: int
    start: str
    end: str
    benchmark: str | None
    returns: dict[str, Returns]
    benchmark_returns: dict[str, Decimal | None]
    allocation: list[dict[str, Any]]
    positions: list[dict[str, Any]]
    activity: dict[str, Any]
    notices: list[str]
    missing: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        def ret(r: Returns) -> dict[str, Any]:
            return {"start": r.start, "end": r.end, "opening": text(r.opening), "closing": text(r.closing), "flows": text(r.flows), "net_result": text(r.net_result),
                    "costs": text(r.costs), "net_return": text(r.net_return), "gross_return": text(r.gross_return)}

        return {"account_id": self.account_id, "start": self.start, "end": self.end, "benchmark": self.benchmark, "returns": {k: ret(v) for k, v in self.returns.items()},
                "benchmark_returns": {k: text(v) for k, v in self.benchmark_returns.items()}, "allocation": self.allocation, "positions": self.positions,
                "activity": self.activity, "notices": self.notices, "missing": self.missing}


def _d(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def modified_dietz(entries: list[BookEntry], *, base: Decimal, start: date, end: date) -> Returns:
    """Return of the span [start, end] (both days included): (closing - opening - flows) / (opening + sum of flow * weight). A flow weighs the days it was
    in the account over the days of the span."""
    s_stamp, e_stamp = f"{start.isoformat()} 00:00:00", f"{end.isoformat()} 23:59:59"
    opening = capital_at(entries, base=base, before=s_stamp)
    closing = capital_at([e for e in entries if e.occurred_at <= e_stamp], base=base, before="9999")
    inside = [e for e in entries if s_stamp <= e.occurred_at <= e_stamp]
    total_days = (end - start).days + 1
    flows = ZERO
    weighted = ZERO
    for e in inside:
        if e.kind in ("deposit", "withdrawal"):
            flows += e.amount
            weighted += e.amount * Decimal((end - date.fromisoformat(e.occurred_at[:10])).days + 1) / Decimal(total_days)
    costs = -sum((e.amount for e in inside if e.kind in ("commission", "fee")), ZERO)
    result = closing - opening - flows
    denominator = opening + weighted
    net = None if denominator <= 0 else result / denominator
    gross = None if denominator <= 0 else (result + costs) / denominator
    return Returns(start.isoformat(), end.isoformat(), opening, closing, flows, result, costs, net, gross)


def asset_class(contract_id: str) -> str:
    """The asset class of a contract id, only from its exact canonical form; anything else is `unknown`."""
    try:
        return identity.parse_canonical(contract_id).asset_type
    except (LookupError, ValueError):
        return "unknown"


def build_report(account_id: int, *, start: date | str, end: date | str, benchmark: Any = None, desk_id: int | None = None, marks: Mapping[str, Decimal] | None = None,
                 db: Database | None = None) -> AccountReport:
    database = db or get_database()
    s, e = _d(start), _d(end)
    if e < s:
        raise ValueError("the end of the report is before its start")
    with database.read() as u:
        entries = book_module.entries_in(u.session, account_id)
        last = u.broker.last_entry(account_id)
    base = dec(last.amount) if last is not None else ZERO
    first_days = [date.fromisoformat(entries[0].occurred_at[:10])] if entries else []
    opened = [_d(last.created_at[:10])] if last is not None else []
    inception = min([*opened, *first_days, e] if (opened or first_days) else [s])        # the account begins with its ledger or its first entry, whichever is earlier
    missing: dict[str, str] = {}
    chosen = as_benchmark(benchmark)
    if chosen is None and desk_id is not None:
        try:
            from hokma.backoffice import benchmarks            # written apart; absent, the report says the benchmark is missing

            chosen = as_benchmark(benchmarks.declared(desk_id, db=database))
        except (ImportError, AttributeError, LookupError):
            chosen = None
    if chosen is None:
        missing["benchmark"] = "no benchmark was fixed beforehand for this account"
    year_start = max(date(e.year, 1, 1), inception)
    spans = {"period": (s, e), "year": (year_start, e), "since_inception": (inception, e)}
    returns = {name: modified_dietz(entries, base=base, start=a, end=b) for name, (a, b) in spans.items()}
    bench = {"period": chosen and chosen.period, "year": chosen and chosen.year, "since_inception": chosen and chosen.inception}
    bench = {k: (v if isinstance(v, Decimal) else None) for k, v in bench.items()}
    if chosen is not None:
        for k, v in bench.items():
            if v is None:
                missing[f"benchmark_{k}"] = "the benchmark has no return for this span"
    upto = [x for x in entries if x.occurred_at <= f"{e.isoformat()} 23:59:59"]
    book = Book(upto)
    prices = {k: dec(v) for k, v in (marks or {}).items()}
    positions, values = [], {}
    for cid, p in sorted(book.cost_positions().items()):
        price = prices.get(cid)
        sign = Decimal(1) if p.qty > 0 else Decimal(-1)
        value = p.qty * (price if price is not None else p.avg_price) * p.multiplier
        values[cid] = value
        row = {"contract_id": cid, "qty": text(p.qty), "avg_price": text(p.avg_price), "price": text(price), "value": text(value),
               "valued_at": "mark" if price is not None else "cost",
               "unrealized": text(None if price is None else (price - p.avg_price) * p.qty * p.multiplier),
               "return_pct": text(None if price is None else (price / p.avg_price - 1) * HUNDRED * sign)}
        positions.append(row)
        if price is None:
            missing[f"unpriced:{cid}"] = "no market price: the position is carried at cost and has no return"
    cash = base + sum((x.amount for x in upto), ZERO) if last is not None else None
    classes: dict[str, Decimal] = {}
    for cid, v in values.items():
        classes[asset_class(cid)] = classes.get(asset_class(cid), ZERO) + v
    if cash is not None:
        classes["cash"] = cash
    total = sum(classes.values(), ZERO)
    allocation = []
    for name in sorted(set(classes) | set(chosen.allocation or {} if chosen else {})):
        weight = None if total == 0 else classes.get(name, ZERO) / total
        bench_weight = None if chosen is None or chosen.allocation is None else chosen.allocation.get(name, ZERO)
        allocation.append({"class": name, "value": text(classes.get(name, ZERO)), "weight": text(weight), "benchmark_weight": text(bench_weight),
                           "active_weight": text(None if weight is None or bench_weight is None else weight - bench_weight)})
    inside = [x for x in entries if f"{s.isoformat()} 00:00:00" <= x.occurred_at <= f"{e.isoformat()} 23:59:59"]
    kinds = {k: sum((x.amount for x in inside if x.kind in group), ZERO) for k, group in
             {"deposits": ("deposit",), "withdrawals": ("withdrawal",), "dividends": ("dividend",), "interest": ("interest",), "commissions": ("commission", "fee")}.items()}
    fills = [x for x in inside if x.kind == "fill"]
    activity = {"trades": len(fills), "bought": text(sum((-x.amount for x in fills if x.amount < 0), ZERO)), "sold": text(sum((x.amount for x in fills if x.amount > 0), ZERO)),
                **{k: text(v) for k, v in kinds.items()}}
    notices = [NOTICES["past_performance"], NOTICES["realized_basis"], NOTICES["net_of_costs"]]
    if any(k.startswith("unpriced:") for k in missing):
        notices.append("Some positions have no market price and are carried at cost.")
    if "benchmark" in missing:
        notices.append("No benchmark was fixed beforehand: no comparison is shown.")
    return AccountReport(account_id, s.isoformat(), e.isoformat(), chosen.name if chosen else None, returns, bench, allocation, positions, activity, notices, missing)


def render_html(report: AccountReport) -> str:
    """The report as HTML. Every value that is printed goes through `html.escape`: a symbol, a notice or a benchmark name cannot add markup."""
    esc = lambda v: html.escape("" if v is None else str(v), quote=True)       # noqa: E731
    data = report.to_dict()
    parts = [f"<h1>Account report {esc(data['start'])} to {esc(data['end'])}</h1>", f"<p>Benchmark: {esc(data['benchmark'] or 'none fixed')}</p>",
             "<table><tr><th>span</th><th>net return</th><th>gross return</th><th>benchmark</th></tr>"]
    for name, r in data["returns"].items():
        parts.append(f"<tr><td>{esc(name)}</td><td>{esc(r['net_return'])}</td><td>{esc(r['gross_return'])}</td><td>{esc(data['benchmark_returns'].get(name))}</td></tr>")
    parts.append("</table><h2>Allocation</h2><table>")
    parts += [f"<tr><td>{esc(a['class'])}</td><td>{esc(a['weight'])}</td><td>{esc(a['benchmark_weight'])}</td></tr>" for a in data["allocation"]]
    parts.append("</table><h2>Positions</h2><table>")
    parts += [f"<tr><td>{esc(p['contract_id'])}</td><td>{esc(p['qty'])}</td><td>{esc(p['avg_price'])}</td><td>{esc(p['price'])}</td><td>{esc(p['return_pct'])}</td></tr>"
              for p in data["positions"]]
    parts.append("</table><h2>Activity</h2><table>")
    parts += [f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in data["activity"].items()]
    parts.append("</table><h2>Notices</h2><ul>")
    parts += [f"<li>{esc(n)}</li>" for n in data["notices"]]
    parts.append("</ul>")
    return "\n".join(parts)


__all__ = ["AccountReport", "Benchmark", "NOTICES", "Returns", "as_benchmark", "asset_class", "build_report", "modified_dietz", "render_html"]
