"""Readers, normalizer and passage splitter for project documents (SPEC-009 P2)."""
from pathlib import Path

import pytest
from docs_support import make_docx, make_gif, make_jpeg, make_png, make_pptx, make_webp

from hokma.docs import (
    CHUNK_TARGET,
    UnreadableDocument,
    UnsupportedFormat,
    detect_format,
    normalize_text,
    read_document,
    split_section,
    supported_extensions,
)

FIX = Path(__file__).parent / "fixtures" / "docs"


def sections(result):
    return [s for s, _ in result.passages]


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


# ------------------------------------------------------------------------------------------ normalizer / splitter

def test_normalize_text_cleans_spaces_controls_and_blank_runs():
    raw = "a" + chr(0xA0) + " b\t\tc\r\n\r\n\r\n\r\nd\x00e" + chr(0xFEFF) + "  \n  f  "
    assert normalize_text(raw) == "a b c\n\nde\nf"


def test_normalize_text_composes_unicode():
    assert normalize_text("cafe" + chr(0x301)) == "caf" + chr(0xE9)


def test_short_section_keeps_its_name_and_is_not_numbered():
    assert split_section("page 4", "hello world") == [("page 4", "hello world")]


def test_empty_text_gives_no_passages():
    assert split_section("page 1", " \n\t ") == []


def test_long_section_is_split_numbered_and_never_cuts_words():
    words = [f"word{i:04d}" for i in range(1200)]
    passages = split_section("page 4", " ".join(words))
    total = len(passages)
    assert total >= 6
    assert [s for s, _ in passages] == [f"page 4 ({n}/{total})" for n in range(1, total + 1)]
    assert all(len(t) <= CHUNK_TARGET for _, t in passages)
    assert " ".join(t for _, t in passages).split(" ") == words


def test_passages_prefer_paragraph_boundaries():
    para = "x" * 700
    passages = split_section("s", "\n\n".join([para, para, para]))
    assert [t for _, t in passages] == [para + "\n\n" + para, para]


def test_a_word_longer_than_the_target_is_cut_hard():
    passages = split_section("s", "y" * 3200, target=1000)
    assert [len(t) for _, t in passages] == [1000, 1000, 1000, 200]


def test_target_below_minimum_is_refused():
    with pytest.raises(ValueError):
        split_section("s", "text", target=10)


# ------------------------------------------------------------------------------------------ format detection

def test_detect_format_by_extension_case_insensitive():
    assert detect_format("Notes.MD") == "md"
    assert detect_format("a.markdown") == "md"
    assert detect_format("run.log") == "text"
    assert detect_format("strategy.cs") == "text"
    assert detect_format("x.pine") == "text"
    assert detect_format("data.TSV") == "tsv"
    assert detect_format("p.htm") == "html"
    assert detect_format("p.JPEG") == "image"
    assert detect_format("book.pdf") == "pdf"
    assert detect_format("book.XLSX") == "xlsx"
    assert detect_format("book.docm") is None
    assert detect_format("noextension") is None
    assert {".md", ".docx", ".pptx", ".webp"} <= supported_extensions()


def test_unsupported_format_is_refused(tmp_path):
    path = write(tmp_path, "movie.mp4", b"\x00\x00")
    with pytest.raises(UnsupportedFormat) as err:
        read_document(path)
    assert err.value.code == "unsupported_format"


def test_missing_file_is_unreadable(tmp_path):
    with pytest.raises(UnreadableDocument):
        read_document(tmp_path / "gone.txt")


# ------------------------------------------------------------------------------------------ markdown

def test_markdown_sections_are_heading_paths():
    result = read_document(FIX / "sample.md")
    assert sections(result) == ["document", "Intro", "Intro > Data", "Intro > Data > Deep", "Second"]
    by_section = dict(result.passages)
    assert by_section["document"] == "Preface text before any heading."
    assert "not a heading, inside a fence" in by_section["Intro > Data"]
    assert by_section["Second"] == "Closing words."
    assert result.truncated is None and result.detail is None


