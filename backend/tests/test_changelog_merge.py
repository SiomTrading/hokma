"""Changelog fragments (SPEC-017: ED-046, TA-96)."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("changelog_merge", ROOT / "scripts" / "changelog_merge.py")
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)


def test_merge_orders_and_deletes_fragments(tmp_path):
    frag = tmp_path / "frag"
    frag.mkdir()
    (frag / "b.fixed.md").write_text("A bug is fixed.\n")
    (frag / "a.new.md").write_text("A feature exists.\n")
    log = tmp_path / "CHANGELOG.md"
    log.write_text("# Changelog\n\n## 1.0.0\n\n- New: first.\n")
    assert cm.merge("1.1.0", directory=frag, changelog=log) == 2
    text = log.read_text()
    assert text.index("## 1.1.0") < text.index("## 1.0.0")
    assert text.index("- New: A feature exists.") < text.index("- Fixed: A bug is fixed.")
    assert list(frag.glob("*.md")) == []


def test_bad_names_and_empty_files_fail(tmp_path):
    (tmp_path / "x.oops.md").write_text("text")
    with pytest.raises(ValueError):
        cm.collect(tmp_path)
    (tmp_path / "x.oops.md").unlink()
    (tmp_path / "y.new.md").write_text("  \n")
    with pytest.raises(ValueError):
        cm.collect(tmp_path)


def test_repository_fragments_are_valid():
    cm.collect()


def test_h1_fragments_describe_the_release_checklist():
    texts = " ".join(text for _kind, _slug, text, _path in cm.collect())
    # The H1 entry lives in its fragment until the release step merges it into the 1.0.12 section. Fragments of
    # other tasks can coexist with it, so the section is read too instead of only when no fragment is left.
    log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if "## 1.0.12" in log:
        texts += " " + log.split("## 1.0.12", 1)[1].split("\n## ", 1)[0]
    assert "metrics, risk, validation and data modules" in texts
    assert "Integrity checklist for this release" in texts
    assert not (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").startswith("# Changelog\n\n## Unreleased")
