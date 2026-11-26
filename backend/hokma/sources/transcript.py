"""The transcript of a video as passages with times (SPEC-030, A1-5).

The text that the `watch` plugin hands over has a time mark at the start of each line ("[03:12] text", "03:12 - 03:40 text", or the blocks of a
subtitle file with "00:03:12,000 --> 00:03:40,000"). Here the lines become entries with a start and an optional end, and the entries are grouped in
blocks of 30 to 60 seconds, each one a passage whose locator is the range of the block: "03:12-03:40". The notes about what the frames show become
passages whose locator is "frame 03:12". Pure functions: nothing here reads a file or the network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

BLOCK_SECONDS = 30                 # a block closes at the first entry that starts this long after the block began
LAST_ENTRY_SECONDS = 5             # the length given to a last entry with no end
MAX_ENTRY_SECONDS = 15             # the length given to an entry with no end when the next one starts later than this

MAX_PASSAGE_CHARS = 1_500          # a passage of a video is about the size of a chunk of the Library, so the detector reads all of it
PASSAGE_OVERLAP = 200              # characters that two neighbour pieces share, so a sentence on the cut is whole in one of them

_TS = r"(?:(?:\d{1,2}):)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"
_LINE = re.compile(rf"^\s*(?:frame\s+)?[\[(]?\s*(?P<a>{_TS})\s*(?:(?:-->|-|\u2013|to)\s*(?P<b>{_TS}))?\s*[\])]?\s*[:\-]?\s*(?P<text>.*)$", re.IGNORECASE)
_INDEX = re.compile(r"^\s*\d+\s*$")


class TranscriptError(ValueError):
    """The transcript has no time marks, or none of its lines has text."""


@dataclass(frozen=True)
class Entry:
    start: float
    end: float | None
    text: str


def seconds(mark: str) -> float:
    """A mark such as 03:12, 1:02:03 or 00:03:12,500 in seconds."""
    parts = mark.replace(",", ".").split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def clock(value: float) -> str:
    """Seconds as MM:SS, or H:MM:SS from one hour on."""
    whole = int(value)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse(text: str) -> list[Entry]:
    """The entries of a text with time marks at the start of its lines. A line with no mark continues the entry before it. Raises
    TranscriptError when a line with text comes before any mark, or when no line has a mark."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    entries: list[list] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.upper().startswith("WEBVTT"):
            continue
        if _INDEX.match(line) and i + 1 < len(lines) and "-->" in lines[i + 1]:
            continue                                    # the number of a subtitle block
        found = _LINE.match(line)
        if found:
            end = seconds(found.group("b")) if found.group("b") else None
            entries.append([seconds(found.group("a")), end, found.group("text").strip()])
        elif entries:
            entries[-1][2] = f"{entries[-1][2]} {line}".strip()
        else:
            raise TranscriptError("the transcript needs a time mark at the start of its lines, for example [03:12]")
    kept = [Entry(s, e, " ".join(t.split())) for s, e, t in entries if t.strip()]
    if not kept:
        raise TranscriptError("the transcript has no line with text and a time mark")
    return sorted(kept, key=lambda e: e.start)


def _ends(entries: list[Entry]) -> list[float]:
    out: list[float] = []
    for i, entry in enumerate(entries):
        if entry.end is not None and entry.end > entry.start:
            out.append(entry.end)
        elif i + 1 < len(entries):
            out.append(min(max(entries[i + 1].start, entry.start + 1), entry.start + MAX_ENTRY_SECONDS))
        else:
            out.append(entry.start + LAST_ENTRY_SECONDS)
    return out


def split_long(place: str, text: str, limit: int = MAX_PASSAGE_CHARS, overlap: int = PASSAGE_OVERLAP) -> list[tuple[str, str]]:
    """(locator, text) pieces of at most `limit` characters, cut at a space when there is one, each sharing `overlap` characters with the one before.
    A text that fits comes back as it is. The pieces of a long one keep the time mark of the block: "03:12-03:40 (2/5)"."""
    if len(text) <= limit:
        return [(place, text)]
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        end = min(start + limit, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + limit // 2, end)
            end = cut if cut > 0 else end
        spans.append((start, end))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
        while start < end and text[start] != " " and text[start - 1] != " ":
            start += 1                                   # begin the next piece at a word
    return [(f"{place} ({n}/{len(spans)})", text[a:b].strip()) for n, (a, b) in enumerate(spans, start=1)]


def blocks(entries: list[Entry]) -> list[tuple[str, str]]:
    """(locator, text) of the blocks of the entries: a block takes entries until one starts BLOCK_SECONDS or more after the block began."""
    ends = _ends(entries)
    out: list[tuple[str, str]] = []
    first = 0
    while first < len(entries):
        last = first
        while last + 1 < len(entries) and entries[last + 1].start - entries[first].start < BLOCK_SECONDS:
            last += 1
        start = entries[first].start
        end = max(ends[first:last + 1])
        text = " ".join(e.text for e in entries[first:last + 1])
        out += split_long(f"{clock(start)}-{clock(max(end, start + 1))}", text)
        first = last + 1
    return out


def frames(text: str) -> list[tuple[str, str]]:
    """(locator, text) of the notes about the frames: one passage per time mark ("frame 03:12"). Notes with no marks at all are one passage,
    "frame notes"."""
    try:
        return [piece for e in parse(text) for piece in split_long(f"frame {clock(e.start)}", e.text)]
    except TranscriptError:
        flat = " ".join(text.split())
        return split_long("frame notes", flat) if flat else []
