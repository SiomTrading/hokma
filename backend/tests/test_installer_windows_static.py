"""Static guarantees for install.ps1 that can only be *run* on Windows.

What is NOT executed here (needs a real Windows 10/11 machine, ideally with a user name
containing accents or an enye, on PowerShell 5.1 and 7): the whole script, `hokma.cmd`
under cmd.exe with `chcp 65001`, robocopy, the user PATH change and git behaviour with CRLF.
"""
from pathlib import Path

PS = (Path(__file__).resolve().parents[2] / "installer" / "install.ps1").read_text()
UNINSTALL = (Path(__file__).resolve().parents[2] / "installer" / "uninstall.ps1").read_text()


def test_launcher_is_utf8_without_bom_and_switches_codepage():
    assert "WriteAllLines" in PS and "UTF8Encoding($false)" in PS
    assert "chcp 65001" in PS
    assert "Set-Content -Path $launcherPath" not in PS and "hokma.cmd\") -Value" not in PS


def test_uninstaller_reads_launcher_as_utf8():
    assert "-Encoding UTF8" in UNINSTALL


def test_git_calls_do_not_trip_error_action_preference_stop():
    assert "function Invoke-Git" in PS
    assert 'ErrorActionPreference = "Continue"' in PS
    assert "core.autocrlf true" in PS
    for line in PS.splitlines():
        if line.strip().startswith(("&  git", "& git")):
            raise AssertionError(f"raw git call outside Invoke-Git: {line}")
    # no git invocation redirects stderr with 2>$null (that raises under Stop in PS 5.1)
    assert "git" not in "".join(l for l in PS.splitlines() if "2>$null" in l)


def test_runtime_dirs_are_outside_the_workdir():
    assert 'Join-Path $env:LOCALAPPDATA "Hokma"' in PS
    assert 'Join-Path $HokmaData "claude-config"' in PS
    assert 'Die "inst.overlap"' in PS
