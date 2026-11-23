"""Merge changelog fragments from changelog.d/ into CHANGELOG.md.

    python scripts/changelog_merge.py 1.1.0        write a "## 1.1.0" section and delete the fragments
    python scripts/changelog_merge.py --check      validate the fragments only
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENTS = ROOT / "changelog.d"
CHANGELOG = ROOT / "CHANGELOG.md"
TYPES = ("new", "changed", "fixed", "removed")
ORDER = {t: i for i, t in enumerate(TYPES)}
NAME = re.compile(r"^(?P<slug>[A-Za-z0-9][A-Za-z0-9_-]*)\.(?P<type>new|changed|fixed|removed)\.md$")


def collect(directory: Path = FRAGMENTS) -> list[tuple[str, str, str, Path]]:
    """Return (type, slug, text, path) for every fragment; raise ValueError on a bad name or an empty file."""
    found = []
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md":
            continue
        match = NAME.match(path.name)
        if not match:
            raise ValueError(f"{path.name}: the name must be <slug>.<new|changed|fixed|removed>.md")
        text = " ".join(path.read_text(encoding="utf-8").split())
        if not text:
            raise ValueError(f"{path.name}: the fragment is empty")
        found.append((match["type"], match["slug"], text, path))
    return sorted(found, key=lambda f: (ORDER[f[0]], f[1]))


def render_section(version: str, fragments: list[tuple[str, str, str, Path]]) -> str:
    lines = [f"## {version}", ""]
    lines += [f"- {kind.capitalize()}: {text}" for kind, _slug, text, _path in fragments]
    return "\n".join(lines) + "\n"


def merge(version: str, *, directory: Path = FRAGMENTS, changelog: Path = CHANGELOG) -> int:
    fragments = collect(directory)
    if not fragments:
        return 0
    body = changelog.read_text(encoding="utf-8")
    head, sep, rest = body.partition("\n## ")
    section = render_section(version, fragments)
    changelog.write_text(f"{head}\n{section}\n## {rest}" if sep else f"{head.rstrip()}\n\n{section}", encoding="utf-8")
    for *_unused, path in fragments:
        path.unlink()
    return len(fragments)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    try:
        if argv[1] == "--check":
            collect()
            return 0
        print(f"merged {merge(argv[1])} fragment(s) into {CHANGELOG.name}")
        return 0
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
