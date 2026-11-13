"""SPEC-011 P14: the third-party notice exists, carries the MIT text, and travels inside the source package that the
desktop app embeds (`git archive HEAD`, then the installer copies the whole tree)."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTICE = ROOT / ".notices" / "THIRD_PARTY.txt"


def test_notice_has_the_license_text():
    text = NOTICE.read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Copyright (c) 2025" in text
    assert "Permission is hereby granted, free of charge" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text


def test_notice_is_in_the_source_package():
    if not (ROOT / ".git").exists():
        return
    listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", ".notices/THIRD_PARTY.txt"], capture_output=True,
                             text=True, check=True).stdout
    assert listing.strip() == ".notices/THIRD_PARTY.txt"
    attributes = ROOT / ".gitattributes"
    assert not attributes.exists() or "export-ignore" not in attributes.read_text()
