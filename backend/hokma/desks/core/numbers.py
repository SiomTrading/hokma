"""Numbers written by a model (SPEC-017: TA-37).

A model writes `1,2k`, `3.4%`, `USD 1.000`, `(1,200)`, `N/A`. Before a number reaches a calculation or a report it is read
into one value with a unit, and it is checked against the snapshot the agent was given: a number that is not in the
snapshot is marked `unverified`, never trusted.

Separators: with both `.` and `,` the last one is the decimal mark. A single separator followed by exactly three digits
after a group of one to three digits is a thousands mark (`1.000`, `1,000`); anything else is a decimal mark. A suffix
(`k`, `m`, `b`, `t`) makes a single separator a decimal mark (`1,2k` is 1200).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

NULLISH = frozenset({"", "none", "n/a", "na", "null", "nil", "-", "--", "tbd", "unknown", "nan"})
SUFFIX = {"k": 1e3, "m": 1e6, "mm": 1e6, "b": 1e9, "bn": 1e9, "t": 1e12}
CURRENCY_WORDS = ("usd", "us$", "eur", "gbp", "jpy", "cop", "mxn", "cad", "chf")
CURRENCY_SIGNS = "$\u20ac\u00a3\u00a5"
_NUMBER = re.compile(
    r"(?P<neg>(?:\(|-|\u2212)\s*)?(?P<cur1>(?:usd|us\$|eur|gbp|jpy|cop|mxn|cad|chf|[$\u20ac\u00a3\u00a5])\s*)?"
    r"(?P<sign>[+-])?(?P<num>\d(?:[\d.,]*\d)?|\.\d+)\s*(?P<suffix>mm|bn|[kmbt])?(?![A-Za-z0-9])\s*(?P<pct>%)?"
    r"(?P<cur2>\s*(?:usd|eur|gbp|jpy|cop|mxn|cad|chf)\b)?", re.IGNORECASE)


_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


@dataclass(frozen=True)
class ParsedNumber:
    raw: str
    value: float | None
    unit: str = "number"          # "number", "pct" or "currency"
    ok: bool = True
    reason: str = ""


@dataclass(frozen=True)
class NumberCheck:
    raw: str
    value: float
    unit: str
    status: str                   # "verified" or "unverified"
    start: int
    end: int


def _decimal_form(digits: str, *, has_suffix: bool) -> tuple[str | None, str]:
    """The digits with a `.` decimal mark, or None when they are not a number. Second item is a note."""
    dots, commas = digits.count("."), digits.count(",")
    if dots and commas:
        last = max(digits.rfind("."), digits.rfind(","))
        mark = digits[last]
        other = "," if mark == "." else "."
        head, tail = digits[:last], digits[last + 1:]
        if mark in head or not tail.isdigit():
            return None, "mixed separators"
        groups = head.split(other)
        if not groups[0].isdigit() or any(len(g) != 3 or not g.isdigit() for g in groups[1:]):
            return None, "bad grouping"
        return "".join(groups) + "." + tail, ""
    sep = "." if dots else ("," if commas else "")
    if not sep:
        return digits, ""
    parts = digits.split(sep)
    if not all(p.isdigit() for p in parts[1:]) or (parts[0] and not parts[0].isdigit()):
        return None, "bad digits"
    if len(parts) > 2:
        if all(len(p) == 3 for p in parts[1:]) and 1 <= len(parts[0]) <= 3:
            return "".join(parts), ""
        return None, "bad grouping"
    head, tail = parts
    if not has_suffix and len(tail) == 3 and 1 <= len(head) <= 3 and not head.startswith("0"):
        return head + tail, "thousands"
    return (head or "0") + "." + tail, ""


def parse_number(text: Any, *, allow_pct: bool = True) -> ParsedNumber:
    """One number written the way a model writes it. A placeholder (`N/A`) gives ok=False with value None."""
    if isinstance(text, bool):
        return ParsedNumber(str(text), None, ok=False, reason="boolean")
    if isinstance(text, (int, float)):
        value = float(text)
        if not math.isfinite(value):
            return ParsedNumber(str(text), None, ok=False, reason="not finite")
        return ParsedNumber(str(text), value)
    raw = "" if text is None else str(text)
    stripped = raw.strip()
    if stripped.lower() in NULLISH:
        return ParsedNumber(raw, None, ok=False, reason="placeholder")
    paren = len(stripped) > 2 and stripped.startswith("(") and stripped.endswith(")")
    if paren:
        stripped = stripped[1:-1].strip()
    match = _NUMBER.fullmatch(stripped)
    if not match:
        return ParsedNumber(raw, None, ok=False, reason="not a number")
    suffix = (match.group("suffix") or "").lower()
    form, note = _decimal_form(match.group("num"), has_suffix=bool(suffix))
    if form is None:
        return ParsedNumber(raw, None, ok=False, reason=note)
    try:
        value = float(form)
    except ValueError:
        return ParsedNumber(raw, None, ok=False, reason="not a number")
    value *= SUFFIX.get(suffix, 1.0)
    negative = paren or (match.group("neg") or "").strip() in ("-", "\u2212") or match.group("sign") == "-"
    if negative:
        value = -value
    if match.group("pct"):
        if not allow_pct:
            return ParsedNumber(raw, None, "pct", ok=False, reason="percentage where an amount was asked")
        return ParsedNumber(raw, value, "pct")
    if match.group("cur1") or match.group("cur2"):
        return ParsedNumber(raw, value, "currency", reason=note)
    return ParsedNumber(raw, value, "number", reason=note)


def coerce_optional_float(value: Any, *, allow_pct: bool = False) -> float | None:
    """A model-written optional number as a float, or None when it is a placeholder, a range or a hedge.

    A percentage is dropped unless `allow_pct`: reading `15%` as 15 where a price was asked would put a stop at 15."""
    if value is None:
        return None
    parsed = parse_number(value, allow_pct=allow_pct)
    return parsed.value if parsed.ok else None


def text_values(text: str) -> list[float]:
    """Every number written in a text (a prompt, a report returned by a tool), read with `parse_number`."""
    scan = _DATE.sub(lambda m: " " * len(m.group(0)), text)
    out: list[float] = []
    for match in _NUMBER.finditer(scan):
        piece = match.group(0).strip()
        if match.start() > 0 and scan[match.start() - 1].isalnum() and piece[:1] in ("-", "\u2212"):
            piece = piece[1:].strip()
        parsed = parse_number(piece)
        if parsed.ok and parsed.value is not None:
            out.append(parsed.value)
    return out


def snapshot_values(snapshot: Any) -> list[float]:
    """Every numeric leaf of a snapshot (dicts, lists, numbers and numeric strings), flattened."""
    out: list[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, (int, float)):
            if math.isfinite(float(node)):
                out.append(float(node))
        elif isinstance(node, str):
            parsed = parse_number(node)
            if parsed.ok and parsed.value is not None:
                out.append(parsed.value)
            else:
                out.extend(text_values(node))
        elif isinstance(node, Mapping):
            for item in node.values():
                walk(item)
        elif isinstance(node, (list, tuple, set, frozenset)):
            for item in node:
                walk(item)
        elif hasattr(node, "tolist"):
            walk(node.tolist())

    walk(snapshot)
    return out


def _close(a: float, b: float, rel_tol: float, decimals: int | None) -> bool:
    if math.isclose(a, b, rel_tol=rel_tol, abs_tol=1e-12):
        return True
    if decimals is not None:
        return round(b, decimals) == round(a, decimals) or abs(a - b) <= 0.5 * 10 ** (-decimals) + 1e-12
    return False


def is_in_snapshot(value: float, unit: str, values: Iterable[float], *, rel_tol: float = 1e-6, decimals: int | None = None) -> bool:
    """True when `value` matches a snapshot value. A percentage also matches the snapshot ratio (`3.4%` matches 0.034)."""
    for candidate in values:
        if _close(value, candidate, rel_tol, decimals):
            return True
        if unit == "pct" and _close(value / 100.0, candidate, rel_tol, None if decimals is None else decimals + 2):
            return True
    return False


def _decimals_of(raw_number: str) -> int | None:
    form, _ = _decimal_form(raw_number, has_suffix=False)
    if form is None or "." not in form:
        return 0 if form is not None else None
    return len(form.split(".")[1])


def check_text(text: str, snapshot: Any, *, rel_tol: float = 1e-6, ignore_below: float = 0.0) -> list[NumberCheck]:
    """Every number in `text`, each marked verified or unverified against `snapshot`.

    A number whose absolute value is below `ignore_below` (list markers, years of a calendar) is skipped."""
    values = snapshot_values(snapshot)
    checks: list[NumberCheck] = []
    scan = _DATE.sub(lambda m: " " * len(m.group(0)), text)           # a date is not a quantity
    for match in _NUMBER.finditer(scan):
        piece = match.group(0).strip()
        joined = match.start() > 0 and scan[match.start() - 1].isalnum()
        if joined and piece[:1] in ("-", "\u2212"):                      # the dash of a range (150-160), not a minus sign
            piece = piece[1:].strip()
        parsed = parse_number(piece)
        if not parsed.ok or parsed.value is None:
            continue
        if abs(parsed.value) < ignore_below:
            continue
        decimals = None if match.group("suffix") else _decimals_of(match.group("num"))
        status = "verified" if is_in_snapshot(parsed.value, parsed.unit, values, rel_tol=rel_tol, decimals=decimals) else "unverified"
        checks.append(NumberCheck(piece, parsed.value, parsed.unit, status, match.start(),
                                  match.start() + len(match.group(0).rstrip())))
    return checks


def unverified_numbers(text: str, snapshot: Any, **kwargs: Any) -> list[NumberCheck]:
    return [c for c in check_text(text, snapshot, **kwargs) if c.status == "unverified"]


def mark_unverified(text: str, snapshot: Any, *, tag: str = "[unverified]", **kwargs: Any) -> str:
    """`text` with ` [unverified]` placed after each number that is not in the snapshot."""
    out, last = [], 0
    for check in check_text(text, snapshot, **kwargs):
        if check.status != "unverified":
            continue
        out.append(text[last:check.end])
        out.append(f" {tag}")
        last = check.end
    out.append(text[last:])
    return "".join(out)
