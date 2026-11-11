"""Guard logic for Windows and macOS, exercised on Linux (ntpath, simulated case-insensitive FS)."""
import ntpath
import os
import types

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
    """Ambiguous cases keep the fail-closed answer: no drive on one side, mixed types, same drive."""
    data, work = dirs

    def boom(paths):
        raise ValueError("Paths can't be compared")

    monkeypatch.setattr(guard.os.path, "commonpath", boom)
    assert guard._inside("/anything", ["/root"]) is True
    assert guard._inside("D:\\work", ["relative\\root"]) is True      # mixed absolute and relative
    assert guard._inside("C:\\a", ["c:\\b"]) is True                  # same drive, case-folded: still ambiguous
    assert guard._inside(b"D:\\work", ["C:\\root"]) is True           # mixed types
    assert decide(work / "notes.md", [data], work) == 2


@pytest.fixture
def windows_paths(monkeypatch):
    """The guard sees Windows path rules (ntpath) while the rest of the process keeps its own os.path."""
    monkeypatch.setattr(guard, "os", types.SimpleNamespace(path=ntpath, stat=os.stat))


def test_different_drives_are_not_inside(windows_paths):
    """Windows: ntpath.commonpath raises ValueError across drives; D: can never be inside a root on C:."""
    assert guard._inside("D:\\work\\notes.md", ["C:\\Users\\x\\Hokma"]) is False
    assert guard._inside("d:\\work", ["C:\\Users\\x\\Hokma", "E:\\other"]) is False
    assert guard._inside("C:\\Users\\x\\Hokma\\claude-config\\settings.json", ["C:\\Users\\x\\Hokma"]) is True
    assert guard._inside("c:\\users\\x\\hokma\\a", ["c:\\users\\x\\hokma"]) is True
    # a second root on the same drive as the path still protects it, whatever the first root's drive is
    assert guard._inside("D:\\data\\a", ["C:\\x", "D:\\data"]) is True


def test_work_folder_on_d_is_not_protected_by_roots_on_c(windows_paths):
    protect = ["C:\\Users\\x\\AppData\\Local\\Hokma"]
    assert guard.under_any("D:\\work\\notes.md", protect) is False                    # hardblock: Read
    assert guard.decide({"tool_name": "Edit", "tool_input": {"file_path": "D:\\work\\notes.md"},
                         "cwd": "D:\\work"}, protect)[0] == 0                          # hardblock: Edit
    assert guard.decide({"tool_name": "Write", "tool_input": {"file_path": "notes.md"},
                         "cwd": "D:\\work"}, protect)[0] == 0


def test_real_protected_path_on_c_stays_protected(windows_paths):
    protect = ["C:\\Users\\x\\AppData\\Local\\Hokma"]
    assert guard.under_any("C:\\Users\\x\\AppData\\Local\\Hokma\\claude-config\\settings.json", protect) is True
    assert guard.under_any("c:\\users\\X\\appdata\\local\\hokma", protect) is True
    assert guard.decide({"tool_name": "Edit", "tool_input": {
        "file_path": "C:\\Users\\x\\AppData\\Local\\Hokma\\claude-config\\settings.json"},
        "cwd": "D:\\work"}, protect)[0] == 2


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
    assert decide(work / "innocent.json", [protected], work) == 2             # the same inode as a protected file
    assert decide(work / "innocent.json", [data / "elsewhere"], work) == 0   # a hard link to something not protected
    assert decide(work / "plain.txt", [data], work) == 0
