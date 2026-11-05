"""Guard logic for Windows and macOS, exercised on Linux (ntpath, simulated case-insensitive FS)."""
import os

import pytest

from hokma.kit import guard


def decide(target, protect, cwd, tool="Write"):
    return guard.decide({"tool_name": tool, "tool_input": {"file_path": str(target)}, "cwd": str(cwd)},
                        [str(p) for p in protect])[0]


@pytest.fixture
def dirs(tmp_path):
    data, work = tmp_path / "data", tmp_path / "work"
    (data / "claude-config").mkdir(parents=True)
    work.mkdir()
    return data, work


@pytest.mark.parametrize("target", [
    "\\\\?\\C:\\Users\\x\\AppData\\Local\\Hokma\\a",
    "\\\\.\\C:\\x",
    "//?/C:/x",
    "//./C:/x",
    "\\\\server\\share\\x",
    "//server/share/x",
    "\\\\?\\UNC\\server\\share\\x",
])
def test_device_and_unc_paths_are_blocked(dirs, target):
    data, work = dirs
    assert guard.is_device_or_unc(target)
    assert decide(target, [data], work) == 2


@pytest.mark.parametrize("target", ["C:\\Users\\x", "c:/work/a.txt", "relative/a.txt", "/home/u/a.txt"])
def test_plain_drive_letters_and_posix_paths_are_not_device_paths(target):
    assert not guard.is_device_or_unc(target)


def test_unc_cwd_is_blocked(dirs):
    data, _ = dirs
    assert guard.decide({"tool_name": "Write", "tool_input": {"file_path": "x"}, "cwd": "//server/share"},
                        [str(data)])[0] == 2


def test_commonpath_valueerror_counts_as_protected(dirs, monkeypatch):
    data, work = dirs

    def boom(paths):
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr(guard.os.path, "commonpath", boom)
    assert guard._inside("/anything", ["/root"]) is True
    assert decide(work / "notes.md", [data], work) == 2


def test_case_insensitive_fs_compares_casefolded(dirs, monkeypatch):
    data, work = dirs
    monkeypatch.setattr(guard, "CASE_INSENSITIVE", True)
    upper = str(data).upper()     # different letter case, pretend the FS ignores it
    assert decide(upper + "/Claude-Config/SETTINGS.json", [data], work) == 2
    assert decide(work / "notes.md", [data], work) == 0


def test_case_sensitive_fs_keeps_case(dirs, monkeypatch):
    data, work = dirs
    monkeypatch.setattr(guard, "CASE_INSENSITIVE", False)
    other = str(work / "Data-Lookalike" / "x")
    assert decide(other, [data], work) == 0


def test_identity_of_deepest_existing_ancestor_beats_name_tricks(dirs, monkeypatch):
    """Names differ (an alias path), but the existing ancestor is the same directory by inode."""
    data, work = dirs
    monkeypatch.setattr(guard, "CASE_INSENSITIVE", False)
    alias = work / "alias"
    alias.symlink_to(data)                       # realpath already catches this...
    monkeypatch.setattr(guard.os.path, "realpath", lambda p: p)   # ...so disable realpath to isolate inode logic
    assert decide(alias / "claude-config" / "new.json", [data], work) == 2
    assert guard._same_identity_as_root(str(alias / "not-yet-created.json"),
                                        [guard._identity(str(data))]) is True


def test_hardlink_to_a_protected_file_is_blocked(dirs):
    data, work = dirs
    protected = data / "claude-config" / "settings.json"
    protected.write_text("{}")
    os.link(protected, work / "innocent.json")
    assert decide(work / "innocent.json", [data / "elsewhere"], work) == 2   # nlink > 1, even if root differs
    assert decide(work / "plain.txt", [data], work) == 0
