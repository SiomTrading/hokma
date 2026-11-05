"""Where git and gh may be found: the working folder and the exact cwd are refused, user installs are not."""
import os

import pytest

from hokma.update import safe_exec


def make_program(folder, name="git"):
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


@pytest.fixture(autouse=True)
def posix_only():
    if os.name != "posix":
        pytest.skip("uses executable shell stubs")


def test_a_per_user_install_under_the_cwd_is_allowed(monkeypatch, tmp_path):
    home = tmp_path / "home"                                       # the cwd is an ancestor of the install
    exe = make_program(home / "AppData" / "Local" / "Programs" / "Git" / "cmd")
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "elsewhere" / "Hokma"))
    monkeypatch.chdir(home)
    monkeypatch.setenv("PATH", str(exe.parent))
    assert safe_exec.find_executable("git") == str(exe)


def test_the_cwd_itself_and_relative_or_empty_entries_are_refused(monkeypatch, tmp_path):
    home = tmp_path / "home"
    make_program(home, "gh")
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "elsewhere" / "Hokma"))
    monkeypatch.chdir(home)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "relative/bin", str(home)]))
    assert safe_exec.find_executable("gh") is None


def test_a_cwd_inside_the_workdir_blocks_everything_below_it(monkeypatch, tmp_path):
    work = tmp_path / "home" / "Hokma"
    sub = work / "project"
    make_program(sub / "bin")
    monkeypatch.setenv("HOKMA_HOME", str(work))
    monkeypatch.chdir(sub)
    monkeypatch.setenv("PATH", str(sub / "bin"))
    assert safe_exec.find_executable("git") is None
    monkeypatch.chdir(tmp_path)                                   # blocked even when the cwd is elsewhere
    assert safe_exec.find_executable("git") is None


def test_a_link_in_a_trusted_folder_that_points_into_the_workdir_is_refused(monkeypatch, tmp_path):
    work = tmp_path / "Hokma"
    target = make_program(work / "bin", "gh")
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (trusted / "gh").symlink_to(target)
    monkeypatch.setenv("HOKMA_HOME", str(work))
    monkeypatch.chdir(tmp_path / "trusted")
    monkeypatch.setenv("PATH", str(trusted))
    assert safe_exec.find_executable("gh") is None