def test_markdown_heading_jump_and_setext(tmp_path):
    path = write(tmp_path, "a.md", "Title\n=====\n\nbody one\n\nSub\n---\n\nbody two\n\n### Skipped\n\nbody three\n")
    result = read_document(path)
    assert sections(result) == ["Title", "Title > Sub", "Title > Sub > Skipped"]


def test_markdown_closing_hashes_are_trimmed(tmp_path):
    result = read_document(write(tmp_path, "a.md", "## Name ##\n\ntext\n"))
    assert sections(result) == ["Name"]


# ------------------------------------------------------------------------------------------ plain text family

def test_text_blocks_of_lines():
    result = read_document(FIX / "sample.txt")
    assert result.passages == [("lines 1-2", "line one\nline two")]


def test_text_blocks_split_every_sixty_lines(tmp_path):
    path = write(tmp_path, "big.log", "\n".join(f"l{i}" for i in range(1, 131)))
    assert sections(read_document(path)) == ["lines 1-60", "lines 61-120", "lines 121-130"]


def test_text_blank_blocks_are_skipped(tmp_path):
    path = write(tmp_path, "a.txt", "\n" * 70 + "late text")
    assert sections(read_document(path)) == ["lines 61-71"]


def test_text_decodes_utf8_utf8_bom_utf16_and_latin1(tmp_path):
    text = "caf" + chr(0xE9) + " price 18000"
    cases = {
        "u8.txt": text.encode("utf-8"),
        "bom.txt": b"\xef\xbb\xbf" + text.encode("utf-8"),
        "u16.txt": text.encode("utf-16"),
        "u16le.txt": text.encode("utf-16-le"),
        "u16be.txt": text.encode("utf-16-be"),
        "l1.txt": text.encode("latin-1"),
    }
    for name, data in cases.items():
        assert read_document(write(tmp_path, name, data)).passages[0][1] == text, name


def test_code_files_are_read_as_text(tmp_path):
    result = read_document(write(tmp_path, "strategy.cs", "class A { }\n"))
    assert result.format == "text" and result.passages[0][1] == "class A { }"


# ------------------------------------------------------------------------------------------ csv / tsv

def test_csv_rows_with_header():
    assert read_document(FIX / "sample.csv").passages == [
        ("rows 1-2", "symbol | price | qty\nNQ | 18000 | 2\nES | 5000 | 1")]


def test_tsv_rows_with_header():
    assert read_document(FIX / "sample.tsv").passages == [("rows 1-1", "symbol | price\nNQ | 18000")]


def test_csv_repeats_header_in_every_block_of_fifty_rows(tmp_path):
    rows = ["id,name"] + [f"{i},n{i}" for i in range(1, 121)]
    result = read_document(write(tmp_path, "t.csv", "\n".join(rows)))
    assert sections(result) == ["rows 1-50", "rows 51-100", "rows 101-120"]
    assert all(t.startswith("id | name\n") for _, t in result.passages)
    assert "\n51 | n51" in result.passages[1][1]


def test_csv_quoted_fields_and_blank_rows(tmp_path):
    result = read_document(write(tmp_path, "t.csv", 'a,b\n"x, y","line\nbreak"\n,\n'))
    assert result.passages == [("rows 1-1", "a | b\nx, y | line\nbreak")]


def test_csv_header_only_and_empty(tmp_path):
    assert read_document(write(tmp_path, "h.csv", "a,b\n")).passages == [("rows 0-0", "a | b")]
    assert read_document(write(tmp_path, "e.csv", "")).passages == []


# ------------------------------------------------------------------------------------------ html

def test_html_sections_follow_h1_to_h3_without_script_or_style():
    result = read_document(FIX / "sample.html")
    assert sections(result) == ["Guide", "Guide > Setup", "Guide > Usage"]
    by_section = dict(result.passages)
    assert by_section["Guide"] == "Opening paragraph."
    assert "Install the add-on." in by_section["Guide > Setup"]
    assert "[image: Chart of the setup screen]" in by_section["Guide > Setup"]
    assert by_section["Guide > Usage"] == "Run it & check."
    joined = " ".join(t for _, t in result.passages)
    assert "hidden" not in joined and "color:red" not in joined and "Ignored title" not in joined


