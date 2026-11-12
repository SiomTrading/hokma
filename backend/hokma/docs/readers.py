"""Standard-library readers for project documents (SPEC-009 section 3).

`read_document(path)` returns a `ReadResult` whose `passages` are `(section, text)` pairs ready for
`DocumentsRepo.add_chunks`. Each reader yields raw `(section, text)` sections lazily; the budget (pages, passages and
seconds) is enforced while consuming them, so a huge file stops early and the document keeps what was read.

Readers here: md, plain text family, csv/tsv, html, docx, pptx, pdf (pypdf), xlsx/xlsm (openpyxl), xls (xlrd) and
image headers. No OCR.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import struct
import time
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterator
from xml.etree import ElementTree as ET

from hokma.docs.chunker import CHUNK_TARGET, normalize_text, split_section

MAX_PAGES = 2000
MAX_PASSAGES = 5000
READ_SECONDS = 120.0

LINES_PER_BLOCK = 60
ROWS_PER_BLOCK = 50
PARAGRAPHS_PER_BLOCK = 20
MAX_ZIP_MEMBER_BYTES = 64 * 1024 * 1024
DEFAULT_SECTION = "document"
ROWS_PER_SHEET_BLOCK = 50
XML_PROLOG_BYTES = 2048
MAX_SHEET_ROWS = 1_048_576  # the Excel limits; anything past them is a hostile or broken dimension
MAX_SHEET_COLUMNS = 16_384
NO_TEXT_NOTICE = "This PDF has no selectable text; the agent only sees its name."

Section = tuple[str, str]


class _OutOfTime(Exception):
    """Raised from inside a reader loop when the read budget in seconds is spent."""


class UnsupportedFormat(Exception):
    """The extension is not one of the formats this module reads."""
    code = "unsupported_format"


UNREADABLE_CODES = (
    "pdf_password", "pdf_encrypted", "pdf_damaged", "office_invalid", "unsafe_xml", "part_too_large",
    "workbook_invalid", "workbook_password", "sheet_damaged", "csv_invalid", "html_invalid", "image_invalid",
    "open_failed",
)


class UnreadableDocument(Exception):
    """The file has the right extension but its content cannot be read (corrupt, wrong type, unsafe).

    `code` is a short key into the i18n catalog (docs.detail.reason.<code>); the message stays in English for logs
    and is never shown to the student."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Truncation:
    """Why reading stopped early: kind is "pages", "passages" or "time"; `reached` is how far it got."""
    kind: str
    reached: int

    @property
    def detail(self) -> str:
        unit = {"pages": "pages", "passages": "passages", "time": "seconds"}[self.kind]
        return f"Read stopped at the {self.kind} limit after {self.reached} {unit}."


@dataclass
class ReadResult:
    format: str
    passages: list[Section] = field(default_factory=list)
    truncated: Truncation | None = None
    width: int | None = None
    height: int | None = None
    notice: str | None = None  # the document is usable but the student should know something (e.g. no text in a PDF)

    @property
    def detail(self) -> str | None:
        return " ".join(x for x in (self.truncated.detail if self.truncated else None, self.notice) if x) or None


# ---------------------------------------------------------------------------------------------------- formats

_TEXT_EXTENSIONS = {
    ".txt", ".log", ".json", ".yaml", ".yml", ".py", ".cs", ".pine", ".ini", ".cfg", ".toml", ".xml", ".sql",
    ".js", ".ts", ".css", ".sh", ".bat", ".ps1", ".java", ".c", ".h", ".cpp", ".hpp", ".go", ".rs", ".rb", ".php",
    ".rst", ".tex", ".env", ".conf", ".properties", ".ipynb", ".srt", ".vtt",
}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_FORMATS: dict[str, str] = {
    ".md": "md", ".markdown": "md", ".csv": "csv", ".tsv": "tsv", ".html": "html", ".htm": "html",
    ".docx": "docx", ".pptx": "pptx", ".pdf": "pdf", ".xlsx": "xlsx", ".xlsm": "xlsx", ".xls": "xls",
    **{ext: "text" for ext in _TEXT_EXTENSIONS},
    **{ext: "image" for ext in _IMAGE_EXTENSIONS},
}


def supported_extensions() -> frozenset[str]:
    return frozenset(_FORMATS)


