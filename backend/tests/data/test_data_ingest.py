"""The six multimodal ingestion failure modes as checks (SPEC-017: RA-025)."""
from datetime import timedelta

from hokma.data import ingest_checks as ic

from .support import fixture_json, when


def blocks() -> list[dict]:
    return fixture_json("content_list.json")


def codes(found) -> set[str]:
    return {w.code for w in found}


def test_the_six_failure_modes_are_the_six_checks_RA_025() -> None:
    assert ic.FAILURE_MODES == ("ocr_corrupts_text", "table_structure_lost", "image_caption_misaligned", "retrieval_text_bias", "ingest_stuck", "asset_path_not_portable")
    report = ic.run_ingest_checks(blocks(), enabled_modalities=("table",), started_at=when("2024-03-01T00:00:00"),
                                  last_progress_at=when("2024-03-01T00:00:00"), now=when("2024-03-01T01:00:00"))
    assert report.codes() == set(ic.FAILURE_MODES) and not report.ok and report.checked == ic.FAILURE_MODES


def test_failure_1_ocr_corruption_triggers_its_warning_RA_025() -> None:
    found = ic.check_ocr_corruption(blocks())
    messages = " ".join(w.message for w in found)
    assert codes(found) == {"ocr_corrupts_text"} and "1O4.5" in messages and "l,250" in messages     # look-alike letters inside numbers
    assert any("ligature" in w.message for w in found) and any("repeated" in w.message and w.severity == "info" for w in found)   # ligature; a watermark line on most pages
    assert ic.check_ocr_corruption([{"type": "text", "page_idx": 0, "text": "Revenue was 104.5 million, up 12%"}]) == []
    second = ic.check_ocr_corruption([{"type": "text", "text": "net income 1,250"}], source_figures={0: ["1,250", "7,000"]})
    assert second and "7,000" in second[0].message and "1,250" not in second[0].message


def test_failure_2_table_structure_lost_triggers_its_warning_RA_025() -> None:
    found = ic.check_table_structure(blocks())
    by_block = {w.blocks[0]: w for w in found}
    assert by_block[4].severity == "warning" and "flattened" in by_block[4].message                    # rows and cells lost
    assert by_block[5].severity == "warning" and "different numbers of cells" in by_block[5].message   # ragged rows
    assert by_block[6].severity == "info" and "merged cells" in by_block[6].message                    # merged cells need another parser
    assert ic.check_table_structure([{"type": "table", "table_body": ""}])[0].message == "table block has no body"
    good = [{"type": "table", "table_body": "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"}]
    assert ic.check_table_structure(good) == []
    assert ic.check_table_structure([{"type": "table", "table_body": "| a | b |\n|---|---|\n| 1 | 2 |"}]) == []


def test_failure_3_image_caption_misalignment_triggers_its_warning_RA_025() -> None:
    messages = [w.message for w in ic.check_caption_alignment(blocks())]
    assert any("without a file path" in m for m in messages) and any("without a caption" in m for m in messages)
    assert any("2 different captions" in m for m in messages)                                          # one file, Figure 1 and Figure 2
    assert any("page index goes back" in m for m in messages)
    ordered = [{"type": "image", "page_idx": 0, "img_path": "a.png", "image_caption": ["Figure 1"]}, {"type": "image", "page_idx": 1, "img_path": "b.png", "image_caption": "Figure 2"}]
    assert ic.check_caption_alignment(ordered) == []


def test_failure_4_retrieval_biased_to_text_triggers_its_warning_RA_025() -> None:
    found = ic.check_multimodal_indexing(blocks(), enabled_modalities=("table",))
    assert any("image processing is off" in w.message for w in found) and not any("table processing is off" in w.message for w in found)
    described = [{"type": "table", "description": "revenue by quarter"}, {"type": "image"}]
    found = ic.check_multimodal_indexing(described, enabled_modalities=("table", "image"))
    assert len(found) == 1 and found[0].blocks == (1,) and "no enriched description" in found[0].message
    assert ic.check_multimodal_indexing([{"type": "text"}], enabled_modalities=()) == []


def test_failure_5_slow_is_told_apart_from_stuck_RA_025() -> None:
    start = when("2024-03-01T00:00:00")
    assert ic.check_progress(start, start + timedelta(minutes=2), now=start + timedelta(minutes=3)) == []
    slow = ic.check_progress(start, start + timedelta(minutes=9), now=start + timedelta(minutes=10))
    assert [w.severity for w in slow] == ["info"] and "slow, not stuck" in slow[0].message
    stuck = ic.check_progress(start, start + timedelta(minutes=1), now=start + timedelta(minutes=30))
    assert [w.severity for w in stuck] == ["warning"] and "hang" in stuck[0].message and stuck[0].code == "ingest_stuck"


def test_failure_6_local_paths_without_a_public_url_trigger_its_warning_RA_025() -> None:
    found = ic.check_asset_paths(blocks())
    assert len(found) == 1 and found[0].code == "asset_path_not_portable" and found[0].blocks == (7, 8)
    portable = [{"type": "image", "img_path": "/home/student/a.png", "img_public_url": "https://cdn.test/a.png"}, {"type": "image", "img_path": "images/a.png"},
                {"type": "image", "img_path": "C:\\Users\\student\\a.png"}]
    assert ic.check_asset_paths(portable)[0].blocks == (2,)                                             # a drive path is absolute too


def test_a_clean_document_passes_all_six_RA_025() -> None:
    clean = [{"type": "text", "page_idx": 0, "text": "Revenue was 104.5 million."},
             {"type": "table", "page_idx": 0, "table_body": "<table><tr><td>a</td></tr></table>", "description": "revenue"},
             {"type": "image", "page_idx": 1, "img_path": "images/a.png", "image_caption": ["Figure 1"], "description": "chart"}]
    report = ic.run_ingest_checks(clean, started_at=when("2024-03-01T00:00:00"), last_progress_at=when("2024-03-01T00:00:30"), now=when("2024-03-01T00:01:00"))
    assert report.ok and report.warnings == ()
