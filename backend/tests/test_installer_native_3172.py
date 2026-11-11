"""Task 3172: Windows PowerShell 5.1 with ErrorActionPreference=Stop turns every stderr line of a native program into a
terminating NativeCommandError. uv writes its progress to stderr, so `& $Uv sync` stopped the install of 0.0.23 on a
Windows Server ("uv.exe : Using CPython 3.12.13 ... NativeCommandError ... exit code 1"). Static guards: no native program
is called outside Invoke-Native (or Invoke-Git, which calls it) in any installer script.
"""
import re
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[2] / "installer"
SCRIPTS = [p for p in INSTALLER.rglob("*.ps1")]
NATIVE = INSTALLER / "native.ps1"
HELPER = INSTALLER / "windows" / "setup-helper.ps1"
# The one place allowed to use the call operator: the function bodies themselves.
CALL = re.compile(r"(?<![\w&])&\s+(?!&)(\S+)")
BARE = re.compile(r"^\s*(uv|git|node|bun|claude|python|pip|npm|winget|robocopy|hokma|powershell|pwsh|cmd)(\.exe)?\s", re.I)
START_PROCESS_NATIVE = re.compile(r"(Start-Process|\bInvoke-Expression\b|\biex\b)", re.I)


def _strip(text):
    return "\n".join(line.split(" #")[0] if not line.lstrip().startswith("#") else "" for line in text.splitlines())


def test_scripts_exist_and_are_ascii_without_em_dash():
    assert NATIVE.is_file() and HELPER.is_file()
    for path in SCRIPTS:
        text = path.read_bytes().decode("utf-8")
        assert chr(0x2014) not in text, path
        if path.name != "messages.ps1":
            text.encode("ascii")


def test_invoke_native_lowers_error_action_only_during_the_call():
    for path in (NATIVE, HELPER):
        text = path.read_text()
        body = text.split("function Invoke-Native")[1]
        body = body.split("\nfunction ")[0]
        assert '$prev = $ErrorActionPreference' in body, path.name
        assert '$ErrorActionPreference = "Continue"' in body, path.name
        assert "finally { $ErrorActionPreference = $prev }" in body, path.name
        assert "2>&1" in body and "$LASTEXITCODE" in body, path.name
        assert '"$_"' in body, path.name                     # stderr lines forwarded as text, not as error records


def test_no_script_calls_a_native_program_directly():
    for path in SCRIPTS:
        for number, line in enumerate(_strip(path.read_text()).splitlines(), 1):
            if path.name in ("native.ps1",) or (path == HELPER and "& $file @arguments 2>&1" in line):
                continue
            match = CALL.search(line.replace("& $Report (", "(") if path.name == "download.ps1" else line)  # $Report is a scriptblock
            assert not match, f"{path.name}:{number}: raw call operator outside Invoke-Native: {line.strip()}"
            assert not BARE.match(line), f"{path.name}:{number}: bare native call: {line.strip()}"


def test_native_calls_are_not_judged_by_a_leaked_last_exit_code():
    """Nothing reads $LASTEXITCODE outside the two Invoke-Native bodies: callers use the returned code."""
    for path in SCRIPTS:
        text = _strip(path.read_text())
        if path.name == "native.ps1" or path == HELPER:
            outside = text.split("function Invoke-Native")[0] + text.split("function Invoke-Native")[1].split("\nfunction ", 1)[-1]
            if path == HELPER:
                assert "$LASTEXITCODE" not in outside
            continue
        assert "$LASTEXITCODE" not in text, path.name


def test_install_runs_every_native_step_through_the_function():
    text = (INSTALLER / "install.ps1").read_text()
    assert '. (Join-Path $PSScriptRoot "native.ps1")' in text
    for needle in ('Invoke-Native $Uv @("python", "install", $PyVersion)',
                   'Invoke-Native $Uv @("sync", "--project", $App, "--frozen", "--no-dev", "--python", $PyVersion)',
                   'Invoke-Native "robocopy"', 'Invoke-Native $HokmaExe @("write-manifest")',
                   'Invoke-Native $HokmaExe @("verify-install")', 'Invoke-Native $HokmaExe @("install-tools")',
                   'Invoke-Native $HokmaExe @("kit-sync")', 'Invoke-Native $HokmaExe @("init-config")',
                   'Invoke-Native $HokmaExe @("shortcuts", "--force")', 'Invoke-Native $launcherPath @("doctor")',
                   'Invoke-Native "git" ([string[]]$args)'):
        assert needle in text, needle


def test_helper_forwards_native_output_as_lines():
    text = HELPER.read_text()
    assert 'Emit ("LINE:" + $text)' in text
    assert "Invoke-Logged" not in text
    assert text.count("Invoke-Native ") >= 6


def test_uninstall_has_no_native_call_and_ships_with_what_it_uses():
    text = _strip((INSTALLER / "uninstall.ps1").read_text())
    assert not CALL.search(text) and "Start-Process" not in text


def test_windows_server_is_logged_not_blocked():
    install = (INSTALLER / "install.ps1").read_text()
    native = NATIVE.read_text()
    assert "function Test-WindowsServer" in native and "InstallationType" in native
    assert 'Info "inst.windows_edition" (Get-WindowsEdition)' in install
    assert 'if ($IsServer) { Warn "inst.server_admin" } else { Die "inst.no_admin" }' in install
    for lang in ("es", "en"):
        props = (INSTALLER / "messages" / f"{lang}.properties").read_text(encoding="utf-8")
        assert "inst.windows_edition=" in props and "inst.server_admin=" in props
    assert "Get-WindowsEdition" in HELPER.read_text()


def test_the_windows_server_workflow_runs_install_ps1():
    wf = (INSTALLER.parent / ".github" / "workflows" / "desktop-check.yml").read_text()
    assert "install-windows-server" in wf and "windows-2022" in wf and "install.ps1" in wf
