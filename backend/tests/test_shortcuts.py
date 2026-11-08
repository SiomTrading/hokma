"""The one shortcut (Hokma) and the deletion of the old Shut down Hokma and Restart Hokma ones.

Linux runs for real in a temporary HOME. Windows and macOS are checked through the content and the commands that
would be produced, with a fake process runner. What needs the real systems (an actual `.lnk` opening Hokma without a
console, an actual osacompile application, double click, icons shown) is NOT simulated here: it is a manual test on
Windows 10/11 and macOS.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from dbsupport import env  # noqa: F401  (fixture)

from hokma.core import config, shortcuts
from hokma.i18n import t
from hokma_cli.main import main

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "installer"
ACTIONS = ("start",)
LEGACY_NAMES = ["Apagar Hokma", "Reiniciar Hokma", "Shut down Hokma", "Restart Hokma"]


@pytest.fixture
def home(env, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    h = env / "home"
    (h / ".local" / "bin").mkdir(parents=True)
    for name in ("hokma", "hokma.cmd"):                                  # the name depends on the (faked) platform
        (h / ".local" / "bin" / name).write_text("#!/bin/sh\n", encoding="utf-8")
    return h


def _desktop_files(home: Path) -> dict[str, str]:
    apps = home / ".local" / "share" / "applications"
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(apps.glob("hokma*.desktop"))}


# ---- names and assets ---------------------------------------------------------------------------------------------
def test_labels_come_from_the_installer_files_in_both_languages():
    assert shortcuts.labels("es") == {"start": "Hokma"}
    assert shortcuts.labels("en") == {"start": "Hokma"}


def test_legacy_labels_are_the_four_old_names(env):
    assert sorted(shortcuts.legacy_labels()) == sorted(LEGACY_NAMES)                 # S-07


def test_all_labels_are_only_the_start_shortcut(env):
    assert shortcuts.all_labels() == {"start": ["Hokma"]}


def test_a_missing_name_is_an_error_not_a_blank_shortcut(monkeypatch, tmp_path):
    messages = tmp_path / "installer" / "messages"
    messages.mkdir(parents=True)
    for lang in ("en", "es"):
        (messages / f"{lang}.properties").write_text("shortcut.start=Hokma\n", encoding="utf-8")
    monkeypatch.setattr("hokma.kit.integrity.app_root", lambda: tmp_path)
    with pytest.raises(shortcuts.ShortcutError, match="legacy shortcut names for stop, restart"):
        shortcuts.legacy_labels()
    (messages / "en.properties").write_text("shortcut.legacy.stop=A\nshortcut.legacy.restart=B\n", encoding="utf-8")
    with pytest.raises(shortcuts.ShortcutError, match="no shortcut names for start"):
        shortcuts.labels("en")


def test_icons_exist_and_have_the_right_format():
    assets = INSTALLER / "assets"
    assert (assets / "hokma.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"
    assert (assets / "hokma.icns").read_bytes()[:4] == b"icns"
    assert (assets / "hokma.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_assets_travel_with_the_rest_of_the_installer():
    """Both installers copy the whole source tree into the app folder, and the release is a `git archive` of HEAD, so
    the assets are packaged as long as nothing excludes them."""
    for script in ("install.sh", "install.ps1"):
        assert "assets" not in (INSTALLER / script).read_text(encoding="utf-8").replace("shortcuts", ""), script
    attributes = ROOT / ".gitattributes"
    assert not attributes.exists() or "installer" not in attributes.read_text(encoding="utf-8")
    ignored = subprocess.run(["git", "check-ignore", "-q", str(INSTALLER / "assets" / "hokma.ico")], cwd=ROOT)
    assert ignored.returncode == 1, "installer/assets is ignored by git: it would not enter the release"


# ---- Linux, for real ----------------------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop entries")
def test_linux_creates_only_the_hokma_desktop_file_that_runs_the_launcher(home):    # S-01
    written = shortcuts.install()
    files = _desktop_files(home)
    assert sorted(files) == ["hokma.desktop"]
    assert len(written) == 1 and written.legacy_left == []
    launcher = config.launcher_path()
    for name, action, label in (("hokma.desktop", "start", "Hokma"),):
        text = files[name]
        assert text.startswith("[Desktop Entry]\nType=Application\n")
        assert f"Name={label}\n" in text
        assert f'Exec="{launcher}" {action}\n' in text
        assert "Terminal=false\n" in text
        assert (home / ".local" / "share" / "applications" / name).stat().st_mode & 0o111
    icon = config.hokma_data_dir() / "bin" / "hokma.png"
    assert icon.is_file() and f"Icon={icon}\n" in files["hokma.desktop"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop entries")
def test_linux_only_missing_keeps_what_exists_and_force_rewrites(home):
    shortcuts.install()
    apps = home / ".local" / "share" / "applications"
    (apps / "hokma.desktop").write_text("custom\n", encoding="utf-8")
    assert shortcuts.install(only_missing=True) == []
    assert (apps / "hokma.desktop").read_text(encoding="utf-8") == "custom\n"
    shortcuts.install()
    assert "Name=Hokma" in (apps / "hokma.desktop").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop entries")
@pytest.mark.parametrize("only_missing", [True, False])
def test_linux_removes_the_old_desktop_files_either_way(home, only_missing):         # S-02
    apps = home / ".local" / "share" / "applications"
    apps.mkdir(parents=True)
    for name in ("hokma-stop.desktop", "hokma-restart.desktop"):
        (apps / name).write_text("old\n", encoding="utf-8")
    (apps / "other.desktop").write_text("not ours\n", encoding="utf-8")
    shortcuts.install(only_missing=only_missing)
    assert sorted(p.name for p in apps.glob("*.desktop")) == ["hokma.desktop", "other.desktop"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop entries")
def test_linux_honours_xdg_data_home(home, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    shortcuts.install()
    assert (tmp_path / "xdg" / "applications" / "hokma.desktop").is_file()


def test_exec_quoting_survives_spaces_and_special_characters():
    entry = shortcuts.desktop_entry("Hokma", Path('/home/Jos\u00e9 \u00d1and\u00fa/a"b$c`d%e/hokma'), "start", None)
    assert 'Exec="/home/Jos\u00e9 \u00d1and\u00fa/a\\"b\\$c\\`d%%e/hokma" start\n' in entry
    assert "Icon=" not in entry


def test_install_without_a_launcher_fails_with_a_readable_error(env):
    with pytest.raises(shortcuts.ShortcutError, match="does not exist"):
        shortcuts.install()


# ---- macOS: content and commands ----------------------------------------------------------------------------------
def test_macos_script_runs_the_launcher_without_terminal():
    script = shortcuts.applescript(Path('/Users/a b/.local/bin/hokma'), "restart")
    assert script == 'do shell script quoted form of "/Users/a b/.local/bin/hokma" & " restart >/dev/null 2>&1"'
    assert "Terminal" not in script
    assert 'quoted form of "/x\\"y"' in shortcuts.applescript(Path('/x"y'), "start")


def test_macos_builds_one_application_with_osacompile(home, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        app = Path(cmd[cmd.index("-o") + 1])
        (app / "Contents" / "Resources").mkdir(parents=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shortcuts.subprocess, "run", fake_run)
    written = shortcuts.install()
    folder = home / "Applications" / "Hokma"
    assert [p.name for p in written] == ["Hokma.app"]
    assert all(c[0] == "osacompile" for c in calls) and len(calls) == 1
    assert calls[0][-1] == shortcuts.applescript(config.launcher_path(), "start")
    assert (folder / "Hokma.app" / "Contents" / "Resources" / "applet.icns").read_bytes()[:4] == b"icns"


def test_macos_removes_the_four_old_applications_and_touches_nothing_else(home, monkeypatch):   # S-03
    folder = home / "Applications" / "Hokma"
    for name in LEGACY_NAMES:
        (folder / f"{name}.app").mkdir(parents=True)
    (folder / "Mine.app").mkdir()
    outside = home / "Applications" / "Apagar Hokma.app"
    outside.mkdir()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        (Path(cmd[cmd.index("-o") + 1]) / "Contents" / "Resources").mkdir(parents=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shortcuts.subprocess, "run", fake_run)
    shortcuts.install(only_missing=True)
    assert len(calls) == 1
    assert sorted(p.name for p in folder.iterdir()) == ["Hokma.app", "Mine.app"]
    assert outside.is_dir()


def test_macos_osacompile_failure_is_a_shortcut_error(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shortcuts.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"))
    with pytest.raises(shortcuts.ShortcutError, match="boom"):
        shortcuts.install()


# ---- Windows: content and commands --------------------------------------------------------------------------------
def test_windows_vbs_is_hidden_validates_the_action_and_calls_only_the_launcher():
    vbs = shortcuts.VBS.format(launcher=r"C:\Users\Jos\u00e9\.local\bin\hokma.cmd")
    assert ", 0, False" in vbs                                         # window style 0 = hidden, do not wait
    assert 'If action <> "start" Then WScript.Quit 2' in vbs
    assert '"stop"' not in vbs and '"restart"' not in vbs                            # S-06
    assert "hokma.cmd" in vbs and "Chr(34)" in vbs


def test_windows_spec_points_at_wscript_and_the_stable_vbs(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    vbs = Path(r"C:\Users\a\AppData\Local\Hokma\bin\hokma-hidden.vbs")
    spec = shortcuts.windows_spec(Path(r"C:\Users\a\.local\bin\hokma.cmd"), shortcuts.all_labels(), True, vbs,
                                  Path(r"C:\Users\a\AppData\Local\Hokma\bin\hokma.ico"))
    assert spec["target"].endswith("wscript.exe") and spec["only_missing"] is True
    assert [i["action"] for i in spec["items"]] == list(ACTIONS)                     # S-04
    assert sorted(spec["remove"]) == sorted(LEGACY_NAMES)
    for item in spec["items"]:
        assert item["arguments"] == f'//B //Nologo "{vbs}" {item["action"]}'
        assert "runtime" not in item["arguments"]                      # never a slot an update overwrites
    assert spec["icon"].endswith("hokma.ico,0")


def test_windows_install_runs_powershell_with_the_spec_in_the_environment(home, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shortcuts, "spawn_kwargs", lambda: {})
    monkeypatch.setattr(shortcuts.subprocess, "run", fake_run)
    written = shortcuts.install(only_missing=True)
    vbs = config.hokma_data_dir() / "bin" / "hokma-hidden.vbs"
    assert written == [vbs] and vbs.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")        # UTF-16 with BOM
    assert seen["cmd"][0] == "powershell.exe" and "-NoProfile" in seen["cmd"] and "-EncodedCommand" in seen["cmd"]
    spec = json.loads(seen["env"]["HOKMA_LNK_SPEC"])
    assert spec["only_missing"] is True and len(spec["items"]) == 1
    assert "HOKMA_LNK_SPEC" not in " ".join(seen["cmd"])                # data never pasted into the code


def test_windows_powershell_script_has_no_login_start_and_is_ascii():
    script = shortcuts.POWERSHELL
    script.encode("ascii")
    for forbidden in ("CurrentVersion\\Run", "HKCU", "Startup", "RunOnce"):
        assert forbidden not in script
    assert 'GetFolderPath("Programs")' in script and 'GetFolderPath("Desktop")' in script
    assert "WindowStyle = 7" in script                                  # minimised: never a visible console
    # S-05: old shortcuts are deleted with -LiteralPath, in both folders, before the creation loop
    assert "Remove-Item -LiteralPath" in script and "$spec.remove" in script
    assert script.index("Remove-Item -LiteralPath") < script.index("CreateShortcut")


def test_windows_reports_the_old_shortcuts_it_could_not_delete(home, monkeypatch, capsys):    # S-09
    out = "LEFT:C:\\Users\\a\\Desktop\\Apagar Hokma.lnk\r\n"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shortcuts, "spawn_kwargs", lambda: {})
    monkeypatch.setattr(shortcuts.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, ""))
    result = shortcuts.install(only_missing=True)
    assert [str(p) for p in result.legacy_left] == ["C:\\Users\\a\\Desktop\\Apagar Hokma.lnk"]
    assert main(["shortcuts"]) == 0
    assert "Apagar Hokma.lnk" in capsys.readouterr().err


def test_windows_powershell_failure_is_a_shortcut_error(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shortcuts, "spawn_kwargs", lambda: {})
    monkeypatch.setattr(shortcuts.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "denied"))
    with pytest.raises(shortcuts.ShortcutError, match="denied"):
        shortcuts.install()


# ---- Static guarantees over every file that could start Hokma with the system ---------------------------------------
LOGIN_START = ("CurrentVersion\\Run", "HKCU:", "HKLM:", "LaunchAgent", "LaunchDaemon", "systemd", "RegisterApplicationRestart",
               "autostart", "crontab", "@reboot", "schtasks", "Register-ScheduledTask")


@pytest.mark.parametrize("path", sorted(INSTALLER.glob("*.ps1")) + sorted(INSTALLER.glob("*.sh"))
                         + [ROOT / "backend" / "hokma" / "core" / "shortcuts.py"], ids=lambda p: p.name)
def test_nothing_starts_hokma_at_login(path):
    text = path.read_text(encoding="utf-8")
    for word in LOGIN_START:
        assert word.lower() not in text.lower(), f"{path.name} mentions {word}"


@pytest.mark.parametrize("path", sorted(INSTALLER.glob("*.ps1")), ids=lambda p: p.name)
def test_powershell_files_are_plain_ascii(path):
    path.read_bytes().decode("ascii")


def test_both_installers_call_the_shortcuts_command_and_both_uninstallers_remove_them():
    assert "shortcuts --force" in (INSTALLER / "install.ps1").read_text(encoding="utf-8")
    assert "shortcuts --force" in (INSTALLER / "install.sh").read_text(encoding="utf-8")
    ps = (INSTALLER / "uninstall.ps1").read_text(encoding="utf-8")
    sh = (INSTALLER / "uninstall.sh").read_text(encoding="utf-8")
    for key in ("shortcut.start", "shortcut.legacy.stop", "shortcut.legacy.restart"):          # S-10
        assert key in ps and key in sh, key
    assert ".lnk" in ps and "hokma.desktop" in sh and "hokma-stop.desktop" in sh and "hokma-restart.desktop" in sh
    assert ".app" in sh
    for text in (ps, sh):
        assert "--purge" in text or "Purge" in text                      # the working folder stays behind the old gate


# ---- The commands ----------------------------------------------------------------------------------------------------
def test_shortcuts_command_creates_and_reports(home, capsys):
    assert main(["shortcuts"]) == 0
    assert t("cli.shortcuts.created") in capsys.readouterr().out
    assert main(["shortcuts", "--force"]) == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop entries")
def test_post_update_deletes_old_shortcuts_and_keeps_the_hokma_one(home, monkeypatch):      # S-08
    shortcuts.install()
    apps = home / ".local" / "share" / "applications"
    (apps / "hokma.desktop").write_text("mine\n", encoding="utf-8")
    for name in ("hokma-stop.desktop", "hokma-restart.desktop"):
        (apps / name).write_text("old\n", encoding="utf-8")
    monkeypatch.setattr("hokma.kit.integrity.is_installed_app", lambda: True)
    import hokma_cli.main as cli

    cli._ensure_shortcuts()
    assert sorted(p.name for p in apps.glob("hokma*.desktop")) == ["hokma.desktop"]
    assert (apps / "hokma.desktop").read_text(encoding="utf-8") == "mine\n"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop entries")
def test_an_old_shortcut_that_cannot_be_deleted_is_a_warning_not_an_error(home, monkeypatch, capsys):   # S-09
    apps = home / ".local" / "share" / "applications"
    apps.mkdir(parents=True)
    stuck = apps / "hokma-stop.desktop"
    stuck.write_text("old\n", encoding="utf-8")
    real_unlink = Path.unlink

    def deny(self, *a, **kw):
        if self == stuck:
            raise PermissionError("denied")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", deny)
    result = shortcuts.install()
    assert result.legacy_left == [stuck]
    assert main(["shortcuts"]) == 0
    assert str(stuck) in capsys.readouterr().err


def test_no_text_sends_the_owner_to_a_shortcut_that_no_longer_exists():                    # S-11
    banned = ("acceso Reiniciar Hokma", "acceso Apagar Hokma", "Restart Hokma shortcut", "Shut down Hokma shortcut")
    roots = [ROOT / "installer" / "messages", ROOT / "backend" / "hokma" / "catalog", ROOT / "dashboard" / "src" / "i18n"]
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in (".properties", ".json", ".ts"):
                text = path.read_text(encoding="utf-8")
                for phrase in banned:
                    assert phrase not in text, f"{path} still says {phrase}"


def test_shortcuts_command_failure_returns_one(env, capsys):
    assert main(["shortcuts"]) == 1                                     # no launcher in the temporary HOME
    assert t("cli.tag.warn") in capsys.readouterr().err


def test_post_update_asks_for_the_missing_shortcuts(env, monkeypatch):
    import hokma_cli.main as cli

    calls = []
    monkeypatch.setattr(cli, "_ensure_shortcuts", lambda: calls.append(True))
    assert main(["post-update"]) == 0
    assert calls == [True]


def test_ensure_shortcuts_creates_only_the_missing_ones(env, monkeypatch):
    import hokma_cli.main as cli

    calls = []
    monkeypatch.setattr("hokma.kit.integrity.is_installed_app", lambda: True)
    monkeypatch.setattr(shortcuts, "install", lambda only_missing=False: calls.append(only_missing) or [])
    cli._ensure_shortcuts()
    assert calls == [True]


def test_ensure_shortcuts_never_fails_the_update(env, monkeypatch, capsys):
    import hokma_cli.main as cli

    def boom(only_missing=False):
        raise shortcuts.ShortcutError("no permission")

    monkeypatch.setattr("hokma.kit.integrity.is_installed_app", lambda: True)
    monkeypatch.setattr(shortcuts, "install", boom)
    cli._ensure_shortcuts()                                              # returns, does not raise
    assert "no permission" in capsys.readouterr().err


def test_ensure_shortcuts_in_a_dev_checkout_creates_nothing(env, monkeypatch):
    import hokma_cli.main as cli

    monkeypatch.setattr("hokma.kit.integrity.is_installed_app", lambda: False)
    monkeypatch.setattr(shortcuts, "install", lambda only_missing=False: pytest.fail("shortcuts in a dev checkout"))
    cli._ensure_shortcuts()
