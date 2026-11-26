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


def test_licenses_of_the_lgpl_mpl_and_apache_dependencies_are_given():
    """Task 3324: the notices of the LGPL, MPL and Apache-2.0 material travel with the source package, with their license texts."""
    notice = NOTICE.read_text(encoding="utf-8")
    listing = (ROOT / "legal" / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8")
    assert "There are no entries" not in listing
    for name in ("psycopg", "python-telegram-bot", "portion", "certifi", "orjson", "tqdm", "QuantConnect"):
        assert name in notice, name
        assert name in listing, name
    texts = {"LGPL-3.0.txt": "GNU LESSER GENERAL PUBLIC LICENSE", "GPL-3.0.txt": "GNU GENERAL PUBLIC LICENSE",
             "MPL-2.0.txt": "Mozilla Public License Version 2.0", "Apache-2.0.txt": "Apache License"}
    for name, heading in texts.items():
        assert heading in (ROOT / "legal" / "licenses" / name).read_text(encoding="utf-8"), name
