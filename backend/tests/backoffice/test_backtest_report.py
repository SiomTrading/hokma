"""Report of a backtest run (SPEC-020: L-39): curve, metrics, trades and warnings, HTML escaped, and a PDF read back through the statements reader."""
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pandas as pd
import pytest

from hokma.backoffice import statements
from hokma.backoffice.reports import backtest_report
from hokma.backtest import results
from hokma.backtest.results import BacktestResult, EquityCurve, Trade

UTC = timezone.utc
FIXTURE = Path(__file__).resolve().parents[1] / "quant" / "fixtures" / "returns_daily.csv"
NQ = "FUT:NQ:2026-03"


def curve() -> EquityCurve:
    r = pd.read_csv(FIXTURE, parse_dates=["date"], index_col="date")["return"].iloc[:120]
    wealth = (1.0 + r).cumprod() * 100000.0
    return EquityCurve(tuple(i.to_pydatetime().replace(hour=21, tzinfo=UTC) for i in wealth.index), tuple(D(repr(round(v, 2))) for v in wealth))


def trades(n=3, tag="orb_long"):
    t0 = datetime(2021, 2, 1, 15, 0, tzinfo=UTC)
    return tuple(Trade(NQ, "long", D(1), t0 + timedelta(hours=i), D("20000"), t0 + timedelta(hours=i, minutes=30), D("20010"), D(100 - 150 * (i % 2)), D("4.5"),
                       D(-40), D(120), tag, "target", {"rsi": 55.0}) for i in range(n))


def make(*, warnings=("historical_margin_missing",), ambiguous=(datetime(2021, 3, 3, 15, 1, tzinfo=UTC),), tag="orb_long", n=3) -> BacktestResult:
    base = BacktestResult(7, "done", "spec-abc", "data-def", "engine-1", 5, trades(n, tag), curve(), D(250), D(-30), {"commission": D("13.5")}, len(ambiguous), {}, {},
                          tuple(warnings), tuple(ambiguous))
    return results.with_stats(base, multipliers={NQ: D(20)})


def test_the_report_has_curve_metrics_trades_and_warnings_L_39():
    html = backtest_report.render_html(make(), title="NQ run 7")
    assert html.startswith("<!doctype html>") and "<h2>Equity curve</h2>" in html and html.count("<svg") == 2        # the curve and the drawdown
    assert "<h2>Metrics</h2>" in html and "<td>ratios</td>" in html and "<td>trade_stats</td><td>win_rate</td>" in html
    assert "<h2>Trades</h2>" in html and html.count("<td>orb_long</td>") == 3 and "<td>-40</td><td>120</td>" in html
    assert "Ambiguous bars (stop and target touched in the same bar): 1 at 2021-03-03T15:01:00+00:00" in html          # the count and the date
    assert "Historical margin is missing" in html
    assert "Spec spec-abc" in html and "engine engine-1" in html and "seed 5" in html and "Costs USD: commission 13.5" in html


def test_a_run_without_ambiguity_still_says_zero_and_has_no_margin_notice_L_39():
    html = backtest_report.render_html(make(warnings=(), ambiguous=()), title="t")
    assert "touched in the same bar): 0<" in html and "Historical margin" not in html


def test_every_text_of_the_result_is_escaped_and_nothing_is_loaded_from_outside_L_39():
    bad = "<script>alert(1)</script>"
    html = backtest_report.render_html(make(tag=bad, warnings=("<img src=x onerror=1>",)), title="<b>" + bad)
    assert bad not in html and "<img src=x" not in html and "&lt;script&gt;" in html and "&lt;img src=x" in html
    assert not re.search(r"<(script|link|img|iframe)\b|@import|https?://|<[^>]* src=", html)


def test_a_long_list_of_trades_is_cut_and_says_so_L_39():
    html = backtest_report.render_html(make(n=backtest_report.MAX_TRADES + 5), title="t")
    assert html.count("<td>FUT:NQ:2026-03</td>") == backtest_report.MAX_TRADES and f"{backtest_report.MAX_TRADES} of {backtest_report.MAX_TRADES + 5} trades shown" in html


def test_the_pdf_is_text_read_back_by_the_statements_reader_L_39():
    result = make()
    pdf = backtest_report.render_pdf(result, title="NQ run 7 (draft)")
    read = statements.read_statement_pdf(pdf)
    assert read.ok and read.code is None
    assert "NQ run 7 (draft)" in read.text and "Spec spec-abc" in read.text and "Ambiguous bars" in read.text and "1 at 2021-03-03T15:01:00+00:00" in read.text
    assert "Historical margin is missing" in read.text and "FUT:NQ:2026-03 long 1" in read.text and "orb_long target" in read.text
    many = statements.read_statement_pdf(backtest_report.render_pdf(make(n=120), title="t"))
    assert many.ok and len(many.pages) > 1                                                # more than one page when the report is long
    assert pdf.startswith(b"%PDF") and pdf.rstrip().endswith(b"%%EOF")
