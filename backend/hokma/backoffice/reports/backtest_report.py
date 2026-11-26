"""Report of one backtest run (SPEC-020: L-39), in the style of the account report: ONE self-contained HTML file with the equity curve and the drawdown
as inline SVG (the charts of `reports.charts`), the table of metrics, the trades and the warnings, and the same facts as a PDF made of text.

Every number is the one of the result: nothing is computed here but the layout. Every text that comes from the result (a tag, a contract id, a
warning, a metric name) is escaped, so a report can never carry markup. The page loads nothing from outside. The PDF is plain text pages that
`backoffice.statements.read_statement_pdf` reads back, which is how the tests prove its content.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from html import escape
from typing import Any

from hokma.backoffice.reports import charts
from hokma.backtest.results import BacktestResult, Trade, daily_equity

MAX_TRADES = 200
MAX_METRIC_ROWS = 400
MAX_PDF_LINES = 2000
LINES_PER_PAGE = 52
WARNING_TEXT = {
    "historical_margin_missing": "Historical margin is missing for part of the period: the margin checks of this run are not exact.",
    "below_realistic_expectation": "The result is below the realistic expectation of the strategy.",
    "costs_kill_edge": "Costs remove the edge of the strategy.",
}


def _amount(value: Decimal | None) -> str:
    return "" if value is None else format(value.normalize() if value == value.to_integral() else value, "f")


def _figure(value: Any) -> str:
    if value is None:
        return "not available"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return format(value, ".6g")
    return str(value)


def metric_rows(result: BacktestResult) -> list[tuple[str, str, str]]:
    """(block, figure, value) of every scalar the stats carry. A block that is a table counts its rows; a block with no value says so."""
    rows: list[tuple[str, str, str]] = []
    for block in sorted(result.stats):
        q = result.stats[block]
        if q.value is None:
            rows.append((block, "", "not available: " + "; ".join(q.warnings)))
        elif isinstance(q.value, dict):
            for key in sorted(q.value):
                v = q.value[key]
                rows.append((block, key, f"{len(v)} item(s)" if isinstance(v, (list, dict)) else _figure(v)))
        else:
            rows.append((block, "", _figure(q.value)))
    return rows[:MAX_METRIC_ROWS]


def warning_lines(result: BacktestResult) -> list[str]:
    """What the judge must see first: the ambiguous bars (always, even when there are none) and every warning of the run."""
    dates = ", ".join(t.isoformat() for t in result.ambiguous_bar_dates[:20])
    lines = [f"Ambiguous bars (stop and target touched in the same bar): {result.ambiguous_bars}" + (f" at {dates}" if dates else "")]
    lines += [WARNING_TEXT.get(code, code) for code in result.warnings]
    return lines


def _points(result: BacktestResult) -> list[tuple[str, Decimal]]:
    equity = daily_equity(result.equity)
    points = [(str(day.date()), Decimal(repr(round(float(v), 2)))) for day, v in equity.items()]
    if len(points) > charts.MAX_POINTS:
        step = -(-len(points) // charts.MAX_POINTS)
        points = points[::step]
    return points


def _trade_cells(t: Trade) -> list[str]:
    return [t.contract_id, t.side, _amount(t.qty), t.entry_at.isoformat(), _amount(t.entry_price), "" if t.exit_at is None else t.exit_at.isoformat(),
            _amount(t.exit_price), _amount(t.pnl_usd), _amount(t.costs_usd), _amount(t.mae_usd), _amount(t.mfe_usd), t.entry_tag or "", t.exit_tag or ""]


TRADE_HEAD = ("Contract", "Side", "Qty", "Entry at", "Entry price", "Exit at", "Exit price", "Result USD", "Costs USD", "MAE USD", "MFE USD", "Entry tag", "Exit tag")


def render_html(result: BacktestResult, *, title: str) -> str:
    """The report of a run as one HTML text."""
    safe = escape(str(title)[:120])
    points = _points(result)
    curve = (f"<h2>Equity curve</h2>{charts.equity_svg(points)}<h2>Drawdown</h2>{charts.drawdown_svg(points)}" if len(points) >= 2
             else "<h2>Equity curve</h2><p>The run has fewer than two days of equity: no curve.</p>")
    notes = "".join(f"<li>{escape(line)}</li>" for line in warning_lines(result))
    metrics = "".join(f"<tr><td>{escape(b)}</td><td>{escape(k)}</td><td>{escape(v)}</td></tr>" for b, k, v in metric_rows(result))
    shown = result.trades[:MAX_TRADES]
    head = "".join(f"<th>{escape(h)}</th>" for h in TRADE_HEAD)
    body = "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in _trade_cells(t)) + "</tr>" for t in shown)
    cut = f"<p class=\"note\">{len(shown)} of {len(result.trades)} trades shown.</p>" if len(result.trades) > len(shown) else ""
    facts = (f"Run {result.run_id}, status {escape(result.status)}. Spec {escape(result.spec_hash)}, data {escape(result.data_hash)}, "
             f"engine {escape(result.engine_version)}, seed {result.seed}. Realized {escape(_amount(result.realized_pnl_usd))} USD, "
             f"marked to market {escape(_amount(result.mtm_pnl_usd))} USD.")
    costs = ", ".join(f"{escape(k)} {escape(_amount(v))}" for k, v in sorted(result.costs_usd.items()))
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">"
            f"<title>{safe}</title><style>{charts.CSS}</style></head><body><h1>{safe}</h1><p>{facts}</p><p>Costs USD: {costs or 'none'}.</p>"
            f"<h2>Warnings</h2><ul>{notes}</ul>{curve}<h2>Metrics</h2><table><tr><th>Block</th><th>Figure</th><th>Value</th></tr>{metrics}</table>"
            f"<h2>Trades</h2><table><tr>{head}</tr>{body}</table>{cut}</body></html>")


def _pdf_escape(text: str) -> str:
    safe = text.encode("ascii", "replace").decode("ascii")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_lines(result: BacktestResult, *, title: str) -> list[str]:
    """The report as plain lines: title, facts, warnings, metrics and trades."""
    lines = [str(title)[:120], f"Run {result.run_id} status {result.status} seed {result.seed}", f"Spec {result.spec_hash}", f"Data {result.data_hash}",
             f"Engine {result.engine_version}", f"Realized USD {_amount(result.realized_pnl_usd)} marked to market USD {_amount(result.mtm_pnl_usd)}", "", "Warnings"]
    lines += warning_lines(result) + ["", "Metrics"] + [f"{b} {k} {v}".replace("  ", " ") for b, k, v in metric_rows(result)] + ["", "Trades"]
    lines += [" ".join(c or "-" for c in _trade_cells(t)) for t in result.trades[:MAX_TRADES]]
    return lines[:MAX_PDF_LINES]


def render_pdf(result: BacktestResult, *, title: str) -> bytes:
    """The report as a text PDF, built byte by byte with no PDF library. Read it back with `statements.read_statement_pdf`."""
    lines = text_lines(result, title=title)
    pages = [lines[i:i + LINES_PER_PAGE] for i in range(0, len(lines), LINES_PER_PAGE)] or [[""]]
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for n, page in enumerate(pages):
        page_id, content_id = 4 + 2 * n, 5 + 2 * n
        kids.append(f"{page_id} 0 R")
        stream = "BT /F1 8 Tf 40 760 Td 12 TL " + " ".join(f"({_pdf_escape(line)}) Tj T*" for line in page) + " ET"
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R /Resources << /Font << /F1 3 0 R >> >> >>")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("ascii")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode("ascii")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    return out


__all__ = ["metric_rows", "render_html", "render_pdf", "text_lines", "warning_lines"]
