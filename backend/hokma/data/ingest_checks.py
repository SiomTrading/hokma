"""Checklist for ingesting documents with figures, tables and images (SPEC-017: RA-025).

Six failure modes of multimodal ingestion, each one a check that returns warnings, so that a review (Vera's) can run the list
on a parsed document. A parsed document is a list of blocks, each a dict with a `type` (text, table, image, equation), a
`page_idx` and the fields of its type (`text`, `table_body`, `table_caption`, `img_path`, `image_caption`, `description`).

1. ocr_corrupts_text         OCR or layout silently corrupts numbers, ligatures or columns
2. table_structure_lost      a table lost its structure (merged cells, flattened rows, ragged columns)
3. image_caption_misaligned  captions and figures do not match or are out of order
4. retrieval_text_bias       figures and tables are not indexed, so only text can be retrieved
5. ingest_stuck              slow but progressing is told apart from a hang
6. asset_path_not_portable   chunks point to files that exist only on the machine that ingested them
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Collection, Mapping, Sequence

from hokma.data.sentinels import to_utc, utcnow

OCR = "ocr_corrupts_text"
TABLE = "table_structure_lost"
CAPTION = "image_caption_misaligned"
RETRIEVAL = "retrieval_text_bias"
STUCK = "ingest_stuck"
PATHS = "asset_path_not_portable"
FAILURE_MODES: tuple[str, ...] = (OCR, TABLE, CAPTION, RETRIEVAL, STUCK, PATHS)

Block = Mapping[str, Any]


@dataclass(frozen=True)
class IngestWarning:
    code: str
    severity: str                     # warning | info
    message: str
    blocks: tuple[int, ...] = ()


@dataclass(frozen=True)
class IngestReport:
    warnings: tuple[IngestWarning, ...]
    checked: tuple[str, ...] = FAILURE_MODES

    @property
    def ok(self) -> bool:
        return not any(w.severity == "warning" for w in self.warnings)

    def codes(self) -> set[str]:
        return {w.code for w in self.warnings}


# ---------------------------------------------------------------------------------------------------- 1
_LOOKALIKES = {"l": "1", "I": "1", "O": "0", "o": "0", "S": "5", "B": "8", "Z": "2"}
_NUMBERISH = re.compile(r"(?<![A-Za-z])[\d$€£][\d.,%$€£A-Za-z]*\d[\d.,%]*(?![A-Za-z])|(?<![A-Za-z])[lIOoSBZ][\d.,]*\d[\d.,]*(?![A-Za-z])")
_LIGATURES = "\ufb00\ufb01\ufb02\ufb03\ufb04"


def check_ocr_corruption(blocks: Sequence[Block], *, source_figures: Mapping[int, Sequence[str]] | None = None) -> list[IngestWarning]:
    """Numbers that mix digits with look-alike letters (`1O4.5`, `l,250`), ligature or replacement characters, a short line that
    repeats on most pages (a watermark), and figures that differ from a second reading (`source_figures`: block index to the
    figures read independently)."""
    found: list[IngestWarning] = []
    bad_numbers: dict[int, list[str]] = {}
    for index, block in enumerate(blocks):
        text = str(block.get("text") or "")
        for token in _NUMBERISH.findall(text):
            if any(ch in _LOOKALIKES for ch in token) and any(ch.isdigit() for ch in token):
                bad_numbers.setdefault(index, []).append(token)
        if any(ch in text for ch in _LIGATURES) or "\ufffd" in text:
            found.append(IngestWarning(OCR, "warning", "ligature or replacement characters in the text", (index,)))
    for index, tokens in bad_numbers.items():
        found.append(IngestWarning(OCR, "warning", f"numbers with letters that look like digits: {sorted(set(tokens))[:5]}", (index,)))
    pages = {b.get("page_idx") for b in blocks if b.get("page_idx") is not None}
    if len(pages) >= 3:
        lines = Counter()
        for page in pages:
            seen = {line.strip() for b in blocks if b.get("page_idx") == page for line in str(b.get("text") or "").splitlines() if 3 <= len(line.strip()) <= 40}
            lines.update(seen)
        repeated = [line for line, count in lines.items() if count >= max(3, int(0.6 * len(pages)))]
        if repeated:
            found.append(IngestWarning(OCR, "info", f"lines repeated on most pages (watermark or header): {repeated[:3]}"))
    for index, expected in (source_figures or {}).items():
        text = str(blocks[index].get("text") or "") if 0 <= index < len(blocks) else ""
        missing = [figure for figure in expected if figure not in text]
        if missing:
            found.append(IngestWarning(OCR, "warning", f"figures from the second reading are not in the text: {missing[:5]}", (index,)))
    return found


# ---------------------------------------------------------------------------------------------------- 2
def _html_rows(body: str) -> list[int]:
    return [len(re.findall(r"<t[dh]\b", row, flags=re.IGNORECASE)) for row in re.findall(r"<tr\b.*?</tr>", body, flags=re.IGNORECASE | re.DOTALL)]


def check_table_structure(blocks: Sequence[Block]) -> list[IngestWarning]:
    """Table blocks with no body, with a body that is flat text instead of rows and cells, with rows of different width, or with
    merged cells (listed as info: exact structure needs another parser)."""
    found: list[IngestWarning] = []
    for index, block in enumerate(blocks):
        if block.get("type") != "table":
            continue
        body = str(block.get("table_body") or "")
        if not body.strip():
            found.append(IngestWarning(TABLE, "warning", "table block has no body", (index,)))
            continue
        widths = _html_rows(body)
        if not widths:
            pipe_rows = [r for r in body.splitlines() if "|" in r]
            if len(pipe_rows) < 2:
                found.append(IngestWarning(TABLE, "warning", "table was flattened to plain lines: rows and cells are lost", (index,)))
                continue
            widths = [len([c for c in r.strip("| ").split("|")]) for r in pipe_rows if not set(r.strip("| ")) <= set("-: |")]
        if len(set(widths)) > 1 and not re.search(r"(colspan|rowspan)", body, flags=re.IGNORECASE):
            found.append(IngestWarning(TABLE, "warning", f"rows have different numbers of cells: {sorted(set(widths))}", (index,)))
        if re.search(r"(colspan|rowspan)", body, flags=re.IGNORECASE):
            found.append(IngestWarning(TABLE, "info", "merged cells present: check the exact structure against the source", (index,)))
    return found


# ---------------------------------------------------------------------------------------------------- 3
def check_caption_alignment(blocks: Sequence[Block]) -> list[IngestWarning]:
    """Images without a file or without a caption, one file with different captions, and page numbers that go backwards."""
    found: list[IngestWarning] = []
    captions: dict[str, set[str]] = {}
    last_page = None
    for index, block in enumerate(blocks):
        page = block.get("page_idx")
        if isinstance(page, int):
            if last_page is not None and page < last_page:
                found.append(IngestWarning(CAPTION, "warning", f"page index goes back from {last_page} to {page}", (index,)))
            last_page = page
        if block.get("type") != "image":
            continue
        path, caption = block.get("img_path"), block.get("image_caption")
        caption_text = " ".join(caption) if isinstance(caption, (list, tuple)) else str(caption or "")
        if not path:
            found.append(IngestWarning(CAPTION, "warning", "image block without a file path", (index,)))
        if not caption_text.strip():
            found.append(IngestWarning(CAPTION, "warning", "image block without a caption", (index,)))
        if path and caption_text.strip():
            captions.setdefault(str(path), set()).add(caption_text.strip())
    for path, texts in captions.items():
        if len(texts) > 1:
            found.append(IngestWarning(CAPTION, "warning", f"one image file has {len(texts)} different captions: {path}"))
    return found


# ---------------------------------------------------------------------------------------------------- 4
def check_multimodal_indexing(blocks: Sequence[Block], enabled_modalities: Collection[str]) -> list[IngestWarning]:
    """Retrieval biased to text: tables, images or equations exist in the document but their modality is not indexed, or the
    block has no enriched description to embed (only raw OCR)."""
    found: list[IngestWarning] = []
    for modality in ("table", "image", "equation"):
        indexes = tuple(i for i, b in enumerate(blocks) if b.get("type") == modality)
        if not indexes:
            continue
        if modality not in enabled_modalities:
            found.append(IngestWarning(RETRIEVAL, "warning", f"{len(indexes)} {modality} block(s) are in the document but {modality} processing is off", indexes))
            continue
        bare = tuple(i for i in indexes if not str(blocks[i].get("description") or "").strip())
        if bare:
            found.append(IngestWarning(RETRIEVAL, "warning", f"{len(bare)} {modality} block(s) have no enriched description: retrieval sees only raw text", bare))
    return found


# ---------------------------------------------------------------------------------------------------- 5
def check_progress(started_at: datetime, last_progress_at: datetime, *, now: datetime | None = None,
                   slow_after_seconds: float = 300.0, stuck_after_seconds: float = 900.0) -> list[IngestWarning]:
    """Tell slow but progressing from a hang: a run is stuck when nothing advanced for `stuck_after_seconds`; it is only slow
    when it is old but still advancing."""
    moment = to_utc(now) if now else utcnow()
    since_progress = (moment - to_utc(last_progress_at)).total_seconds()
    elapsed = (moment - to_utc(started_at)).total_seconds()
    if since_progress >= stuck_after_seconds:
        return [IngestWarning(STUCK, "warning", f"no progress for {int(since_progress)} s: treat as a hang and check timeouts, parser logs and disk space")]
    if elapsed >= slow_after_seconds:
        return [IngestWarning(STUCK, "info", f"running for {int(elapsed)} s and still progressing: slow, not stuck")]
    return []


# ---------------------------------------------------------------------------------------------------- 6
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\)")


def check_asset_paths(blocks: Sequence[Block]) -> list[IngestWarning]:
    """Blocks whose file path is absolute on the machine that ingested them and that carry no public URL."""
    bad = tuple(i for i, b in enumerate(blocks)
                if _ABSOLUTE.match(str(b.get("img_path") or "")) and not (b.get("img_public_url") or b.get("public_url")))
    if not bad:
        return []
    return [IngestWarning(PATHS, "warning", f"{len(bad)} block(s) point to local absolute paths with no public URL: another host cannot load them", bad)]


# ---------------------------------------------------------------------------------------------------- all six
def run_ingest_checks(blocks: Sequence[Block], *, enabled_modalities: Collection[str] = ("table", "image", "equation"),
                      started_at: datetime | None = None, last_progress_at: datetime | None = None, now: datetime | None = None,
                      source_figures: Mapping[int, Sequence[str]] | None = None) -> IngestReport:
    """Run the six checks. The progress check runs only when the run times are given."""
    found: list[IngestWarning] = []
    found += check_ocr_corruption(blocks, source_figures=source_figures)
    found += check_table_structure(blocks)
    found += check_caption_alignment(blocks)
    found += check_multimodal_indexing(blocks, enabled_modalities)
    if started_at is not None and last_progress_at is not None:
        found += check_progress(started_at, last_progress_at, now=now)
    found += check_asset_paths(blocks)
    return IngestReport(tuple(found))
