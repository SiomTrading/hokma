"""Charts of an account as ONE self-contained HTML file (SPEC-018: AS-016): the equity curve, the drawdown and the composition of the book.
The recipe is the module's own (inline SVG built from Decimal figures); the page loads nothing from outside (no script, no style sheet,
no font, no image link) and every text that comes from data is escaped. The file is returned as text: where it is saved is the caller's
decision, through the helpers of the desk folder."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from typing import Any

from hokma.exec.money import ZERO, dec

WIDTH, HEIGHT, PAD = 640, 220, 28
MAX_POINTS = 5000
MAX_SLICES = 40
_CENT = Decimal("0.01")
_PALETTE = ("#2f6fed", "#e8590c", "#2b8a3e", "#ae3ec9", "#f08c00", "#0c8599", "#c92a2a", "#5f3dc4")
CSS = ("body{font:14px system-ui,sans-serif;margin:24px;color:#222}h1{font-size:20px}h2{font-size:16px;margin-top:28px}"
       "svg{border:1px solid #ddd;background:#fff}.axis{stroke:#999;stroke-width:1}.curve{fill:none;stroke:#2f6fed;stroke-width:2}"
       ".dd{fill:#c92a2a33;stroke:#c92a2a;stroke-width:1.5}.note{color:#666;font-size:12px}td,th{padding:2px 10px;text-align:left}")


class ChartError(ValueError):
    """The data of a chart is not usable."""


def _num(value: Decimal) -> str:
    return format(value.quantize(_CENT, rounding=ROUND_HALF_UP), "f")


def _series(points: Sequence[tuple[str, Any]]) -> list[tuple[str, Decimal]]:
    if len(points) > MAX_POINTS:
        raise ChartError(f"a curve has at most {MAX_POINTS} points")
    out = [(str(day)[:19], dec(value, "equity")) for day, value in points]
    if len(out) < 2:
        raise ChartError("a curve needs at least two points")
    return out


def _path(values: Sequence[Decimal], low: Decimal, high: Decimal, *, close: bool = False) -> str:
    span = (high - low) or Decimal(1)
    inner_w, inner_h = Decimal(WIDTH - 2 * PAD), Decimal(HEIGHT - 2 * PAD)
    last = max(len(values) - 1, 1)
    coords = []
    for i, v in enumerate(values):
        x = PAD + inner_w * i / last
        y = PAD + inner_h - inner_h * (v - low) / span
        coords.append(f"{_num(x)},{_num(y)}")
    if close:
        base = _num(Decimal(PAD + HEIGHT - 2 * PAD))
        coords = [f"{PAD},{base}", *coords, f"{_num(PAD + inner_w)},{base}"]
    return " ".join(coords)


def _frame(title: str, body: str, lo: str, hi: str) -> str:
    return (f'<svg role="img" aria-label="{escape(title, quote=True)}" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">'
            f'<line class="axis" x1="{PAD}" y1="{PAD}" x2="{PAD}" y2="{HEIGHT - PAD}"/><line class="axis" x1="{PAD}" y1="{HEIGHT - PAD}" x2="{WIDTH - PAD}" y2="{HEIGHT - PAD}"/>'
            f'{body}<text x="{PAD + 4}" y="{PAD - 8}" font-size="11">{escape(hi)}</text><text x="{PAD + 4}" y="{HEIGHT - 8}" font-size="11">{escape(lo)}</text></svg>')


def drawdown(values: Sequence[Decimal]) -> list[Decimal]:
    """The drop from the highest value so far, as a fraction (0 at a new high, -0.1 is ten per cent under it)."""
    peak, out = None, []
    for v in values:
        peak = v if peak is None or v > peak else peak
        out.append(ZERO if peak <= 0 else v / peak - 1)
    return out


def equity_svg(points: Sequence[tuple[str, Any]]) -> str:
    series = _series(points)
    values = [v for _d, v in series]
    low, high = min(values), max(values)
    return _frame("Equity curve", f'<polyline class="curve" points="{_path(values, low, high)}"/>', _num(low), _num(high))


def drawdown_svg(points: Sequence[tuple[str, Any]]) -> str:
    series = _series(points)
    drops = drawdown([v for _d, v in series])
    low = min(drops)
    return _frame("Drawdown", f'<polygon class="dd" points="{_path(drops, low if low < 0 else Decimal(-1), ZERO, close=True)}"/>',
                  f"{_num(low * 100)}%", "0%")


def composition_table(weights: Mapping[str, Any]) -> str:
    """The composition as bars and a table. A negative value (a short) is shown as its size with the word short."""
    if len(weights) > MAX_SLICES:
        raise ChartError(f"a composition has at most {MAX_SLICES} lines")
    sized = {str(k): dec(v, "position value") for k, v in weights.items()}
    total = sum((abs(v) for v in sized.values()), ZERO)
    if total == 0:
        raise ChartError("a composition needs at least one non-zero value")
    rows, y = [], 0
    bars = []
    for i, (name, value) in enumerate(sorted(sized.items(), key=lambda kv: (-abs(kv[1]), kv[0]))):
        share = abs(value) / total
        color = _PALETTE[i % len(_PALETTE)]
        bars.append(f'<rect x="{PAD}" y="{y + 6}" width="{_num(share * (WIDTH - 2 * PAD))}" height="14" fill="{color}"/>')
        rows.append(f"<tr><td>{escape(name)}</td><td>{'short' if value < 0 else 'long'}</td><td>{escape(_num(value))}</td><td>{escape(_num(share * 100))}%</td></tr>")
        y += 24
    svg = f'<svg role="img" aria-label="Composition" width="{WIDTH}" height="{y + 12}" viewBox="0 0 {WIDTH} {y + 12}">{"".join(bars)}</svg>'
    return f'{svg}<table><tr><th>Contract</th><th>Side</th><th>Value</th><th>Share</th></tr>{"".join(rows)}</table>'


def build(title: str, equity: Sequence[tuple[str, Any]], composition: Mapping[str, Any]) -> str:
    """The whole page: equity curve, drawdown and composition, in one HTML text with nothing loaded from outside."""
    safe = escape(str(title)[:120])
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">"
            f"<title>{safe}</title><style>{CSS}</style></head><body><h1>{safe}</h1>"
            f"<h2>Equity curve</h2>{equity_svg(equity)}<h2>Drawdown</h2>{drawdown_svg(equity)}"
            f"<h2>Composition</h2>{composition_table(composition)}"
            f"<p class=\"note\">From {escape(str(equity[0][0])[:19])} to {escape(str(equity[-1][0])[:19])}.</p></body></html>")
