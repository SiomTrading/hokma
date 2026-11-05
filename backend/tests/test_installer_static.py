"""Static checks of the installer files. Run everywhere, no network."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[2] / "installer"
PLATFORMS = {"linux-x86_64", "linux-aarch64", "macos-aarch64", "macos-x86_64",
             "windows-x86_64", "windows-aarch64"}


def test_lock_json_complete():
    lock = json.loads((INSTALLER / "lock.json").read_text())
    assert lock["uv_version"] and lock["python_version"]
    assert set(lock["platforms"]) == PLATFORMS
    for name, entry in lock["platforms"].items():
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), name
        assert entry["url"].startswith("https://") and lock["uv_version"] in entry["url"], name


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
def test_bash_syntax(script):
    bash = shutil.which("bash")
    assert bash
    r = subprocess.run([bash, "-n", str(INSTALLER / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
def test_shell_scripts_are_executable(script):
    assert (INSTALLER / script).stat().st_mode & 0o100


@pytest.mark.parametrize("script", ["install.ps1", "uninstall.ps1"])
def test_powershell_static_rules(script):
    text = (INSTALLER / script).read_bytes()
    text.decode("ascii")  # Windows PowerShell 5.1 reads BOM-less files as ANSI
    src = text.decode("ascii")
    assert "RunAs" not in src and "-Verb" not in src, "must never elevate"
    assert src.count("{") == src.count("}")
    assert src.count("(") == src.count(")")


def test_install_scripts_verify_sha256_before_executing():
    sh = (INSTALLER / "install.sh").read_text()
    assert sh.index("sha256_of \"$ARCHIVE\"") < sh.index('tar -xzf "$ARCHIVE"')
    ps = (INSTALLER / "install.ps1").read_text()
    assert ps.index("Get-FileHash") < ps.index("Expand-Archive")


def test_installers_do_not_touch_personal_claude_dir():
    for script in ("install.sh", "install.ps1", "uninstall.sh", "uninstall.ps1"):
        src = (INSTALLER / script).read_text()
        assert not re.search(r"(~|HOME|USERPROFILE)[/\\\"]*\.claude(?![-\w])", src), script


def test_powershell_parser_if_available():
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("pwsh not installed; PowerShell syntax is NOT verified here")
    for script in ("install.ps1", "uninstall.ps1"):
        cmd = ("$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{INSTALLER / script}',[ref]$null,[ref]$e);if($e){{$e;exit 1}}")
        r = subprocess.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
