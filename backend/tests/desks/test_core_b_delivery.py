"""OA-13: a file made by the desk is delivered in the reply when it is inside the desk folder; outside is rejected."""
import os
import sys

import pytest

from hokma.desks.core import delivery


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "desk 1"
    (base / "reports").mkdir(parents=True)
    (base / "reports" / "summary.pdf").write_bytes(b"%PDF-1.4 test")
    (base / "reports" / "chart.png").write_bytes(b"png-bytes")
    (base / "notes.md").write_text("hello", encoding="utf-8")
    return base


def kinds(result):
    return [(p.kind, p.text) for p in result.pieces]


def test_a_file_inside_the_desk_is_delivered_at_its_position_OA_13(root):
    reply = "Here is the report.\n[[reports/summary.pdf]]\nThe chart shows it.\n[[reports/chart.png]]\nDone."
    result = delivery.deliver(reply, root)
    assert [p.kind for p in result.pieces] == ["text", "file", "text", "file", "text"]
    assert [p.text for p in result.files] == ["reports/summary.pdf", "reports/chart.png"]
    assert result.files[0].path == (root / "reports" / "summary.pdf").resolve() and result.files[0].size == 13
    assert result.files[1].as_image is True and result.files[0].as_image is False and not result.rejected
    assert result.text.startswith("Here is the report.") and "[[" not in result.text


@pytest.mark.parametrize("ref,reason", [
    ("../secret.txt", "parent"), ("reports/../../secret.txt", "parent"), ("/etc/passwd.txt", "absolute"), ("C:/Windows/x.txt", "absolute"),
    ("~/x.txt", "absolute"), ("file:reports/summary.pdf", "prefix"), ("reports/summary.pdf|label", "label"),
    ("reports/run.exe", "unsupported"), ("reports/missing.pdf", "missing"), ("reports", "unsupported"), ("reports//summary.pdf", "parent"),
])
def test_a_path_outside_or_malformed_is_rejected_and_stays_text_OA_13(root, ref, reason):
    result = delivery.deliver(f"See [[{ref}]] now", root)
    assert not result.files and [r.reason for r in result.rejected] == [reason]
    assert f"[[{ref}]]" in result.text


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_a_link_that_points_outside_is_rejected_OA_13(root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, root / "reports" / "link.txt")
    result = delivery.deliver("[[reports/link.txt]]", root)
    assert not result.files and result.rejected[0].reason == "outside"


def test_a_folder_is_not_a_file_OA_13(root):
    (root / "folder.md").mkdir()
    assert delivery.deliver("[[folder.md]]", root).rejected[0].reason == "not_file"


def test_references_in_code_are_examples_not_deliveries_OA_13(root):
    reply = "Syntax: `[[notes.md]]`\n```\n[[reports/summary.pdf]]\n```\nReal: [[notes.md]]"
    result = delivery.deliver(reply, root)
    assert [p.text for p in result.files] == ["notes.md"] and not result.rejected


def test_the_limits_are_five_files_and_one_mebibyte_OA_13(root):
    for i in range(7):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")
    reply = " ".join(f"[[f{i}.txt]]" for i in range(7))
    result = delivery.deliver(reply, root)
    assert len(result.files) == 5 and [r.reason for r in result.rejected] == ["limit", "limit"]
    big = root / "big.csv"
    big.write_bytes(b"0" * (delivery.MAX_BYTES + 1))
    assert delivery.deliver("[[big.csv]]", root).rejected[0].reason == "too_big"
    big.write_bytes(b"0" * delivery.MAX_BYTES)
    assert len(delivery.deliver("[[big.csv]]", root).files) == 1


def test_the_same_file_named_twice_is_delivered_once_OA_13(root):
    result = delivery.deliver("a [[notes.md]] b [[notes.md]] c", root)
    assert len(result.files) == 1 and not result.rejected and result.text == "a  b  c"


def test_a_reply_without_references_is_one_text_piece_OA_13(root):
    assert kinds(delivery.deliver("plain", root)) == [("text", "plain")] and kinds(delivery.deliver("", root)) == []


def test_a_missing_desk_folder_is_an_error_OA_13(tmp_path):
    with pytest.raises(delivery.DeliveryError):
        delivery.deliver("[[a.txt]]", tmp_path / "nope")
