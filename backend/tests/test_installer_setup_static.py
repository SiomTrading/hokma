"""Static checks of Hokma-Setup.exe (installer/windows): it cannot be compiled on Linux, so what can be read is read.
Task 3138, SPEC-001 addendum 0.0.13."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "installer"
ISS = INSTALLER / "windows" / "hokma-setup.iss"
HELPER = INSTALLER / "windows" / "setup-helper.ps1"


def _isl_keys(lang: str) -> dict[str, str]:
    text = (INSTALLER / "messages" / f"setup-{lang}.isl").read_text(encoding="utf-8-sig")
    section, out = "", {}
    for line in text.splitlines():
        if line.startswith("["):
            section = line
        elif "=" in line and not line.startswith(";") and section == "[CustomMessages]":
            key, value = line.split("=", 1)
            out[key] = value
    return out


def _properties(lang: str) -> dict[str, str]:
    lines = (INSTALLER / "messages" / f"{lang}.properties").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


def test_wizard_texts_exist_in_spanish_and_english():
    es, en = _isl_keys("es"), _isl_keys("en")
    assert es.keys() == en.keys()
    used = set(re.findall(r"CustomMessage\('(\w+)'\)", ISS.read_text(encoding="ascii")))
    used |= set(re.findall(r"\{cm:(\w+)\}", ISS.read_text(encoding="ascii")))
    assert used and used <= es.keys(), used - es.keys()


def test_isl_files_are_utf8_with_bom_and_crlf():
    for lang in ("es", "en"):
        raw = (INSTALLER / "messages" / f"setup-{lang}.isl").read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf") and raw.count(b"\n") == raw.count(b"\r\n") > 0


def test_scripts_are_ascii_and_have_no_em_dash():
    for path in (ISS, HELPER):
        path.read_text(encoding="ascii")
    for path in list((INSTALLER / "messages").glob("*")) + [ISS, HELPER]:
        assert "\u2014" not in path.read_text(encoding="utf-8-sig"), path


def test_setup_is_per_user_with_a_stable_name():
    text = ISS.read_text(encoding="ascii")
    assert "PrivilegesRequired=lowest" in text
    assert "OutputBaseFilename=Hokma-Setup" in text
    assert "WizardSizePercent=100" in text
    assert "ModePage.SelectedValueIndex := 1;" in text              # D2: Reinstall from scratch by default
    assert all("-Purge" not in line for line in text.splitlines() if line.startswith("Filename:"))


def test_helper_never_deletes_the_working_folder():
    text = HELPER.read_text(encoding="ascii")
    for line in text.splitlines():
        if "Remove-Item" in line:
            assert "$Dir" not in line and "$workdir" not in line, line
    assert "$KeepItems" in text and '"hokma.db"' in text and '"extensions.json"' in text


def test_install_rejects_onedrive_and_reloads_path():
    text = (INSTALLER / "install.ps1").read_text(encoding="ascii")
    assert 'Die "inst.onedrive"' in text
    assert 'GetEnvironmentVariable("Path", "Machine")' in text
    for lang in ("es", "en"):
        props = _properties(lang)
        for key in ("inst.onedrive", "un.reset_done", "un.reset_failed"):
            assert props.get(key), (lang, key)


def test_uninstall_reset_all_never_touches_the_working_folder():
    text = (INSTALLER / "uninstall.ps1").read_text(encoding="ascii")
    block = text.split("if ($ResetAll) {")[1].split("exit 0")[0]
    assert "$Dir" not in block.replace('Info "un.workdir_kept" $Dir', "")


def test_lock_pins_git_for_windows_with_sha256():
    lock = json.loads((INSTALLER / "lock.json").read_text(encoding="utf-8"))
    git = lock["git_for_windows"]
    for platform in ("windows-x86_64", "windows-aarch64"):
        entry = git[platform]
        assert entry["url"].startswith("https://github.com/git-for-windows/git/releases/download/")
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])


def test_powershell_scripts_do_not_depend_on_inherited_modules():
    """Task 3138, 0.0.18: a PowerShell 7 parent leaks PSModulePath and 5.1 loses Get-FileHash; no script uses it."""
    for path in (INSTALLER / "install.ps1", INSTALLER / "uninstall.ps1", HELPER):
        text = path.read_text(encoding="utf-8")
        assert "(Get-FileHash" not in text, path.name
        assert "$env:PSModulePath =" in text, path.name
    for path in (INSTALLER / "install.ps1", HELPER):
        assert "SHA256]::Create()" in path.read_text(encoding="utf-8"), path.name
    cmd = (INSTALLER / "Instalar-Hokma.cmd").read_text(encoding="utf-8")
    assert "Remove-Item Env:PSModulePath" in cmd
