"""Reports: the metrics table, console reports, charts and the single-file HTML tearsheet.

The HTML report is one file with inline styles and inline SVG charts. It loads nothing from outside and links to nothing outside:
a test fails if the generated file contains an http or https address. Every marker of the template is filled with content or with
the text "n/a". Output is deterministic: no clock, no random numbers.
"""
from __future__ import annotations

import re
import html as _html
import math
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.metrics import summary as _summary
from hokma.quant.metrics.summary import _BENCH_IN, _PPY_IN, _RF_IN, _SERIES_IN, align_pair, empty, finish, prepare_series
from hokma.quant.types import QuantResult

REPORT_VERSION = "1"
NA = "n/a"
MARKERS = ("title", "date_range", "matched_dates", "params", "v", "metrics", "returns", "log_returns", "vol_returns", "eoy_title", "eoy_returns",
           "eoy_table", "monthly_dist", "returns_dist", "daily_returns", "rolling_beta", "rolling_vol", "rolling_sharpe", "rolling_sortino",
           "dd_plot", "dd_info", "dd_periods", "monthly_heatmap")
_COLORS = {"strategy": "#1f4e79", "benchmark": "#9aa5b1", "good": "#2e7d32", "bad": "#c62828", "grid": "#e3e7ec", "text": "#26323d"}
_W, _H, _ML, _MR, _MT, _MB = 900, 280, 58, 14, 24, 34


