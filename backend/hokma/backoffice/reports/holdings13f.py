"""The holdings of an institutional manager as a report (SPEC-018: ED-004): the table of positions of the latest quarter, the comparison with
the quarter before, the history of the total value by quarter and a sparkline of it. The data comes from `hokma.data.sec.thirteenf` (H1);
this module only lays it out and does no network call of its own. Every text that comes from a filing is escaped.

Money figures are shown from the exact text of the number the filing gave: the unit decision (dollars or thousands) is the one of H1 and its
warning is printed in the report."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from html import escape

from hokma.data.sec import thirteenf
from hokma.data.sec.client import SecClient
from hokma.exec.money import dec

TOP_ROWS = 25
SPARK_W, SPARK_H = 160, 32
_UNIT = Decimal(1)


@dataclass(frozen=True)
class Rendered:
    status: str                      # ok | no_data | unavailable
    html: str
    warnings: tuple[str, ...] = ()


def _usd(value: float) -> str:
    return f"{dec(value, 'value').quantize(_UNIT, rounding=ROUND_HALF_UP):,}"


def _pct(value: float | None) -> str:
    return "new" if value is None else f"{(dec(value, 'pct') * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def sparkline(values: Sequence[float]) -> str:
    """A small line of the values, oldest first, as inline SVG."""
    if len(values) < 2:
        return ""
    nums = [dec(v, "value") for v in values]
    low, high = min(nums), max(nums)
    span = (high - low) or Decimal(1)
    pts = [f"{(Decimal(SPARK_W) * i / (len(nums) - 1)).quantize(Decimal('0.1'))},{(SPARK_H - Decimal(SPARK_H - 4) * (v - low) / span - 2).quantize(Decimal('0.1'))}"
           for i, v in enumerate(nums)]
    return (f'<svg role="img" aria-label="Value by quarter" width="{SPARK_W}" height="{SPARK_H}" viewBox="0 0 {SPARK_W} {SPARK_H}">'
            f'<polyline fill="none" stroke="#2f6fed" stroke-width="2" points="{" ".join(pts)}"/></svg>')


def _positions(report: thirteenf.ThirteenFReport, limit: int) -> str:
    ordered = sorted(report.holdings, key=lambda h: (-h.value_usd, h.cusip))[:limit]
    rows = "".join(f"<tr><td>{escape(h.issuer)}</td><td>{escape(h.ticker or '')}</td><td>{escape(h.cusip)}</td><td>{escape(h.class_title)}</td>"
                   f"<td>{escape(h.put_call or '')}</td><td>{escape(_usd(h.shares))}</td><td>{escape(_usd(h.value_usd))}</td></tr>" for h in ordered)
    return ("<table><tr><th>Issuer</th><th>Ticker</th><th>CUSIP</th><th>Class</th><th>Put/Call</th><th>Shares</th><th>Value (USD)</th></tr>" + rows + "</table>")


def _comparison(current: thirteenf.ThirteenFReport, previous: thirteenf.ThirteenFReport, limit: int) -> str:
    changes = thirteenf.compare_reports(current, previous)[:limit]
    rows = "".join(f"<tr><td>{escape(c.issuer)}</td><td>{escape(c.status)}</td><td>{escape(_usd(c.shares_previous))}</td><td>{escape(_usd(c.shares_current))}</td>"
                   f"<td>{escape(_pct(c.pct_change))}</td><td>{escape(_usd(c.value_current_usd))}</td></tr>" for c in changes)
    return ("<table><tr><th>Issuer</th><th>Change</th><th>Shares before</th><th>Shares now</th><th>Shares %</th><th>Value now (USD)</th></tr>" + rows + "</table>")


def _history(history: Sequence[thirteenf.ThirteenFReport]) -> str:
    rows = "".join(f"<tr><td>{escape(str(r.period))}</td><td>{r.entry_count}</td><td>{escape(_usd(r.total_value_usd))}</td></tr>" for r in history)
    return "<table><tr><th>Quarter</th><th>Positions</th><th>Total value (USD)</th></tr>" + rows + "</table>"


def render(history: Sequence[thirteenf.ThirteenFReport], *, rows: int = TOP_ROWS, warnings: Sequence[str] = ()) -> str:
    """The report of a manager from its holdings reports, newest first (the order of `thirteenf.holdings_history`)."""
    if not history:
        raise ValueError("a 13F report needs at least one holdings report")
    latest = history[0]
    shown = max(1, min(int(rows), 200))
    parts = [f"<h1>{escape(latest.manager_name)}</h1>", f"<p>Quarter {escape(str(latest.period))}, {latest.entry_count} positions, "
             f"total {escape(_usd(latest.total_value_usd))} USD ({escape(latest.unit.unit)}).</p>"]
    for note in [*warnings, *([latest.unit.warning] if latest.unit.warning else [])]:
        parts.append(f'<p class="warning">{escape(note)}</p>')
    parts += ["<h2>Positions</h2>", _positions(latest, shown)]
    if len(history) > 1:
        parts += [f"<h2>Change against {escape(str(history[1].period))}</h2>", _comparison(latest, history[1], shown)]
    parts += ["<h2>History</h2>", _history(history), sparkline([r.total_value_usd for r in reversed(history)])]
    return "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>13F</title></head><body>" + "".join(parts) + "</body></html>"


def build(client: SecClient, manager: str | int, *, as_of: datetime | date, quarters: int = 4, unit: str | None = None, rows: int = TOP_ROWS) -> Rendered:
    """Read the history of a manager (visible at `as_of`) through the H1 filings module and render it. A manager with no report is
    `no_data`; a source that cannot be reached is `unavailable`; neither produces an empty table."""
    got = thirteenf.holdings_history(client, manager, as_of=as_of, quarters=max(1, min(int(quarters), 12)), unit=unit)
    if not got.ok:
        return Rendered(str(got.status).lower(), "", tuple(got.warnings))
    return Rendered("ok", render(got.value, rows=rows, warnings=got.warnings), tuple(got.warnings))


__all__ = ["Rendered", "build", "render", "sparkline"]