def test_html_without_headings_is_one_section(tmp_path):
    result = read_document(write(tmp_path, "a.htm", "<p>one</p><p>two</p>"))
    assert result.passages == [("document", "one\n\ntwo")]


def test_html_broken_markup_does_not_fail(tmp_path):
    result = read_document(write(tmp_path, "a.html", "<h1>Open<p>text <b>bold</h2><script>x"))
    assert result.passages and "text" in result.passages[0][1]


def test_html_h4_does_not_start_a_section(tmp_path):
    result = read_document(write(tmp_path, "a.html", "<h1>Top</h1><h4>Minor</h4><p>text</p>"))
    assert sections(result) == ["Top"] and "text" in result.passages[0][1]


# ------------------------------------------------------------------------------------------ docx

def test_docx_sections_follow_heading_styles():
    result = read_document(FIX / "sample.docx")
    assert result.passages == [("Overview", "First body paragraph."), ("Overview > Details", "Second body paragraph.")]


def test_docx_without_headings_uses_paragraph_blocks():
    assert read_document(FIX / "plain.docx").passages == [("paragraphs 1-2", "Alpha paragraph.\n\nBeta paragraph.")]


def test_docx_paragraph_blocks_of_twenty(tmp_path):
    data = make_docx([(None, f"p{i}") for i in range(1, 46)])
    assert sections(read_document(write(tmp_path, "a.docx", data))) == [
        "paragraphs 1-20", "paragraphs 21-40", "paragraphs 41-45"]


def test_docx_heading_styles_found_by_style_id_without_styles_part(tmp_path):
    data = make_docx([("Heading1", "Top"), (None, "body")], heading_names=False)
    assert read_document(write(tmp_path, "a.docx", data)).passages == [("Top", "body")]


def test_docx_text_before_first_heading_keeps_a_section(tmp_path):
    data = make_docx([(None, "lead"), ("Heading1", "Top"), (None, "body")])
    assert sections(read_document(write(tmp_path, "a.docx", data))) == ["document", "Top"]


def test_docx_not_a_zip_or_wrong_package_is_unreadable(tmp_path):
    for data in (b"not a zip", make_pptx([{"title": "x"}])):
        with pytest.raises(UnreadableDocument):
            read_document(write(tmp_path, "bad.docx", data))


def test_docx_with_entity_declarations_is_refused(tmp_path):
    evil = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>&a;</w:t></w:r></w:p></w:body></w:document>")
    with pytest.raises(UnreadableDocument, match="unsafe"):
        read_document(write(tmp_path, "evil.docx", make_docx([], document_xml=evil)))


def test_docx_damaged_xml_is_unreadable(tmp_path):
    with pytest.raises(UnreadableDocument):
        read_document(write(tmp_path, "bad.docx", make_docx([], document_xml="<w:document")))


# ------------------------------------------------------------------------------------------ pptx

def test_pptx_slides_with_title_and_notes():
    result = read_document(FIX / "sample.pptx")
    assert result.passages == [
        ("slide 1", "Opening\nAgenda item\nNotes:\nSay hello"),
        ("slide 2", "Results\nNumbers go here"),
    ]


def test_pptx_slides_are_ordered_numerically(tmp_path):
    data = make_pptx([{"title": f"T{i}"} for i in range(1, 12)])
    assert sections(read_document(write(tmp_path, "a.pptx", data))) == [f"slide {i}" for i in range(1, 12)]


def test_pptx_not_a_presentation_is_unreadable(tmp_path):
    for data in (b"junk", make_docx([(None, "x")])):
        with pytest.raises(UnreadableDocument):
            read_document(write(tmp_path, "bad.pptx", data))


