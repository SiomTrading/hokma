"""Static guarantees for installer/Instalar-Hokma.cmd, the one file the owner double-clicks on Windows.

What is NOT executed here (needs a real Windows 10/11 machine): the batch header under cmd.exe, the hidden
PowerShell, the progress window, `hokma stop`, the process cleanup under <data>, git clone and the final start.
"""
from pathlib import Path

RAW = (Path(__file__).resolve().parents[2] / "installer" / "Instalar-Hokma.cmd").read_bytes()
TEXT = RAW.decode("utf-8")
BATCH, _, POWERSHELL = TEXT.partition("#PSBEGIN")


def test_file_is_utf8_without_bom_with_crlf():
    assert not RAW.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in RAW and b"\n" not in RAW.replace(b"\r\n", b"")


def test_batch_header_ends_before_the_powershell_part_and_opens_no_console():
    assert BATCH.count("exit /b") == 1
    assert "-WindowStyle Hidden" in BATCH and "start \"\" /min" in BATCH
    assert "%~f0" in BATCH
    # the marker is only ever built from two pieces in the header, so IndexOf finds the real marker line
    assert TEXT.count("#PSBEGIN") == 1


def test_it_runs_the_repository_installer_from_main_in_a_temp_folder():
    assert "SiomTrading/hokma.git" in POWERSHELL
    assert "--branch main" in POWERSHELL and "--depth 1" in POWERSHELL
    assert "installer\\install.ps1" in POWERSHELL
    assert "$env:TEMP" in POWERSHELL


def test_it_closes_hokma_only_under_the_data_folder():
    assert "hokma.cmd" in POWERSHELL and "stop" in POWERSHELL
    assert '(Join-Path $data "runtime")' in POWERSHELL
    assert "$_.ProcessId -ne $PID" in POWERSHELL


def test_missing_git_or_claude_gets_a_clear_message_and_nothing_is_installed():
    assert "Get-Command git" in POWERSHELL and "Get-Command claude" in POWERSHELL
    assert "git-scm.com/download/win" in POWERSHELL
    assert "claude.ai/install.ps1" in POWERSHELL
    assert "winget install" not in POWERSHELL


def test_it_opens_hokma_at_the_end():
    assert POWERSHELL.rstrip().endswith("exit 0")
    assert '" start' in POWERSHELL
