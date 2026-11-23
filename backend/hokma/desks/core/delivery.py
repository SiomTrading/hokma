"""Files in the reply of a desk (SPEC-017: OA-13).

A desk agent that made a file (a report, a chart) names it in its reply as `[[reports/summary.pdf]]`. This module reads those
references, checks each one against the desk's folder and returns the reply as an ordered list of text pieces and files, so the
screen or the chat can send the file at the position it was named.

A reference is delivered only when it is a plain relative path with a supported extension, inside the desk folder (after
resolving links), an existing file, at most 1 MiB, and among the first five distinct files of the reply. Everything else stays
as literal text and is reported in `rejected` with the reason: writing a reference is not proof of delivery. A reference inside
a code span or a code fence is an example, never a delivery.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from hokma.desks.core.paths import is_within

MAX_FILES = 5
MAX_BYTES = 1024 * 1024
SUPPORTED = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".txt", ".md", ".csv", ".json", ".html", ".xlsx", ".zip"})
IMAGES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_REF = re.compile(r"\[\[([^\[\]\n]+)\]\]")
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_SPAN = re.compile(r"`[^`\n]*`")


class DeliveryError(ValueError):
    """The folder of the desk is not usable."""


@dataclass(frozen=True)
class Piece:
    kind: str                         # "text" or "file"
    text: str = ""                    # the text, or the relative path of a file
    path: Path | None = None          # absolute path of a delivered file
    size: int = 0
    as_image: bool = False


@dataclass(frozen=True)
class Rejected:
    ref: str
    reason: str                       # absolute, parent, prefix, label, unsupported, outside, missing, too_big, limit, not_file, empty


@dataclass(frozen=True)
class Delivery:
    pieces: tuple[Piece, ...]
    rejected: tuple[Rejected, ...] = field(default_factory=tuple)

    @property
    def files(self) -> list[Piece]:
        return [p for p in self.pieces if p.kind == "file"]

    @property
    def text(self) -> str:
        """The reply as text: the pieces in order, a delivered reference removed, a rejected one kept literally."""
        return "".join(p.text for p in self.pieces if p.kind == "text")


def check_ref(ref: str) -> tuple[str | None, str]:
    """(clean relative path, "") or (None, reason) for the text between the brackets, before touching the disk."""
    if ref != ref.strip() or not ref:
        return None, "empty"
    lowered = ref.lower()
    if lowered.startswith("file:"):
        return None, "prefix"
    if "|" in ref:
        return None, "label"
    if ref.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", ref) or ref.startswith("~"):
        return None, "absolute"
    parts = re.split(r"[\\/]", ref)
    if any(p == ".." for p in parts) or any(p == "" for p in parts):
        return None, "parent"
    if "\x00" in ref:
        return None, "outside"
    if Path(ref).suffix.lower() not in SUPPORTED:
        return None, "unsupported"
    return "/".join(parts), ""


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _FENCE.finditer(text)]
    for m in _SPAN.finditer(text):
        if not any(a <= m.start() < b for a, b in spans):
            spans.append(m.span())
    return spans


def deliver(reply: str, root: Path | str, *, max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES) -> Delivery:
    """Split `reply` into text and files. `root` is the desk folder: a file outside it is rejected, links included."""
    base = Path(root)
    if not base.is_dir():
        raise DeliveryError(f"the desk folder does not exist: {base}")
    real_base = base.resolve()
    protected = _protected_spans(reply)
    pieces: list[Piece] = []
    rejected: list[Rejected] = []
    seen: set[str] = set()
    cursor = 0
    delivered = 0

    def add_text(chunk: str) -> None:
        if not chunk:
            return
        if pieces and pieces[-1].kind == "text":
            pieces[-1] = Piece("text", pieces[-1].text + chunk)
        else:
            pieces.append(Piece("text", chunk))

    for match in _REF.finditer(reply):
        if any(a <= match.start() < b for a, b in protected):
            continue
        ref = match.group(1)
        clean, reason = check_ref(ref)
        target: Path | None = None
        size = 0
        if clean is not None:
            candidate = base.joinpath(*clean.split("/"))
            try:
                target = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                reason = "missing"
            else:
                if not is_within(str(real_base), str(target)):
                    reason = "outside"
                elif not target.is_file():
                    reason = "not_file"
                else:
                    size = target.stat().st_size
                    if size > max_bytes:
                        reason = "too_big"
        if not reason and clean in seen:
            add_text(reply[cursor:match.start()])
            cursor = match.end()
            continue                                   # the same file named twice is delivered once, at its first place
        if not reason and delivered >= max_files:
            reason = "limit"
        add_text(reply[cursor:match.start()])
        cursor = match.end()
        if reason:
            rejected.append(Rejected(ref, reason))
            add_text(match.group(0))
            continue
        assert clean is not None and target is not None
        seen.add(clean)
        delivered += 1
        pieces.append(Piece("file", clean, target, size, Path(clean).suffix.lower() in IMAGES))
    add_text(reply[cursor:])
    return Delivery(tuple(pieces), tuple(rejected))