# ------------------------------------------------------------------------------------------ images

@pytest.mark.parametrize("name,size", [
    ("sample.png", (3, 2)), ("sample.gif", (5, 4)), ("sample.jpg", (7, 6)), ("sample.webp", (9, 8)),
])
def test_image_header_gives_size_and_indexes_the_name(name, size):
    result = read_document(FIX / name)
    assert (result.width, result.height) == size
    assert result.format == "image"
    assert result.passages == [("image", "sample")]


@pytest.mark.parametrize("kind", ["VP8 ", "VP8L", "VP8X"])
def test_webp_variants(tmp_path, kind):
    result = read_document(write(tmp_path, "a.webp", make_webp(640, 480, kind)))
    assert (result.width, result.height) == (640, 480)


def test_large_png_and_jpeg_sizes(tmp_path):
    assert read_document(write(tmp_path, "a.png", make_png(1920, 1080))).width == 1920
    result = read_document(write(tmp_path, "a.jpeg", make_jpeg(4000, 3000)))
    assert (result.width, result.height) == (4000, 3000)
    assert read_document(write(tmp_path, "a.gif", make_gif(300, 200))).height == 200


def test_image_indexes_description_and_alt(tmp_path):
    path = write(tmp_path, "chart one.png", make_png(2, 2))
    result = read_document(path, description="Volume profile of NQ", alt="Weekly chart")
    assert result.passages == [("image", "chart one\nVolume profile of NQ\nWeekly chart")]


def test_image_with_wrong_content_is_unreadable(tmp_path):
    with pytest.raises(UnreadableDocument):
        read_document(write(tmp_path, "fake.png", b"this is not an image at all"))
    with pytest.raises(UnreadableDocument):
        read_document(write(tmp_path, "short.jpg", b"\xff\xd8\xff"))


# ------------------------------------------------------------------------------------------ limits

def test_long_section_is_split_inside_read_document(tmp_path):
    path = write(tmp_path, "a.md", "# Big\n\n" + " ".join(f"w{i}" for i in range(2000)))
    result = read_document(path)
    assert len(result.passages) > 3
    assert result.passages[0][0].startswith("Big (1/")


def test_passage_limit_keeps_what_was_read_and_says_where(tmp_path):
    path = write(tmp_path, "a.txt", "\n".join(f"line {i}" for i in range(1, 601)))
    result = read_document(path, max_passages=4)
    assert len(result.passages) == 4
    assert result.truncated.kind == "passages" and result.truncated.reached == 4
    assert "passages" in result.detail


def test_page_limit_applies_to_slides(tmp_path):
    data = make_pptx([{"title": f"T{i}"} for i in range(1, 8)])
    result = read_document(write(tmp_path, "a.pptx", data), max_pages=3)
    assert sections(result) == ["slide 1", "slide 2", "slide 3"]
    assert result.truncated.kind == "pages" and result.truncated.reached == 3


def test_exactly_at_the_limit_is_not_truncated(tmp_path):
    data = make_pptx([{"title": f"T{i}"} for i in range(1, 4)])
    result = read_document(write(tmp_path, "a.pptx", data), max_pages=3)
    assert len(result.passages) == 3 and result.truncated is None


def test_time_limit_stops_reading_with_a_fake_clock(tmp_path):
    path = write(tmp_path, "a.csv", "a,b\n" + "\n".join(f"{i},x" for i in range(300)))
    ticks = iter([0.0, 0.0, 1.0, 500.0, 500.0, 500.0])
    result = read_document(path, seconds=120, clock=lambda: next(ticks))
    assert [s for s, _ in result.passages] == ["rows 1-50", "rows 51-100"]
    assert result.truncated.kind == "time" and result.truncated.reached == 120


def test_default_limits_match_the_spec():
    from hokma.docs import MAX_PAGES, MAX_PASSAGES, READ_SECONDS
    assert (MAX_PAGES, MAX_PASSAGES, READ_SECONDS) == (2000, 5000, 120.0)