def detect_format(name: str) -> str | None:
    """Format key for a file name (by extension, case-insensitive), or None when this module does not read it."""
    return _FORMATS.get(Path(name).suffix.lower())


# ---------------------------------------------------------------------------------------------------- budget

class _Budget:
    def __init__(self, max_pages: int, max_passages: int, seconds: float, clock: Callable[[], float]):
        self.max_pages, self.max_passages = max_pages, max_passages
        self.clock = clock
        self.deadline = clock() + seconds
        self.pages = 0
        self.passages = 0

    def time_up(self) -> bool:
        return self.clock() > self.deadline


def _decode_text(data: bytes) -> str:
    """UTF-8 (with or without BOM); if not valid, UTF-16; if not, latin-1 (which never fails)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    if len(data) >= 4 and len(data) % 2 == 0:
        even_nulls = data[0::2].count(0)
        odd_nulls = data[1::2].count(0)
        half = len(data) // 2
        for nulls, codec in ((odd_nulls, "utf-16-le"), (even_nulls, "utf-16-be")):
            if nulls * 4 >= half:
                try:
                    return data.decode(codec)
                except UnicodeDecodeError:
                    pass
    return data.decode("latin-1")


# ---------------------------------------------------------------------------------------------------- markdown

_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_ATX = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_SETEXT = re.compile(r"^\s{0,3}(=+|-+)[ \t]*$")


def _trail(headings: list[str]) -> str:
    return " > ".join(h for h in headings if h) or DEFAULT_SECTION


def _read_markdown(data: bytes, **_) -> Iterator[Section]:
    headings: list[str] = []
    buffer: list[str] = []
    in_fence = False
    lines = _decode_text(data).splitlines()

    def flush() -> Section | None:
        text = "\n".join(buffer).strip()
        buffer.clear()
        return (_trail(headings), text) if text else None

    def push(level: int, title: str) -> None:
        del headings[level - 1:]
        headings.extend([""] * (level - 1 - len(headings)))
        headings.append(title)

    for i, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            buffer.append(line)
            continue
        if in_fence:
            buffer.append(line)
            continue
        match = _ATX.match(line)
        if match:
            done = flush()
            if done:
                yield done
            push(len(match.group(1)), match.group(2))
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if line.strip() and not _paragraph_is_open(buffer) and _SETEXT.match(nxt) and not _ATX.match(line):
            # Setext heading: this line is the title, the next line is its underline.
            done = flush()
            if done:
                yield done
            push(1 if nxt.strip().startswith("=") else 2, line.strip())
            lines[i + 1] = ""
            continue
        buffer.append(line)
    done = flush()
    if done:
        yield done


def _paragraph_is_open(buffer: list[str]) -> bool:
    """True when the previous line is text of the same paragraph (a setext title must start its own paragraph)."""
    return bool(buffer) and bool(buffer[-1].strip())


# ---------------------------------------------------------------------------------------------------- plain text

def _read_text(data: bytes, **_) -> Iterator[Section]:
    lines = _decode_text(data).splitlines()
    for start in range(0, len(lines), LINES_PER_BLOCK):
        block = lines[start:start + LINES_PER_BLOCK]
        if any(line.strip() for line in block):
            yield (f"lines {start + 1}-{start + len(block)}", "\n".join(block))


# ---------------------------------------------------------------------------------------------------- csv / tsv

def _read_delimited(data: bytes, *, delimiter: str, **_) -> Iterator[Section]:
    csv.field_size_limit(10 * 1024 * 1024)
    try:
        rows = [r for r in csv.reader(io.StringIO(_decode_text(data), newline=""), delimiter=delimiter)]
    except csv.Error as exc:
        raise UnreadableDocument(f"Not a valid delimited file: {exc}", code="csv_invalid") from exc
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        return
    header, body = rows[0], rows[1:]
    head_line = " | ".join(header)
    if not body:
        yield ("rows 0-0", head_line)
        return
    for start in range(0, len(body), ROWS_PER_BLOCK):
        block = body[start:start + ROWS_PER_BLOCK]
        text = "\n".join([head_line] + [" | ".join(row) for row in block])
        yield (f"rows {start + 1}-{start + len(block)}", text)


# ---------------------------------------------------------------------------------------------------- html

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer", "nav", "pre",
    "blockquote", "form", "dd", "dt", "dl", "figure", "figcaption", "main", "aside", "td", "th", "hr",
}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3}


class _HtmlSections(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[Section] = []
        self._skip = 0
        self._buffer: list[str] = []
        self._headings = ["", "", ""]
        self._heading_level = 0
        self._heading_text: list[str] = []

    def _flush(self) -> None:
        text = "".join(self._buffer).strip()
        self._buffer.clear()
        if normalize_text(text):
            self.sections.append((_trail(self._headings), text))

    def _close_heading(self) -> None:
        level = self._heading_level
        title = normalize_text(" ".join(self._heading_text)).replace("\n", " ")
        self._headings[level - 1] = title
        for deeper in range(level, 3):
            self._headings[deeper] = ""
        self._heading_level = 0
        self._heading_text = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if self._heading_level and (tag in _HEADINGS or tag in _BLOCK_TAGS):
            self._close_heading()  # a heading that was never closed ends where the next block starts
        if tag in _HEADINGS:
            self._flush()
            self._heading_level = _HEADINGS[tag]
            self._heading_text = []
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt and alt.strip():
                self._buffer.append(f"\n[image: {alt.strip()}]\n")
        if tag in _BLOCK_TAGS:
            self._buffer.append("\n")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.handle_starttag(tag, attrs)
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in _HEADINGS and self._heading_level:
            self._close_heading()
        if tag in _BLOCK_TAGS:
            self._buffer.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._heading_level:
            self._heading_text.append(data)
        else:
            self._buffer.append(data)

    def close(self) -> None:
        super().close()
        if self._heading_level:
            self._close_heading()
        self._flush()


def _read_html(data: bytes, **_) -> Iterator[Section]:
    parser = _HtmlSections()
    try:
        parser.feed(_decode_text(data))
        parser.close()
    except Exception as exc:  # html.parser is lenient; anything left is a broken input, not a bug to hide
        raise UnreadableDocument(f"Not a readable HTML page: {exc}", code="html_invalid") from exc
    yield from parser.sections


# ---------------------------------------------------------------------------------------------------- office zips

def _open_zip(data: bytes, required: str) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnreadableDocument("The file is not a valid Office document.", code="office_invalid") from exc
    if required not in archive.namelist():
        raise UnreadableDocument("The file is not a valid Office document.", code="office_invalid")
    return archive


def _member_xml(archive: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_ZIP_MEMBER_BYTES:
        raise UnreadableDocument("The document has a part that is too large to read.", code="part_too_large")
    raw = archive.read(name)
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise UnreadableDocument("The document contains unsafe XML.", code="unsafe_xml")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise UnreadableDocument("The document has damaged content.", code="office_invalid") from exc


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_R = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_HEADING_STYLE = re.compile(r"^heading\s*([1-3])$", re.I)


def _heading_styles(archive: zipfile.ZipFile) -> dict[str, int]:
    """style id -> heading level 1..3, using the style names and the plain ids as fallback."""
    levels: dict[str, int] = {}
    root = _member_xml(archive, "word/styles.xml")
    if root is not None:
        for style in root.iter(f"{_W}style"):
            style_id = style.get(f"{_W}styleId", "")
            name_el = style.find(f"{_W}name")
            name = name_el.get(f"{_W}val", "") if name_el is not None else ""
            match = _HEADING_STYLE.match(name) or _HEADING_STYLE.match(style_id)
            if match:
                levels[style_id] = int(match.group(1))
    return levels


def _paragraph_text(paragraph: ET.Element) -> str:
    parts: list[str] = []
    for el in paragraph.iter():
        if el.tag == f"{_W}t":
            parts.append(el.text or "")
        elif el.tag in (f"{_W}tab", f"{_W}br", f"{_W}cr"):
            parts.append(" ")
    return "".join(parts).strip()


def _read_docx(data: bytes, **_) -> Iterator[Section]:
    archive = _open_zip(data, "word/document.xml")
    styles = _heading_styles(archive)
    root = _member_xml(archive, "word/document.xml")
    items: list[tuple[int, str]] = []  # (heading level or 0, text)
    for paragraph in root.iter(f"{_W}p"):
        text = _paragraph_text(paragraph)
        if not text:
            continue
        style = paragraph.find(f"{_W}pPr/{_W}pStyle")
        style_id = style.get(f"{_W}val", "") if style is not None else ""
        match = _HEADING_STYLE.match(style_id)
        level = styles.get(style_id) or (int(match.group(1)) if match else 0)
        items.append((level, text))
    if not any(level for level, _ in items):
        for start in range(0, len(items), PARAGRAPHS_PER_BLOCK):
            block = items[start:start + PARAGRAPHS_PER_BLOCK]
            yield (f"paragraphs {start + 1}-{start + len(block)}", "\n\n".join(text for _, text in block))
        return
    headings = ["", "", ""]
    buffer: list[str] = []
    for level, text in items:
        if level:
            if buffer:
                yield (_trail(headings), "\n\n".join(buffer))
                buffer = []
            headings[level - 1] = text
            for deeper in range(level, 3):
                headings[deeper] = ""
        else:
            buffer.append(text)
    if buffer:
        yield (_trail(headings), "\n\n".join(buffer))


def _slide_number(name: str) -> int:
    return int(re.search(r"(\d+)\.xml$", name).group(1))


def _shape_paragraphs(root: ET.Element) -> list[str]:
    return [t for t in ("".join(x.text or "" for x in p.iter(f"{_A}t")).strip() for p in root.iter(f"{_A}p")) if t]


def _slide_title(root: ET.Element) -> str:
    for shape in root.iter(f"{_P}sp"):
        ph = shape.find(f"{_P}nvSpPr/{_P}nvPr/{_P}ph")
        if ph is not None and ph.get("type") in ("title", "ctrTitle"):
            return " ".join(_shape_paragraphs(shape))
    return ""


def _notes_for(archive: zipfile.ZipFile, slide_name: str) -> list[str]:
    rels = _member_xml(archive, f"ppt/slides/_rels/{Path(slide_name).name}.rels")
    if rels is None:
        return []
    for rel in rels.iter(f"{_R}Relationship"):
        target = rel.get("Target", "")
        if "notesSlide" in target:
            notes = _member_xml(archive, "ppt/notesSlides/" + Path(target).name)
            if notes is None:
                return []
            # The notes page also holds the slide image and the slide number: keep only the body placeholder.
            body: list[str] = []
            for shape in notes.iter(f"{_P}sp"):
                ph = shape.find(f"{_P}nvSpPr/{_P}nvPr/{_P}ph")
                if ph is not None and ph.get("type") == "body":
                    body.extend(_shape_paragraphs(shape))
            return body
    return []


def _read_pptx(data: bytes, **_) -> Iterator[Section]:
    archive = _open_zip(data, "[Content_Types].xml")
    slides = sorted(
        (n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=_slide_number
    )
    if not slides:
        raise UnreadableDocument("The file is not a valid presentation.", code="office_invalid")
    for name in slides:
        root = _member_xml(archive, name)
        title = _slide_title(root)
        lines = _shape_paragraphs(root)
        if title and lines and lines[0] == title:
            lines = lines[1:]
        parts = ([title] if title else []) + lines
        notes = _notes_for(archive, name)
        if notes:
            parts += ["Notes:"] + notes
        yield (f"slide {_slide_number(name)}", "\n".join(parts))


# ---------------------------------------------------------------------------------------------------- pdf

def _read_pdf(data: bytes, **_) -> Iterator[Section]:
    from pypdf import PdfReader  # imported on use: the dependency is only needed when a PDF is read

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise UnreadableDocument("The PDF is protected with a password.", code="pdf_password")
        total = len(reader.pages)
    except UnreadableDocument:
        raise
    except Exception as exc:  # pypdf raises many error types (PdfReadError, DependencyError for AES, ValueError...)
        if "cryptography" in str(exc).lower() or "encrypt" in str(exc).lower():
            raise UnreadableDocument("The PDF is encrypted and cannot be opened.", code="pdf_encrypted") from exc
        raise UnreadableDocument("The file is not a valid PDF or is damaged.", code="pdf_damaged") from exc
    failed = 0
    for number in range(total):
        try:
            text = reader.pages[number].extract_text() or ""
        except Exception:  # one damaged page must not lose the rest of the document
            failed += 1
            text = ""
        yield (f"page {number + 1}", text)
    if total and failed == total:
        raise UnreadableDocument("The PDF pages could not be read.", code="pdf_damaged")


# ---------------------------------------------------------------------------------------------------- spreadsheets

def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time(0) else value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return str(value).strip()


def _sheet_sections(
    sheet: str, rows: Iterator[tuple[int, list]], expired: Callable[[], bool] = lambda: False
) -> Iterator[Section]:
    """`(row number, cells)` pairs -> blocks of 50 rows named `sheet "X", rows a-b`, header repeated in each block."""
    header: str | None = None
    block: list[tuple[int, str]] = []

    def flush() -> Section:
        text = "\n".join(([header] if header else []) + [line for _, line in block])
        label = f'sheet "{sheet}", rows {block[0][0]}-{block[-1][0]}'
        block.clear()
        return (label, text)

    first: tuple[int, str] | None = None
    for number, cells in rows:
        if number % 256 == 0 and expired():  # blank rows yield nothing, so the caller cannot check the clock for us
            raise _OutOfTime
        texts = [_cell_text(c) for c in cells]
        while texts and not texts[-1]:
            texts.pop()
        if not texts:
            continue
        line = " | ".join(texts)
        if first is None:
            first = (number, line)
            header = line
            continue
        block.append((number, line))
        if len(block) >= ROWS_PER_SHEET_BLOCK:
            yield flush()
    if block:
        yield flush()
    elif first is not None:
        yield (f'sheet "{sheet}", rows {first[0]}-{first[0]}', first[1])


def _check_xlsx_zip(data: bytes) -> None:
    """Refuse parts that are too large or declare entities before the library parses them."""
    if data[:4] == b"\xd0\xcf\x11\xe0":
        raise UnreadableDocument("The workbook is protected with a password or is not a valid .xlsx file.", code="workbook_invalid")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnreadableDocument("The file is not a valid Excel workbook.", code="workbook_invalid") from exc
    for info in archive.infolist():
        if info.file_size > MAX_ZIP_MEMBER_BYTES:
            raise UnreadableDocument("The workbook has a part that is too large to read.", code="part_too_large")
        if info.filename.endswith((".xml", ".rels")):
            with archive.open(info) as part:
                head = part.read(XML_PROLOG_BYTES)
            if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
                raise UnreadableDocument("The workbook contains unsafe XML.", code="unsafe_xml")
    if "xl/workbook.xml" not in archive.namelist():
        raise UnreadableDocument("The file is not a valid Excel workbook.", code="workbook_invalid")


def _read_xlsx(data: bytes, *, expired: Callable[[], bool] = lambda: False, **_) -> Iterator[Section]:
    from openpyxl import load_workbook

    _check_xlsx_zip(data)
    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise UnreadableDocument("The file is not a valid Excel workbook or is damaged.", code="workbook_invalid") from exc
    try:
        for sheet in book.worksheets:
            try:
                sheet.reset_dimensions()  # do not pad rows out to dimensions the file declares
                rows = ((n, list(r)) for n, r in enumerate(
                    sheet.iter_rows(max_row=MAX_SHEET_ROWS, max_col=MAX_SHEET_COLUMNS, values_only=True), 1))
                yield from _sheet_sections(sheet.title, rows, expired)
            except (UnreadableDocument, _OutOfTime):
                raise
            except Exception as exc:
                raise UnreadableDocument(f'The sheet "{sheet.title}" is damaged.', code="sheet_damaged") from exc
    finally:
        book.close()


def _read_xls(data: bytes, *, expired: Callable[[], bool] = lambda: False, **_) -> Iterator[Section]:
    import xlrd

    try:
        book = xlrd.open_workbook(file_contents=data, on_demand=True)
    except xlrd.XLRDError as exc:
        text = str(exc).lower()
        if "encrypted" in text or "password" in text:
            raise UnreadableDocument("The workbook is protected with a password.", code="workbook_password") from exc
        raise UnreadableDocument("The file is not a valid .xls workbook or is damaged.", code="workbook_invalid") from exc
    except Exception as exc:
        raise UnreadableDocument("The file is not a valid .xls workbook or is damaged.", code="workbook_invalid") from exc

    def value(cell):
        if cell.ctype == xlrd.XL_CELL_DATE:
            try:
                return xlrd.xldate_as_datetime(cell.value, book.datemode)
            except Exception:
                return cell.value
        if cell.ctype == xlrd.XL_CELL_BOOLEAN:
            return bool(cell.value)
        if cell.ctype in (xlrd.XL_CELL_ERROR, xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
            return None
        return cell.value

    try:
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            try:
                rows = ((r + 1, [value(c) for c in sheet.row(r)[:MAX_SHEET_COLUMNS]])
                        for r in range(min(sheet.nrows, MAX_SHEET_ROWS)))
                yield from _sheet_sections(sheet.name, rows, expired)
            except (UnreadableDocument, _OutOfTime):
                raise
            except Exception as exc:
                raise UnreadableDocument(f'The sheet "{sheet.name}" is damaged.', code="sheet_damaged") from exc
            book.unload_sheet(index)
    finally:
        book.release_resources()


# ---------------------------------------------------------------------------------------------------- images

def _image_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from the file header; None when the bytes are not a PNG, GIF, JPEG or WebP header."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data[:2] == b"\xff\xd8":
        return _jpeg_size(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 25:
        kind = data[12:16]
        if kind in (b"VP8 ", b"VP8X") and len(data) < 30:
            return None
        if kind == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if kind == b"VP8L":
            b = data[21:25]
            return ((b[0] | ((b[1] & 0x3F) << 8)) + 1, ((b[1] >> 6) | (b[2] << 2) | ((b[3] & 0x0F) << 10)) + 1)
        if kind == b"VP8X":
            return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        i += 2 + length
    return None


# ---------------------------------------------------------------------------------------------------- entry point

_PAGED = {"pptx", "pdf"}


def _reader_for(fmt: str) -> Callable[..., Iterator[Section]]:
    if fmt == "tsv":
        return lambda data, **kw: _read_delimited(data, delimiter="\t")
    if fmt == "csv":
        return lambda data, **kw: _read_delimited(data, delimiter=",")
    return {"md": _read_markdown, "text": _read_text, "html": _read_html, "docx": _read_docx,
            "pptx": _read_pptx, "pdf": _read_pdf, "xlsx": _read_xlsx, "xls": _read_xls}[fmt]


def read_document(
    path: str | Path,
    *,
    description: str | None = None,
    alt: str | None = None,
    max_pages: int = MAX_PAGES,
    max_passages: int = MAX_PASSAGES,
    seconds: float = READ_SECONDS,
    target: int = CHUNK_TARGET,
    clock: Callable[[], float] = time.monotonic,
) -> ReadResult:
    """Read a file into passages. Raises UnsupportedFormat or UnreadableDocument.

    `description` is what the student wrote for an image and `alt` the alt text when the image came from a page;
    both are indexed with the file name. Past a limit the passages read so far are returned with `truncated` set.
    """
    path = Path(path)
    fmt = detect_format(path.name)
    if fmt is None:
        raise UnsupportedFormat(f"Format not supported yet: {path.suffix or path.name}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise UnreadableDocument(f"The file cannot be opened: {exc.strerror or exc}", code="open_failed") from exc

    if fmt == "image":
        size = _image_size(data)
        if size is None:
            raise UnreadableDocument("The file is not a valid image.", code="image_invalid")
        text = "\n".join(x for x in (path.stem, description, alt) if x and x.strip())
        return ReadResult("image", split_section("image", text, target), width=size[0], height=size[1])

    budget = _Budget(max_pages, max_passages, seconds, clock)
    result = ReadResult(fmt)
    sections = _reader_for(fmt)(data, expired=budget.time_up)
    paged = fmt in _PAGED
    while True:
        if budget.time_up():
            result.truncated = Truncation("time", round(seconds))
            break
        try:
            section, text = next(sections)
        except StopIteration:
            break
        except _OutOfTime:
            result.truncated = Truncation("time", round(seconds))
            break
        if paged:
            if budget.pages >= budget.max_pages:
                result.truncated = Truncation("pages", budget.pages)
                break
            budget.pages += 1
        for passage in split_section(section, text, target):
            if budget.passages >= budget.max_passages:
                result.truncated = Truncation("passages", budget.passages)
                break
            result.passages.append(passage)
            budget.passages += 1
        if result.truncated:
            break
    if fmt == "pdf" and not result.passages and not result.truncated:
        # Scanned or image-only PDF: keep the document searchable by name and say why the agent sees nothing more.
        result.passages = split_section("document", path.stem, target)
        result.notice = NO_TEXT_NOTICE
    return result
