"""Text normalizer and passage splitter.

A reader hands over `(section, text)` pairs. `split_section` turns one pair into passages of about CHUNK_TARGET
characters without cutting words. When a section is split, every part is numbered: `page 4 (2/3)`.
"""
from __future__ import annotations

import re
import unicodedata

CHUNK_TARGET = 1500

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufeff\u200b]")
_SPACES = re.compile(r"[^\S\n]+")
_BLANKS = re.compile(r"\n{3,}")
_SEPARATORS = ("\n\n", "\n", " ")


def normalize_text(text: str) -> str:
    """Unicode NFC, unified line breaks, no control characters, single spaces, at most one blank line in a row."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").replace("\t", " ")
    text = _CONTROL.sub("", text)
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def _pack(units: list[str], sep: str, target: int) -> list[str]:
    packed: list[str] = []
    current = ""
    for unit in units:
        if not current:
            current = unit
        elif len(current) + len(sep) + len(unit) <= target:
            current += sep + unit
        else:
            packed.append(current)
            current = unit
    if current:
        packed.append(current)
    return packed


def _fit(text: str, level: int, target: int) -> list[str]:
    """Pieces of at most `target` characters: cut at paragraphs, then lines, then spaces, and only at the end hard."""
    if len(text) <= target:
        return [text] if text.strip() else []
    if level >= len(_SEPARATORS):
        return [text[i:i + target] for i in range(0, len(text), target)]
    sep = _SEPARATORS[level]
    units: list[str] = []
    for unit in text.split(sep):
        units.extend(_fit(unit, level + 1, target))
    return _pack(units, sep, target)


def split_section(section: str, text: str, target: int = CHUNK_TARGET) -> list[tuple[str, str]]:
    """Passages `(section, text)` for one section; empty list when the text has nothing in it."""
    if target < 50:
        raise ValueError("target must be at least 50 characters")
    parts = _fit(normalize_text(text), 0, target)
    if len(parts) <= 1:
        return [(section, part) for part in parts]
    total = len(parts)
    return [(f"{section} ({n}/{total})", part) for n, part in enumerate(parts, 1)]
