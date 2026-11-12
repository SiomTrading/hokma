"""Builders for small Office and image files used by test_docs_readers.py (and to regenerate the fixtures)."""
import struct
import zipfile
import zlib
from io import BytesIO

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
TYPES = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'


def _esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_docx(paragraphs, heading_names=True, document_xml=None):
    """paragraphs: list of (style_id or None, text). Styles Heading1..3 are declared in styles.xml."""
    styles = "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/></w:style>'
        for n in (1, 2, 3)
    ) if heading_names else ""
    body = ""
    for style, text in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body += f"<w:p>{ppr}<w:r><w:t>{_esc(text)}</w:t></w:r></w:p>"
    document = document_xml or f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", TYPES)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", f'<?xml version="1.0"?><w:styles xmlns:w="{W}">{styles}</w:styles>')
    return out.getvalue()


def make_pptx(slides):
    """slides: list of dicts {title, body: [str], notes: [str]}."""
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", TYPES)
        for n, slide in enumerate(slides, 1):
            shapes = ""
            if slide.get("title"):
                shapes += (f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="T"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
                           f'<p:txBody><a:p><a:r><a:t>{_esc(slide["title"])}</a:t></a:r></a:p></p:txBody></p:sp>')
            for line in slide.get("body", []):
                shapes += (f'<p:sp><p:nvSpPr><p:cNvPr id="3" name="B"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
                           f'<p:txBody><a:p><a:r><a:t>{_esc(line)}</a:t></a:r></a:p></p:txBody></p:sp>')
            z.writestr(f"ppt/slides/slide{n}.xml",
                       f'<?xml version="1.0"?><p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>')
            if slide.get("notes"):
                paras = "".join(f"<a:p><a:r><a:t>{_esc(t)}</a:t></a:r></a:p>" for t in slide["notes"])
                z.writestr(
                    f"ppt/notesSlides/notesSlide{n}.xml",
                    f'<?xml version="1.0"?><p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>'
                    f'<p:sp><p:nvSpPr><p:cNvPr id="1" name="N"/><p:cNvSpPr/><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>{n}</a:t></a:r></a:p></p:txBody></p:sp>'
                    f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="N"/><p:cNvSpPr/><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><p:txBody>{paras}</p:txBody></p:sp>'
                    f'</p:spTree></p:cSld></p:notes>')
                z.writestr(
                    f"ppt/slides/_rels/slide{n}.xml.rels",
                    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    f'<Relationship Id="rId1" Type="notes" Target="../notesSlides/notesSlide{n}.xml"/></Relationships>')
    return out.getvalue()


def make_png(width, height):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_gif(width, height):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00;"


def make_jpeg(width, height):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def make_webp(width, height, kind="VP8 "):
    if kind == "VP8 ":
        payload = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\x00" * 4
    elif kind == "VP8L":
        bits = (width - 1) | ((height - 1) << 14)
        payload = b"\x2f" + struct.pack("<I", bits) + b"\x00" * 4
    else:
        payload = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunk = kind.encode() + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def make_pdf(pages):
    """Minimal PDF, one page per item; an item of None or "" is a page with no text (like a scanned page)."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", None, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        page_id, content_id = len(objects) + 1, len(objects) + 2
        kids.append(f"{page_id} 0 R")
        stream = b""
        if text:
            safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>".encode())
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_encrypted_pdf(pages, user_password):
    """The same PDF protected with a user password (RC4, which pypdf handles without extra packages)."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(BytesIO(make_pdf(pages))))
    writer.encrypt(user_password, algorithm="RC4-128")
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def make_xlsx(sheets):
    """sheets: dict name -> list of rows (tuples). Written with openpyxl."""
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(list(row))
    out = BytesIO()
    book.save(out)
    return out.getvalue()
