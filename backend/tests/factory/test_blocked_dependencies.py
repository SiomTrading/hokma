"""AS-002 to AS-004: libraries marked inactive, archived or unavailable never enter the dependency set.

backend/hokma/factory/data/blocked_dependencies.txt lists package names; uv.lock and the direct dependencies of pyproject.toml must not contain them.
"""
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BLOCKED_FILE = ROOT / "backend" / "hokma" / "factory" / "data" / "blocked_dependencies.txt"
STATES = {"dormant": 27, "archived": 6, "unavailable": 3}


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def blocked(text: str) -> dict[str, tuple[str, str]]:
    """name -> (state, since) from the text of the list; comments and blank lines are ignored."""
    entries: dict[str, tuple[str, str]] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        assert len(parts) == 3, f"line {number}: expected 'name | state | since'"
        name, state, since = parts
        assert name == normalise(name), f"line {number}: {name!r} is not a normalised package name"
        assert name not in entries, f"line {number}: {name!r} appears twice"
        entries[name] = (state, since)
    return entries


def locked_names(lock_text: str) -> set[str]:
    return {normalise(m) for m in re.findall(r'^name = "([^"]+)"', lock_text, re.M)}


def direct_names(pyproject_text: str) -> set[str]:
    data = tomllib.loads(pyproject_text)
    requirements = list(data.get("project", {}).get("dependencies", []))
    for group in data.get("project", {}).get("optional-dependencies", {}).values():
        requirements += group
    for group in data.get("dependency-groups", {}).values():
        requirements += [r for r in group if isinstance(r, str)]
    return {normalise(re.split(r"[\s<>=!~;\[(]", r, maxsplit=1)[0]) for r in requirements}


def test_the_list_has_the_counts_of_the_source_review():
    entries = blocked(BLOCKED_FILE.read_text(encoding="utf-8"))
    counts = {state: sum(1 for s, _ in entries.values() if s == state) for state in STATES}
    assert counts == STATES
    assert len(entries) == 36


def test_dormant_entries_carry_a_month_and_the_others_do_not():
    for name, (state, since) in blocked(BLOCKED_FILE.read_text(encoding="utf-8")).items():
        if state == "dormant":
            assert re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", since), name
        else:
            assert since == "-", name


def test_the_lock_file_holds_no_blocked_library():
    names = blocked(BLOCKED_FILE.read_text(encoding="utf-8")).keys()
    locked = locked_names((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert len(locked) > 50, "uv.lock was not read: too few packages"
    assert sorted(set(names) & locked) == []


def test_the_direct_dependencies_hold_no_blocked_library():
    names = blocked(BLOCKED_FILE.read_text(encoding="utf-8")).keys()
    direct = direct_names((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert direct, "pyproject.toml was not read"
    assert sorted(set(names) & direct) == []


def test_a_blocked_library_in_a_lock_is_detected():
    lock = '[[package]]\nname = "Zipline"\nversion = "1.0"\n\n[[package]]\nname = "ib_insync"\nversion = "0.9"\n\n[[package]]\nname = "numpy"\n'
    names = blocked(BLOCKED_FILE.read_text(encoding="utf-8")).keys()
    assert sorted(set(names) & locked_names(lock)) == ["ib-insync", "zipline"]


def test_a_blocked_library_in_the_direct_dependencies_is_detected():
    text = '[project]\nname = "x"\ndependencies = ["pandas-ta>=0.3", "numpy"]\n'
    names = blocked(BLOCKED_FILE.read_text(encoding="utf-8")).keys()
    assert sorted(set(names) & direct_names(text)) == ["pandas-ta"]


@pytest.mark.parametrize("bad", ["zipline | dormant", "Zipline | dormant | 2024-02", "a | dormant | 2024-02\na | archived | -"])
def test_a_malformed_list_is_rejected(bad):
    with pytest.raises(AssertionError):
        blocked(bad)
