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


def test_missing_git_stops_the_install_and_nothing_is_installed():
    assert "Get-Command git" in POWERSHELL and "Get-Command claude" in POWERSHELL
    assert "git-scm.com/download/win" in POWERSHELL
    assert "if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail $T.no_git }" in POWERSHELL
    assert "winget install" not in POWERSHELL


def test_missing_claude_is_a_warning_and_the_install_goes_on():
    assert "Fail $T.no_claude" not in POWERSHELL
    assert "$claudeMissing" in POWERSHELL
    assert "claude.ai/install.ps1" in POWERSHELL
    assert "Recomendamos Claude Code para chatear; puedes instalarlo " in POWERSHELL
    opened = POWERSHELL.index("Show-Step $T.opening")
    assert opened < POWERSHELL.index("if ($claudeMissing) { Show-Box")      # Hokma is already open when the notice shows


def test_it_opens_hokma_at_the_end():
    assert POWERSHELL.rstrip().endswith("exit 0")
    assert '" start' in POWERSHELL


def test_clone_success_is_decided_by_exit_code_not_by_stderr():
    # git writes "Cloning into ..." to stderr even when it works: that text must never decide anything.
    assert "clone --quiet" in POWERSHELL
    assert "function Test-Succeeded" in POWERSHELL
    assert "if (-not (Test-Succeeded $code $installScript))" in POWERSHELL
    assert "$null = $p.Handle" in POWERSHELL          # ExitCode is reliable on Windows PowerShell 5.1
    assert "-RedirectStandardError ($log" in POWERSHELL    # stderr goes to a file, never into the error stream
    assert "2>&1" not in POWERSHELL
    assert "NativeCommandError" not in POWERSHELL
    assert "core.longpaths=true" in POWERSHELL


def test_failure_message_carries_the_real_detail_and_the_git_code():
    assert '"`r`n(git: " + $code + ")"' in POWERSHELL
