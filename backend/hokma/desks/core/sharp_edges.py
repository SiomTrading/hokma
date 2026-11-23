"""Sharp edges: known bad patterns in the code an agent writes (SPEC-017: ED-037).

Agents write short Python for backtests and indicators. Some mistakes look fine, run without an error and give a number that
is wrong or impossible to trade: reading the future, using the candle that is still open, dividing by a zero. Each rule here has
a regular expression that finds the mistake in a line, a note on what is wrong and the fix. `scan` returns one finding per hit.
It is a screen, not a proof: a line that guards the case (an `if`, `safe_div`, an epsilon) is let through, and a line can opt
out with the marker `sharp-edge: ok`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

OPT_OUT = "sharp-edge: ok"
SEVERITIES = ("low", "medium", "high")
_GUARD = re.compile(r"\bif\b|\bsafe_div\b|np\.where|\.replace\(\s*0|\bor\s+1\b|\bmax\(|\beps(ilon)?\b|\bclip\(|1e-\d+|fillna|\bnan\b|\bwhere\(", re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    id: str
    summary: str
    severity: str
    pattern: re.Pattern[str]
    fix: str
    unless_guarded: bool = False        # a guard on the same line (an if, safe_div, an epsilon) lets the line pass

    def matches(self, line: str) -> bool:
        if not self.pattern.search(line):
            return False
        return not (self.unless_guarded and _GUARD.search(line))


@dataclass(frozen=True)
class Finding:
    rule_id: str
    line: int
    text: str
    severity: str
    summary: str
    fix: str


def _rule(rule_id: str, summary: str, severity: str, pattern: str, fix: str, *, unless_guarded: bool = False) -> Rule:
    return Rule(rule_id, summary, severity, re.compile(pattern), fix, unless_guarded)


RULES: tuple[Rule, ...] = (
    _rule("lookahead-negative-shift", "A negative shift moves tomorrow's value into today's row: the signal reads the future.",
          "high", r"\.shift\(\s*(periods\s*=\s*)?-\s*\d+|\.diff\(\s*-\s*\d+|\.pct_change\(\s*(periods\s*=\s*)?-\s*\d+",
          "Shift forward only: `series.shift(1)` uses yesterday's value. A forward return belongs to the label, never to a feature."),
    _rule("lookahead-roll", "A negative roll wraps the future values to the front of the array.", "high",
          r"\bnp\.roll\([^)]*,\s*-\s*\d+", "Use `shift(1)` on a Series: it leaves NaN at the start instead of wrapping."),
    _rule("lookahead-backfill", "Back-filling copies a later value into an earlier gap: the past knows the future.", "high",
          r"\.bfill\(|\.backfill\(|fillna\([^)]*method\s*=\s*['\"](bfill|backfill)['\"]|interpolate\([^)]*limit_direction\s*=\s*['\"](both|backward)['\"]",
          "Fill forward only (`ffill`), or drop the gap."),
    _rule("lookahead-centered-window", "A centered rolling window uses values after the current bar.", "high",
          r"\.rolling\([^)]*center\s*=\s*True", "Remove `center=True`: a window must end on the current bar."),
    _rule("open-candle", "The last row of a bar series is the candle that is still open: its high, low and close change until it closes.",
          "high", r"\b(close|high|low|volume|bars?|candles?|ohlc\w*|df|data)\b[^\n]*\.iloc\[\s*-\s*1\s*\]|\[\s*['\"](close|high|low|open)['\"]\s*\]\s*\.tail\(\s*1\s*\)",
          "Use the last closed bar, `iloc[-2]`, or filter the series to closed bars before reading its last row."),
    _rule("divide-by-zero-ratio", "A ratio divides by a standard deviation, a variance or a sum that can be zero (flat prices, no trades).",
          "medium", r"/\s*\(?\s*[\w.\[\]'\"]+\.(std|var|sum)\(\s*[^)]*\)\s*\)?(?!\s*\.)",
          "Guard the denominator: `safe_div(a, b)`, or return None when it is zero.", unless_guarded=True),
    _rule("divide-by-difference", "A ratio divides by a difference of two values (a range, a spread) that is zero on a flat bar.",
          "medium", r"/\s*\(\s*[\w.\[\]'\"]+\s*-\s*[\w.\[\]'\"]+\s*\)",
          "Guard the denominator before dividing, or add an epsilon.", unless_guarded=True),
    _rule("annualize-365", "Markets trade about 252 days a year: a 365 annualization inflates every ratio.", "medium",
          r"\bsqrt\(\s*365(\.0)?\s*\)|[*/]\s*365(\.0)?\b|\b365(\.0)?\s*[*/]",
          "Use the periods of the series: 252 for daily bars of stocks and futures, via the metrics module."),
    _rule("shuffled-time-split", "Shuffling before a train and test split mixes the future into the training rows.", "high",
          r"train_test_split\([^)]*shuffle\s*=\s*True|train_test_split\((?![^)]*shuffle\s*=\s*False)[^)]*\)",
          "Split by date: train on the earlier rows, test on the later ones, with `shuffle=False`."),
    _rule("dynamic-code", "Running text as code lets a model answer or a fetched page run anything on the machine.", "high",
          r"(?<![\w.])(eval|exec)\s*\(|os\.system\(|subprocess\.\w+\([^)]*shell\s*=\s*True|__import__\(",
          "Call the function by name from a table of allowed functions, never from text."),
    _rule("float-price-equality", "Comparing two float prices with == fails on rounding; a price level is hit or crossed, not equalled.",
          "low", r"\b(price|close|high|low|open|stop|target|entry)\w*\s*==\s*[\w.]*\d+\.\d+",
          "Compare with `>=` or `<=`, or round both sides to the tick size."),
)
BY_ID = {r.id: r for r in RULES}


def _code_only(line: str) -> str:
    """`line` without its trailing comment (a `#` inside a string is kept)."""
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
        elif char == "#":
            return line[:index]
    return line


def scan(code: str, rules: Iterable[Rule] = RULES) -> list[Finding]:
    """Every hit of `rules` in `code`, in line order. A comment is not code; `# sharp-edge: ok` skips its line."""
    findings: list[Finding] = []
    rule_list = tuple(rules)
    for number, raw in enumerate(code.splitlines(), 1):
        if OPT_OUT in raw:
            continue
        line = _code_only(raw)
        if not line.strip():
            continue
        for rule in rule_list:
            if rule.matches(line):
                findings.append(Finding(rule.id, number, raw.strip(), rule.severity, rule.summary, rule.fix))
    return findings


def has_blockers(findings: Iterable[Finding], *, at_least: str = "high") -> bool:
    """True when a finding has the severity `at_least` or worse."""
    floor = SEVERITIES.index(at_least)
    return any(SEVERITIES.index(f.severity) >= floor for f in findings)


def report(findings: Iterable[Finding]) -> str:
    """The findings as text the agent can act on, one block each. Empty string when there are none."""
    blocks = [f"line {f.line} [{f.severity}] {f.rule_id}: {f.summary}\n    code: {f.text}\n    fix: {f.fix}" for f in findings]
    return "\n".join(blocks)