# --------------------------------------------------------------------------------------------------------------
# Template (QS-056): 23 markers, own layout, no external resources
# --------------------------------------------------------------------------------------------------------------
TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{title}}</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;color:#26323d;margin:0;background:#f5f7fa}
header{background:#1f4e79;color:#fff;padding:18px 28px}
header h1{margin:0;font-size:22px}
header p{margin:4px 0 0;font-size:13px;opacity:.85}
main{max-width:1000px;margin:0 auto;padding:18px 28px}
section{background:#fff;border:1px solid #dde3ea;border-radius:6px;padding:14px 18px;margin:14px 0}
h2{font-size:15px;margin:0 0 8px;color:#1f4e79}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{text-align:right;padding:3px 8px;border-bottom:1px solid #edf0f3}
th:first-child,td:first-child{text-align:left}
svg{width:100%;height:auto;display:block}
.note{font-size:12px;color:#5c6b7a}
footer{text-align:center;font-size:12px;color:#5c6b7a;padding:14px}
</style>
</head>
<body>
<header><h1>{{title}}</h1><p>{{date_range}} | {{matched_dates}}</p><p>{{params}}</p></header>
<main>
<section><h2>Key performance metrics</h2>{{metrics}}</section>
<section><h2>Cumulative returns</h2>{{returns}}</section>
<section><h2>Cumulative returns, log scale</h2>{{log_returns}}</section>
<section><h2>Cumulative returns, volatility matched to the benchmark</h2>{{vol_returns}}</section>
<section><h2>{{eoy_title}}</h2>{{eoy_returns}}{{eoy_table}}</section>
<section><h2>Distribution of monthly returns</h2>{{monthly_dist}}</section>
<section><h2>Distribution of daily returns</h2>{{returns_dist}}</section>
<section><h2>Daily returns</h2>{{daily_returns}}</section>
<section><h2>Rolling beta</h2>{{rolling_beta}}</section>
<section><h2>Rolling volatility</h2>{{rolling_vol}}</section>
<section><h2>Rolling Sharpe</h2>{{rolling_sharpe}}</section>
<section><h2>Rolling Sortino</h2>{{rolling_sortino}}</section>
<section><h2>Underwater (drawdown)</h2>{{dd_plot}}</section>
<section><h2>Worst drawdowns</h2>{{dd_info}}{{dd_periods}}</section>
<section><h2>Monthly returns</h2>{{monthly_heatmap}}</section>
</main>
<footer>Hokma metrics report, format {{v}}</footer>
</body>
</html>
"""


# --------------------------------------------------------------------------------------------------------------
# SVG primitives
# --------------------------------------------------------------------------------------------------------------
def _esc(text: Any) -> str:
    return _html.escape(str(text), quote=True)


def _num(v: float, kind: str = "pct") -> str:
    if v is None or not isinstance(v, (int, float, np.floating)) or not math.isfinite(float(v)):
        return NA
    return f"{float(v) * 100:,.2f}%" if kind == "pct" else f"{float(v):,.2f}"


def _frame(width: int = _W, height: int = _H) -> tuple[float, float, float, float]:
    return _ML, width - _MR, _MT, height - _MB


def _scale(values: np.ndarray, lo: float, hi: float, a: float, b: float) -> np.ndarray:
    span = hi - lo if hi > lo else 1.0
    return b - (values - lo) / span * (b - a)


def _axes(lo: float, hi: float, fmt: Callable[[float], str], x_labels: tuple[str, str]) -> str:
    x0, x1, y0, y1 = _frame()
    parts = []
    for i in range(5):
        value = lo + (hi - lo) * i / 4
        y = y1 - (y1 - y0) * i / 4
        parts.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" stroke="{_COLORS["grid"]}" stroke-width="1"/>')
        parts.append(f'<text x="{x0 - 6}" y="{y + 4:.1f}" font-size="11" text-anchor="end" fill="{_COLORS["text"]}">{_esc(fmt(value))}</text>')
    parts.append(f'<text x="{x0}" y="{_H - 10}" font-size="11" fill="{_COLORS["text"]}">{_esc(x_labels[0])}</text>')
    parts.append(f'<text x="{x1}" y="{_H - 10}" font-size="11" text-anchor="end" fill="{_COLORS["text"]}">{_esc(x_labels[1])}</text>')
    return "".join(parts)


def _wrap(body: str, title: str) -> str:
    return (f'<svg viewBox="0 0 {_W} {_H}" role="img" aria-label="{_esc(title)}"><rect width="{_W}" height="{_H}" fill="#ffffff"/>'
            f'<text x="{_ML}" y="15" font-size="12" font-weight="bold" fill="{_COLORS["text"]}">{_esc(title)}</text>{body}</svg>')


def _x_labels(index: pd.Index) -> tuple[str, str]:
    return str(index[0])[:10], str(index[-1])[:10]


def line_chart(lines: list[tuple[str, pd.Series, str]], title: str, fmt: Callable[[float], str] = _num, fill: bool = False,
               log: bool = False, zero_line: bool = False) -> str:
    """Line chart of one or more series that share a date index. Returns SVG text, or "n/a" when there is nothing to draw."""
    usable = [(n, s.dropna(), c) for n, s, c in lines if s is not None and len(s.dropna()) >= 2]
    if not usable:
        return NA
    index = max((s.index for _, s, _ in usable), key=len)
    pos = {ts: i for i, ts in enumerate(index)}
    values = np.concatenate([s.to_numpy(dtype=float) for _, s, _ in usable])
    if log:
        values = np.log(np.clip(values, 1e-12, None))
    lo, hi = float(values.min()), float(values.max())
    if zero_line:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    if hi == lo:
        hi = lo + 1.0
    x0, x1, y0, y1 = _frame()
    body = _axes(lo, hi, (lambda v: fmt(math.exp(v))) if log else fmt, _x_labels(index))
    if zero_line:
        yz = _scale(np.array([0.0]), lo, hi, y0, y1)[0]
        body += f'<line x1="{x0}" y1="{yz:.1f}" x2="{x1}" y2="{yz:.1f}" stroke="#7b8794" stroke-width="1"/>'
    n = max(len(index) - 1, 1)
    for k, (name, s, color) in enumerate(usable):
        v = s.to_numpy(dtype=float)
        if log:
            v = np.log(np.clip(v, 1e-12, None))
        xs = np.array([x0 + (x1 - x0) * pos.get(ts, 0) / n for ts in s.index])
        ys = _scale(v, lo, hi, y0, y1)
        points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        if fill and k == 0:
            yz = _scale(np.array([0.0 if zero_line else lo]), lo, hi, y0, y1)[0]
            body += f'<polygon points="{xs[0]:.1f},{yz:.1f} {points} {xs[-1]:.1f},{yz:.1f}" fill="{color}" fill-opacity="0.25" stroke="none"/>'
        body += f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.6"/>'
        body += f'<text x="{x1 - 4}" y="{y0 + 12 + 13 * k}" font-size="11" text-anchor="end" fill="{color}">{_esc(name)}</text>'
    return _wrap(body, title)


def bar_chart(labels: list[str], values: list[float], title: str, fmt: Callable[[float], str] = _num, extra: list[float] | None = None) -> str:
    """Vertical bars, green for gains and red for losses. A second set of values is drawn in grey beside the first."""
    clean = [(l, v) for l, v in zip(labels, values) if v is not None and math.isfinite(v)]
    if not clean:
        return NA
    arr = np.array([v for _, v in clean] + [e for e in (extra or []) if e is not None and math.isfinite(e)])
    lo, hi = min(0.0, float(arr.min())), max(0.0, float(arr.max()))
    if hi == lo:
        hi = lo + 1.0
    x0, x1, y0, y1 = _frame()
    body = _axes(lo, hi, fmt, (clean[0][0], clean[-1][0]))
    yz = _scale(np.array([0.0]), lo, hi, y0, y1)[0]
    slot = (x1 - x0) / len(clean)
    width = slot * (0.38 if extra else 0.7)
    for i, (label, v) in enumerate(clean):
        ys = _scale(np.array([v]), lo, hi, y0, y1)[0]
        color = _COLORS["good"] if v >= 0 else _COLORS["bad"]
        x = x0 + slot * i + slot * 0.1
        body += f'<rect x="{x:.1f}" y="{min(ys, yz):.1f}" width="{width:.1f}" height="{abs(ys - yz):.1f}" fill="{color}"><title>{_esc(label)}: {_esc(fmt(v))}</title></rect>'
        if extra and i < len(extra) and extra[i] is not None and math.isfinite(extra[i]):
            ye = _scale(np.array([extra[i]]), lo, hi, y0, y1)[0]
            body += f'<rect x="{x + width + 1:.1f}" y="{min(ye, yz):.1f}" width="{width:.1f}" height="{abs(ye - yz):.1f}" fill="{_COLORS["benchmark"]}"/>'
    body += f'<line x1="{x0}" y1="{yz:.1f}" x2="{x1}" y2="{yz:.1f}" stroke="#7b8794" stroke-width="1"/>'
    return _wrap(body, title)


def histogram(values: pd.Series, title: str, bins: int = 30) -> str:
    """Histogram of a series with the mean marked."""
    v = values.dropna().to_numpy(dtype=float)
    if len(v) < 5 or float(v.max()) == float(v.min()):
        return NA
    counts, edges = np.histogram(v, bins=bins)
    x0, x1, y0, y1 = _frame()
    top = float(counts.max())
    body = _axes(0.0, top, lambda c: f"{c:,.0f}", (_num(edges[0]), _num(edges[-1])))
    slot = (x1 - x0) / len(counts)
    for i, c in enumerate(counts):
        ys = _scale(np.array([float(c)]), 0.0, top, y0, y1)[0]
        body += f'<rect x="{x0 + slot * i:.1f}" y="{ys:.1f}" width="{slot - 1:.1f}" height="{y1 - ys:.1f}" fill="{_COLORS["strategy"]}" fill-opacity="0.8"/>'
    xm = x0 + (float(v.mean()) - edges[0]) / (edges[-1] - edges[0]) * (x1 - x0)
    body += f'<line x1="{xm:.1f}" y1="{y0}" x2="{xm:.1f}" y2="{y1}" stroke="{_COLORS["bad"]}" stroke-dasharray="4 3"/>'
    return _wrap(body, title)


def _diverging(value: float, bound: float) -> str:
    t = 0.0 if bound == 0 else max(-1.0, min(1.0, value / bound))
    if t >= 0:
        r, g, b = (int(255 - 150 * t), int(255 - 50 * t), int(255 - 150 * t))
    else:
        r, g, b = (int(255 + 60 * t), int(255 + 150 * t), int(255 + 150 * t))
    return f"#{r:02x}{g:02x}{b:02x}"


def heatmap(table: pd.DataFrame, title: str, fmt: Callable[[float], str] = _num) -> str:
    """Grid of coloured cells: rows are table.index, columns are table.columns. Green is positive, red is negative."""
    if table is None or table.empty or not np.isfinite(table.to_numpy(dtype=float)).any():
        return NA
    rows, cols = len(table.index), len(table.columns)
    cell_w = (_W - _ML - _MR) / cols
    cell_h = min(26.0, (_H - _MT - 10) / rows)
    height = int(_MT + cell_h * rows + 14)
    bound = float(np.nanmax(np.abs(table.to_numpy(dtype=float))))
    body = []
    for j, col in enumerate(table.columns):
        body.append(f'<text x="{_ML + cell_w * (j + 0.5):.1f}" y="{_MT - 4}" font-size="11" text-anchor="middle" fill="{_COLORS["text"]}">{_esc(col)}</text>')
    for i, idx in enumerate(table.index):
        y = _MT + cell_h * i
        body.append(f'<text x="{_ML - 6}" y="{y + cell_h / 2 + 4:.1f}" font-size="11" text-anchor="end" fill="{_COLORS["text"]}">{_esc(idx)}</text>')
        for j, col in enumerate(table.columns):
            v = table.iloc[i, j]
            if v is None or not math.isfinite(float(v)):
                continue
            body.append(f'<rect x="{_ML + cell_w * j:.1f}" y="{y:.1f}" width="{cell_w - 1:.1f}" height="{cell_h - 1:.1f}" fill="{_diverging(float(v), bound)}"/>')
            body.append(f'<text x="{_ML + cell_w * (j + 0.5):.1f}" y="{y + cell_h / 2 + 4:.1f}" font-size="10" text-anchor="middle" fill="{_COLORS["text"]}">{_esc(fmt(float(v)))}</text>')
    return (f'<svg viewBox="0 0 {_W} {height}" role="img" aria-label="{_esc(title)}"><rect width="{_W}" height="{height}" fill="#ffffff"/>'
            f'<text x="{_ML}" y="13" font-size="12" font-weight="bold" fill="{_COLORS["text"]}">{_esc(title)}</text>{"".join(body)}</svg>')


# --------------------------------------------------------------------------------------------------------------
# Data for the charts
# --------------------------------------------------------------------------------------------------------------
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _period(r: pd.Series, rule: str) -> pd.Series:
    return (1.0 + r).resample(rule).prod() - 1.0


def monthly_table(r: pd.Series) -> pd.DataFrame:
    monthly = _period(r, "ME")
    table = pd.DataFrame({"year": monthly.index.year, "month": monthly.index.month, "value": monthly.to_numpy()})
    grid = table.pivot(index="year", columns="month", values="value").reindex(columns=range(1, 13))
    grid.columns = _MONTHS
    return grid


def _window(n: int, wanted: int = 126) -> int:
    return min(wanted, max(10, n // 3))


def _rolling(r: pd.Series, b: pd.Series | None, kind: str, ppy: int) -> pd.Series | None:
    n = len(r)
    if n < 30:
        return None
    w = _window(n)
    if kind == "vol":
        return r.rolling(w).std() * math.sqrt(ppy)
    if kind == "sharpe":
        return r.rolling(w).mean() / r.rolling(w).std() * math.sqrt(ppy)
    if kind == "sortino":
        downside = np.sqrt((np.minimum(r, 0.0) ** 2).rolling(w).mean())
        return r.rolling(w).mean() / downside.replace(0.0, np.nan) * math.sqrt(ppy)
    if kind == "beta" and b is not None:
        return r.rolling(w).cov(b) / b.rolling(w).var()
    return None


def _clean_chart_series(s: pd.Series | None) -> pd.Series | None:
    if s is None:
        return None
    s = s.replace([np.inf, -np.inf], np.nan).dropna()
    return s if len(s) >= 2 else None


class Context:
    """Prepared inputs shared by every chart of one report."""

    def __init__(self, r: pd.Series, b: pd.Series | None, rf: float, ppy: int) -> None:
        self.r, self.b, self.rf, self.ppy = r, b, rf, ppy


def _plot_returns(c: Context) -> str:
    lines = [("Strategy", (1 + c.r).cumprod() - 1, _COLORS["strategy"])]
    if c.b is not None:
        lines.append(("Benchmark", (1 + c.b).cumprod() - 1, _COLORS["benchmark"]))
    return line_chart(lines, "Cumulative return", zero_line=True)


def _plot_log_returns(c: Context) -> str:
    lines = [("Strategy", (1 + c.r).cumprod(), _COLORS["strategy"])]
    if c.b is not None:
        lines.append(("Benchmark", (1 + c.b).cumprod(), _COLORS["benchmark"]))
    return line_chart(lines, "Growth of 1 (log scale)", fmt=lambda v: f"{v:,.2f}", log=True)


def _plot_vol_returns(c: Context) -> str:
    if c.b is None:
        return NA
    sd_r, sd_b = float(c.r.std(ddof=1)), float(c.b.std(ddof=1))
    if sd_r == 0 or sd_b == 0:
        return NA
    scaled = c.r * (sd_b / sd_r)
    return line_chart([("Strategy at benchmark volatility", (1 + scaled).cumprod() - 1, _COLORS["strategy"]),
                       ("Benchmark", (1 + c.b).cumprod() - 1, _COLORS["benchmark"])], "Cumulative return at matched volatility", zero_line=True)


def _plot_eoy(c: Context) -> str:
    yearly = _period(c.r, "YE")
    extra = None
    if c.b is not None:
        by = _period(c.b, "YE").reindex(yearly.index)
        extra = by.tolist()
    return bar_chart([str(i.year) for i in yearly.index], yearly.tolist(), "Returns by year", extra=extra)


def _plot_monthly_dist(c: Context) -> str:
    return histogram(_period(c.r, "ME"), "Monthly returns", bins=15)


def _plot_returns_dist(c: Context) -> str:
    return histogram(c.r, "Daily returns")


def _plot_daily(c: Context) -> str:
    return line_chart([("Daily return", c.r, _COLORS["strategy"])], "Daily returns", zero_line=True)


def _rolling_plot(kind: str, title: str, fmt: Callable[[float], str]) -> Callable[[Context], str]:
    def plot(c: Context) -> str:
        if kind == "beta" and c.b is None:
            return NA
        s = _clean_chart_series(_rolling(c.r, c.b, kind, c.ppy))
        return NA if s is None else line_chart([(title, s, _COLORS["strategy"])], title, fmt=fmt, zero_line=True)
    return plot


def _plot_dd(c: Context) -> str:
    return line_chart([("Drawdown", helpers.drawdown_series(c.r), _COLORS["bad"])], "Drawdown", fill=True, zero_line=True)


def _plot_dd_periods(c: Context) -> str:
    episodes = _summary.drawdown_episodes(c.r)
    if episodes.empty:
        return NA
    worst = episodes.sort_values("depth").head(5)
    labels = [f"{str(row.start)[:10]} to {str(row.end)[:10]}" for row in worst.itertuples()]
    return bar_chart(labels, worst["depth"].tolist(), "Five deepest drawdowns")


def _plot_heatmap(c: Context) -> str:
    return heatmap(monthly_table(c.r), "Monthly returns")


def _plot_monthly_bars(c: Context) -> str:
    monthly = _period(c.r, "ME")
    return bar_chart([str(i)[:7] for i in monthly.index], monthly.tolist(), "Monthly returns")


def _plot_quantiles(c: Context) -> str:
    q = c.r.quantile([0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
    return bar_chart([f"p{int(round(k * 100))}" for k in q.index], q.tolist(), "Daily return quantiles")


def _plot_corr(c: Context) -> str:
    if c.b is None:
        return NA
    return corr_heatmap(pd.DataFrame({"Strategy": c.r, "Benchmark": c.b}))


def corr_heatmap(frame: pd.DataFrame) -> str:
    """Correlation heatmap of the columns of a frame of returns."""
    if not isinstance(frame, pd.DataFrame):
        raise BadInput(f"frame: expected a DataFrame, got {type(frame).__name__}")
    corr = frame.astype(float).corr()
    return heatmap(corr, "Correlation of returns", fmt=lambda v: f"{v:.2f}")


CHARTS: dict[str, Callable[[Context], str]] = {
    "returns": _plot_returns, "log_returns": _plot_log_returns, "vol_returns": _plot_vol_returns, "eoy_returns": _plot_eoy,
    "monthly_dist": _plot_monthly_dist, "returns_dist": _plot_returns_dist, "daily_returns": _plot_daily,
    "rolling_beta": _rolling_plot("beta", "Rolling beta", lambda v: f"{v:.2f}"),
    "rolling_vol": _rolling_plot("vol", "Rolling volatility", _num),
    "rolling_sharpe": _rolling_plot("sharpe", "Rolling Sharpe", lambda v: f"{v:.2f}"),
    "rolling_sortino": _rolling_plot("sortino", "Rolling Sortino", lambda v: f"{v:.2f}"),
    "dd_plot": _plot_dd, "dd_periods": _plot_dd_periods, "monthly_heatmap": _plot_heatmap, "monthly_bars": _plot_monthly_bars,
    "return_quantiles": _plot_quantiles, "correlation": _plot_corr,
}


# --------------------------------------------------------------------------------------------------------------
# QS-054: metrics table
# --------------------------------------------------------------------------------------------------------------
_ROWS: list[tuple[str, str, str, float]] = [
    ("Start Period", "header", "start", 1), ("End Period", "header", "end", 1), ("Risk-Free Rate %", "header", "risk_free_rate", 100),
    ("Time in Market %", "header", "time_in_market", 100),
    ("Cumulative Return %", "returns", "cumulative_return", 100), ("Total Return %", "returns", "total_return", 100), ("CAGR %", "returns", "cagr", 100),
    ("Sharpe", "ratios", "sharpe", 1), ("Prob. Sharpe Ratio %", "psr", "", 100), ("Smart Sharpe", "ratios", "smart_sharpe", 1),
    ("Sortino", "ratios", "sortino", 1), ("Smart Sortino", "ratios", "smart_sortino", 1), ("Sortino/sqrt(2)", "ratios", "sortino_over_sqrt2", 1),
    ("Smart Sortino/sqrt(2)", "ratios", "smart_sortino_over_sqrt2", 1), ("Omega", "ratios", "omega", 1),
    ("Max Drawdown %", "drawdown", "max_drawdown", 100), ("Max DD Date", "drawdown", "max_dd_date", 1), ("Max DD Period Start", "drawdown", "max_dd_start", 1),
    ("Max DD Period End", "drawdown", "max_dd_end", 1), ("Longest DD Days", "drawdown", "longest_dd_days", 1), ("Avg. Drawdown %", "drawdown", "avg_drawdown", 100),
    ("Avg. Drawdown Days", "drawdown", "avg_drawdown_days", 1),
    ("Volatility (ann.) %", "volatility", "volatility_annual", 100), ("R^2", "volatility", "r_squared", 1), ("Information Ratio", "volatility", "information_ratio", 1),
    ("Calmar", "volatility", "calmar", 1), ("Skew", "volatility", "skew", 1), ("Kurtosis", "volatility", "kurtosis", 1),
    ("Ulcer Performance Index", "volatility", "ulcer_performance_index", 1), ("Risk-Adjusted Return %", "volatility", "risk_adjusted_return", 100),
    ("Risk-Return Ratio", "volatility", "risk_return_ratio", 1),
    ("Avg. Return %", "operations", "avg_return", 100), ("Avg. Win %", "operations", "avg_win", 100), ("Avg. Loss %", "operations", "avg_loss", 100),
    ("Win/Loss Ratio", "operations", "win_loss_ratio", 1), ("Profit Ratio", "operations", "profit_ratio", 1),
    ("Expected Daily %", "expectations", "expected_daily", 100), ("Expected Monthly %", "expectations", "expected_monthly", 100),
    ("Expected Yearly %", "expectations", "expected_yearly", 100), ("Kelly Criterion %", "expectations", "kelly_criterion", 100),
    ("Risk of Ruin %", "ruin", "", 100), ("Daily Value-at-Risk %", "expectations", "daily_var", 100),
    ("Expected Shortfall (cVaR) %", "expectations", "expected_shortfall", 100),
    ("Max Consecutive Wins", "streaks", "max_consecutive_wins", 1), ("Max Consecutive Losses", "streaks", "max_consecutive_losses", 1),
    ("Gain/Pain Ratio", "streaks", "gain_pain_ratio", 1), ("Gain/Pain (1M)", "streaks", "gain_pain_ratio_1m", 1), ("Payoff Ratio", "streaks", "payoff_ratio", 1),
    ("Profit Factor", "streaks", "profit_factor", 1), ("Common Sense Ratio", "streaks", "common_sense_ratio", 1), ("CPC Index", "streaks", "cpc_index", 1),
    ("Tail Ratio", "streaks", "tail_ratio", 1), ("Outlier Win Ratio", "streaks", "outlier_win_ratio", 1), ("Outlier Loss Ratio", "streaks", "outlier_loss_ratio", 1),
    ("MTD %", "periods", "mtd", 100), ("3M %", "periods", "3m", 100), ("6M %", "periods", "6m", 100), ("YTD %", "periods", "ytd", 100), ("1Y %", "periods", "1y", 100),
    ("3Y (ann.) %", "periods", "3y_annualised", 100), ("5Y (ann.) %", "periods", "5y_annualised", 100), ("10Y (ann.) %", "periods", "10y_annualised", 100),
    ("All-time (ann.) %", "periods", "all_time_annualised", 100),
    ("Recovery Factor", "quality", "recovery_factor", 1), ("Ulcer Index", "quality", "ulcer_index", 1), ("Serenity Index", "quality", "serenity_index", 1),
]
_BASIC = {"Start Period", "End Period", "Risk-Free Rate %", "Time in Market %", "Cumulative Return %", "CAGR %", "Sharpe", "Prob. Sharpe Ratio %", "Sortino",
          "Max Drawdown %", "Longest DD Days", "Volatility (ann.) %", "Win/Loss Ratio", "Risk of Ruin %", "Daily Value-at-Risk %", "Payoff Ratio", "Profit Factor",
          "MTD %", "YTD %", "1Y %", "Recovery Factor", "Serenity Index", "Beta", "Alpha", "Correlation", "Treynor Ratio"}
_VERSUS = [("Beta", "beta", 1), ("Alpha", "alpha", 1), ("Correlation", "correlation", 1), ("Treynor Ratio", "treynor_ratio", 1)]


def _column(blocks: dict[str, QuantResult], scale_rows: list[tuple[str, str, str, float]] = _ROWS) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, block, key, scale in scale_rows:
        res = blocks.get(block)
        value = None if res is None else res.value
        if key and isinstance(value, dict):
            value = value.get(key)
        elif key:
            value = None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and scale != 1:
            value = value * scale
        out[label] = value
    return out


def metrics_frame(returns: Any, benchmark: Any = None, rf: float = 0.0, periods_per_year: int = 252, mode: str = "full") -> tuple[pd.DataFrame | None, tuple[str, ...]]:
    """Metrics as a table: one row per metric, a Strategy column and, with a benchmark, a Benchmark column.
    The probabilistic Sharpe row comes from validation.psr, never from a library's own formula."""
    if mode not in ("basic", "full"):
        raise BadInput("mode must be 'basic' or 'full'")
    r, warns = prepare_series(returns)
    if r is None:
        return None, warns
    b = None
    if benchmark is not None:
        r2, b2, bw = align_pair(r, benchmark)
        warns = tuple(warns) + tuple(bw)
        if r2 is not None:
            r, b = r2, b2
    blocks = _summary.build_summary(r, benchmark=b, rf=rf, periods_per_year=periods_per_year)
    for res in blocks.values():
        warns = tuple(warns) + tuple(w for w in res.warnings if w not in warns and not w.startswith("input has no date index"))
    strategy = _column(blocks)
    versus = blocks.get("versus_benchmark")
    for label, key, _ in _VERSUS:
        strategy[label] = None if versus is None or versus.value is None else versus.value.get(key)
    data = {"Strategy": strategy}
    if b is not None:
        bench_blocks = _summary.build_summary(b, rf=rf, periods_per_year=periods_per_year)
        bench = _column(bench_blocks)
        for label, _, _ in _VERSUS:
            bench[label] = None
        data["Benchmark"] = bench
    frame = pd.DataFrame(data, dtype=object)
    if mode == "basic":
        frame = frame.loc[[i for i in frame.index if i in _BASIC]]
    return frame, tuple(dict.fromkeys(warns))


def _cell(v: Any) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return NA
    if isinstance(v, (int, np.integer)) and not isinstance(v, bool):
        return f"{int(v):,}"
    if isinstance(v, (float, np.floating)):
        return f"{float(v):,.2f}"
    return str(v)


def _table_html(frame: pd.DataFrame) -> str:
    head = "".join(f"<th>{_esc(c)}</th>" for c in ["Metric", *frame.columns])
    body = "".join("<tr><td>" + _esc(idx) + "</td>" + "".join(f"<td>{_esc(_cell(v))}</td>" for v in row) + "</tr>" for idx, row in zip(frame.index, frame.itertuples(index=False)))
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _table_text(frame: pd.DataFrame) -> str:
    width = max(len(str(i)) for i in frame.index) + 2
    cols = [str(c) for c in frame.columns]
    lines = ["Metric".ljust(width) + "".join(c.rjust(16) for c in cols), "-" * (width + 16 * len(cols))]
    for idx, row in zip(frame.index, frame.itertuples(index=False)):
        lines.append(str(idx).ljust(width) + "".join(_cell(v).rjust(16) for v in row))
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------------------
# QS-051, QS-056, CC-15: the single-file HTML report
# --------------------------------------------------------------------------------------------------------------
def _eoy_table(r: pd.Series, b: pd.Series | None) -> str:
    yearly = _period(r, "YE")
    cols: dict[str, list[Any]] = {"Year": [str(i.year) for i in yearly.index], "Strategy": [_num(v) for v in yearly]}
    if b is not None:
        by = _period(b, "YE").reindex(yearly.index)
        cols["Benchmark"] = [_num(v) for v in by]
        cols["Difference"] = [_num(x - y) if pd.notna(y) else NA for x, y in zip(yearly, by)]
    head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
    rows = "".join("<tr>" + "".join(f"<td>{_esc(cols[c][i])}</td>" for c in cols) + "</tr>" for i in range(len(yearly)))
    return f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"


def _dd_table(r: pd.Series) -> str:
    episodes = _summary.drawdown_episodes(r)
    if episodes.empty:
        return NA
    worst = episodes.sort_values("depth").head(10)
    rows = "".join(f"<tr><td>{_esc(str(e.start)[:10])}</td><td>{_esc(str(e.valley)[:10])}</td><td>{_esc(str(e.end)[:10])}</td><td>{e.days}</td><td>{_esc(_num(e.depth))}</td></tr>"
                   for e in worst.itertuples())
    return "<table><thead><tr><th>Started</th><th>Valley</th><th>Ended</th><th>Days</th><th>Depth</th></tr></thead><tbody>" + rows + "</tbody></table>"


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_TEMPLATE_NAME = re.compile(r"^[a-z0-9_-]{1,40}$")
_UNKNOWN_TEMPLATE = "template: unknown template"


def load_template(name: str | None) -> str:
    """The built-in layout, or an internal template by name from the fixed folder. A path is never read: whatever the name
    is, an unknown template gets the same message whether a file of that name exists or not."""
    if name is None or name == "default":
        return TEMPLATE
    if not isinstance(name, str) or not _TEMPLATE_NAME.match(name):
        raise BadInput(_UNKNOWN_TEMPLATE)
    candidate = (TEMPLATE_DIR / f"{name}.html").resolve()
    if not candidate.is_relative_to(TEMPLATE_DIR.resolve()) or candidate.suffix != ".html" or not candidate.is_file():
        raise BadInput(_UNKNOWN_TEMPLATE)
    return candidate.read_text(encoding="utf-8")


def render_html(returns: Any, benchmark: Any = None, title: str = "Performance report", rf: float = 0.0, periods_per_year: int = 252,
                template: str | None = None) -> tuple[str | None, tuple[str, ...]]:
    """The report as one HTML string, or None and the reasons. `template` names one of the internal layouts; it uses the same markers."""
    if isinstance(rf, bool) or not isinstance(rf, (int, float)) or not math.isfinite(rf):
        raise BadInput("rf must be a finite number")
    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int) or not 1 <= periods_per_year <= 100_000:
        raise BadInput("periods_per_year must be a positive integer")
    r, warns = prepare_series(returns, min_obs=5)
    if r is None:
        return None, warns
    b = None
    matched = "no benchmark"
    if benchmark is not None:
        r2, b2, bw = align_pair(r, benchmark, min_obs=5)
        warns = tuple(warns) + tuple(bw)
        if r2 is not None:
            r, b = r2, b2
            matched = f"benchmark matched on {len(r)} dates"
    template = load_template(template)
    frame, mw = metrics_frame(r, b, rf, periods_per_year, "full")
    ctx = Context(r, b, rf, periods_per_year)
    values: dict[str, str] = {
        "title": _esc(title), "date_range": _esc(f"{str(r.index[0])[:10]} to {str(r.index[-1])[:10]}"), "matched_dates": _esc(matched),
        "params": _esc(f"risk-free rate {rf * 100:.2f}% | {periods_per_year} periods per year | {len(r)} observations"), "v": _esc(REPORT_VERSION),
        "metrics": NA if frame is None else _table_html(frame),
        "eoy_title": _esc("End of year returns vs benchmark" if b is not None else "End of year returns"),
        "eoy_table": _eoy_table(r, b), "dd_info": _dd_table(r),
    }
    for key in ("returns", "log_returns", "vol_returns", "eoy_returns", "monthly_dist", "returns_dist", "daily_returns", "rolling_beta", "rolling_vol",
                "rolling_sharpe", "rolling_sortino", "dd_plot", "dd_periods", "monthly_heatmap"):
        values[key] = CHARTS[key](ctx)
    out = template
    for marker in MARKERS:
        out = out.replace("{{" + marker + "}}", values.get(marker, NA) or NA)
    return out, tuple(dict.fromkeys(tuple(warns) + tuple(mw)))


def reference_series(n: int = 504, seed: int = 20261122) -> tuple[pd.Series, pd.Series]:
    """A fixed pair of return series (strategy and benchmark) used to show the layout of a report."""
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2022-01-03", periods=n)
    bench = pd.Series(rng.normal(0.0004, 0.010, n), index=index)
    strategy = pd.Series(0.0003 + 0.8 * bench.to_numpy() + rng.normal(0.0, 0.006, n), index=index)
    return strategy, bench


# --------------------------------------------------------------------------------------------------------------
# Registered functions
# --------------------------------------------------------------------------------------------------------------
_REPORT_IN = {"returns": _SERIES_IN, "benchmark": _BENCH_IN, "rf": _RF_IN, "periods_per_year": _PPY_IN}


@registry.register("metrics.report_html", module="metrics",
                   summary="One-file HTML report with inline charts, the metrics table and drawdown tables. No external resources. An optional internal template is chosen by name.",
                   inputs={**_REPORT_IN, "title": {"type": "str", "required": False, "default": "Performance report"},
                           "template": {"type": "str", "required": False, "default": None}}, piece_ids=("QS-051", "QS-056", "CC-15", "QS-057"))
def report_html(returns: Any, benchmark: Any = None, rf: float = 0.0, periods_per_year: int = 252, title: str = "Performance report",
                template: str | None = None) -> QuantResult:
    name, pieces, method = "metrics.report_html", ("QS-051", "QS-056", "CC-15", "QS-057"), "template with 23 markers filled from the metric blocks and inline SVG charts"
    params = {"rf": rf, "periods_per_year": periods_per_year, "title": title, "template": template}
    text, warns = render_html(returns, benchmark, title, rf, periods_per_year, template)
    if text is None:
        return empty(name, "html", method, warns, pieces, params)
    r, _ = prepare_series(returns, min_obs=5)
    return finish(name, {"html": text, "bytes": len(text.encode("utf-8"))}, unit="html", method=method, series=r, pieces=pieces, params=params, warnings=warns)


@registry.register("metrics.report_example", module="metrics", summary="The report layout built from a fixed reference pair of series, to show what a report looks like.",
                   inputs={"title": {"type": "str", "required": False, "default": "Example tearsheet"}}, piece_ids=("QS-062",))
def report_example(title: str = "Example tearsheet") -> QuantResult:
    strategy, bench = reference_series()
    res = report_html(strategy, bench, title=title)
    return QuantResult(name="metrics.report_example", value=res.value, unit=res.unit, method="report built from the fixed reference series", params=dict(res.params),
                       inputs_hash=res.inputs_hash, as_of=res.as_of, warnings=res.warnings, piece_ids=("QS-062",))


@registry.register("metrics.report_metrics", module="metrics",
                   summary="The metrics table: one row per metric, a column for the strategy and one for the benchmark. The probabilistic Sharpe row comes from validation.psr.",
                   inputs={**_REPORT_IN, "mode": {"type": "str", "required": False, "default": "full"}}, piece_ids=("QS-054",))
def report_metrics(returns: Any, benchmark: Any = None, rf: float = 0.0, periods_per_year: int = 252, mode: str = "full") -> QuantResult:
    name, pieces, method = "metrics.report_metrics", ("QS-054",), "summary blocks laid out as a table"
    params = {"rf": rf, "periods_per_year": periods_per_year, "mode": mode}
    frame, warns = metrics_frame(returns, benchmark, rf, periods_per_year, mode)
    if frame is None:
        return empty(name, "mixed", method, warns, pieces, params)
    r, _ = prepare_series(returns)
    value = {str(idx): {str(col): frame.loc[idx, col] for col in frame.columns} for idx in frame.index}
    return finish(name, value, unit="mixed", method=method, series=r, pieces=pieces, params=params, warnings=warns)


def _console(returns: Any, benchmark: Any, rf: float, ppy: int, mode: str, name: str, pieces: tuple[str, ...]) -> QuantResult:
    method = f"{mode} console report"
    params = {"rf": rf, "periods_per_year": ppy, "mode": mode}
    frame, warns = metrics_frame(returns, benchmark, rf, ppy, mode)
    if frame is None:
        return empty(name, "text", method, warns, pieces, params)
    r, _ = prepare_series(returns)
    return finish(name, {"text": _table_text(frame)}, unit="text", method=method, series=r, pieces=pieces, params=params, warnings=warns)


@registry.register("metrics.report_full", module="metrics", summary="Full console report: every metric as fixed-width text.",
                   inputs=_REPORT_IN, piece_ids=("QS-052",))
def report_full(returns: Any, benchmark: Any = None, rf: float = 0.0, periods_per_year: int = 252) -> QuantResult:
    return _console(returns, benchmark, rf, periods_per_year, "full", "metrics.report_full", ("QS-052",))


@registry.register("metrics.report_basic", module="metrics", summary="Short console report for a quick review: the essential metrics as fixed-width text.",
                   inputs=_REPORT_IN, piece_ids=("QS-053",))
def report_basic(returns: Any, benchmark: Any = None, rf: float = 0.0, periods_per_year: int = 252) -> QuantResult:
    return _console(returns, benchmark, rf, periods_per_year, "basic", "metrics.report_basic", ("QS-053",))


@registry.register("metrics.report_plots", module="metrics", summary="Every chart of the report as a separate inline SVG, keyed by chart name.",
                   inputs={**_REPORT_IN, "names": {"type": "list", "required": False, "default": None}}, piece_ids=("QS-055", "QS-057", "FF-020"))
def report_plots(returns: Any, benchmark: Any = None, rf: float = 0.0, periods_per_year: int = 252, names: list[str] | None = None) -> QuantResult:
    name, pieces, method = "metrics.report_plots", ("QS-055", "QS-057", "FF-020"), "inline SVG charts"
    params = {"rf": rf, "periods_per_year": periods_per_year, "names": names}
    chosen = list(CHARTS) if names is None else list(names)
    unknown = [n for n in chosen if n not in CHARTS]
    if unknown:
        raise BadInput(f"unknown chart(s) {unknown}; available: {sorted(CHARTS)}")
    r, warns = prepare_series(returns, min_obs=5)
    if r is None:
        return empty(name, "svg", method, warns, pieces, params)
    b = None
    if benchmark is not None:
        r2, b2, bw = align_pair(r, benchmark, min_obs=5)
        warns = tuple(warns) + tuple(bw)
        if r2 is not None:
            r, b = r2, b2
    ctx = Context(r, b, rf, periods_per_year)
    return finish(name, {n: CHARTS[n](ctx) for n in chosen}, unit="svg", method=method, series=r, pieces=pieces, params=params, warnings=warns)


@registry.register("metrics.report_heatmap", module="metrics",
                   summary="Heatmaps: monthly returns by year (one series) or the correlation of the columns of a frame of returns.",
                   inputs={"returns": {"type": "series or dataframe", "required": True}}, piece_ids=("FF-020",))
def report_heatmap(returns: Any) -> QuantResult:
    name, pieces, method = "metrics.report_heatmap", ("FF-020",), "coloured grid in inline SVG"
    if isinstance(returns, pd.DataFrame) and returns.shape[1] > 1:
        frame = returns.astype(float)
        if len(frame.dropna()) < 3:
            return empty(name, "svg", method, ("fewer than 3 shared observations",), pieces)
        return finish(name, {"svg": corr_heatmap(frame), "kind": "correlation"}, unit="svg", method=method, series=None, pieces=pieces, hash_extra=(frame,))
    r, warns = prepare_series(returns, min_obs=5)
    if r is None:
        return empty(name, "svg", method, warns, pieces)
    return finish(name, {"svg": heatmap(monthly_table(r), "Monthly returns"), "kind": "monthly"}, unit="svg", method=method, series=r, pieces=pieces, warnings=warns)
