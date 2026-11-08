"""The one shortcut that opens Hokma, created from one place.

"Hokma" runs `hokma start`. Shutting down happens inside the panel (the "Shut down Hokma" button), so the old "Shut down
Hokma" and "Restart Hokma" shortcuts of earlier versions are deleted on every install, reinstall and `hokma update`.
Hokma has no start at login: it starts when the owner opens the "Hokma" shortcut. No shortcut opens a console window,
and it points at the stable launcher (`~/.local/bin/hokma[.cmd]`), never at a runtime slot that the next update
overwrites.

  Windows  Start menu and Desktop `.lnk` files. A `.lnk` cannot run a `.cmd` without a console, so it runs
           `wscript.exe` on `<data>/bin/hokma-hidden.vbs`, which starts the launcher with a hidden window.
  macOS    Minimal AppleScript applications in `~/Applications/Hokma/`, built with `osacompile`, that run the launcher
           with `do shell script` (no Terminal).
  Linux    `.desktop` files in `<XDG data home>/applications/` with `Terminal=false`.

The installers call `hokma shortcuts` after they switch to the new runtime, and `hokma post-update` calls it too, so
an owner coming from an older version gets the shortcut without touching the database or the working folder. The
uninstallers remove them with plain shell code because they must work without a runtime.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from hokma.core import config
from hokma.core.proc import spawn_kwargs

ACTIONS = ("start",)
LEGACY_ACTIONS = ("stop", "restart")             # shortcuts of earlier versions: only ever deleted
LANGUAGES = ("es", "en")
POWERSHELL_TIMEOUT = 60
OSACOMPILE_TIMEOUT = 60

# Linux file names do not depend on the language; only the visible Name does.
DESKTOP_FILES = {"start": "hokma.desktop"}
LEGACY_DESKTOP_FILES = ("hokma-stop.desktop", "hokma-restart.desktop")


class ShortcutError(Exception):
    """A shortcut could not be created. The message says why."""


def _asset(name: str) -> Path:
    from hokma.kit.integrity import app_root

    return app_root() / "installer" / "assets" / name


def _read_properties(lang: str) -> tuple[Path, dict[str, str]]:
    from hokma.kit.integrity import app_root

    path = app_root() / "installer" / "messages" / f"{lang}.properties"
    table: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        table[key] = value.strip()
    return path, table


def labels(lang: str) -> dict[str, str]:
    """Visible name of the shortcut in `lang`, read from the installer message files (the one place the
    uninstallers read them from too)."""
    path, table = _read_properties(lang)
    missing = [a for a in ACTIONS if not table.get(f"shortcut.{a}")]
    if missing:
        raise ShortcutError(f"{path} has no shortcut names for {', '.join(missing)}")
    return {a: table[f"shortcut.{a}"] for a in ACTIONS}


def legacy_labels() -> list[str]:
    """Names of the old Shut down and Restart shortcuts in both languages, without repeats."""
    found: list[str] = []
    for lang in LANGUAGES:
        path, table = _read_properties(lang)
        missing = [a for a in LEGACY_ACTIONS if not table.get(f"shortcut.legacy.{a}")]
        if missing:
            raise ShortcutError(f"{path} has no legacy shortcut names for {', '.join(missing)}")
        found.extend(table[f"shortcut.legacy.{a}"] for a in LEGACY_ACTIONS)
    return list(dict.fromkeys(found))


def all_labels() -> dict[str, list[str]]:
    """Per action, the names in the active language first, then the other languages (to recognise existing ones)."""
    from hokma.i18n import language

    order = [language()] + [lang for lang in LANGUAGES if lang != language()]
    tables = [labels(lang) for lang in order]
    return {a: list(dict.fromkeys(table[a] for table in tables)) for a in ACTIONS}


# ---- Linux ------------------------------------------------------------------------------------------------------
def _exec_quote(value: str) -> str:
    """One argument of a `.desktop` Exec line (double quotes; backslash, quote, backtick and dollar escaped; % doubled)."""
    out = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    return f'"{out}"'


def linux_applications_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "applications"


def desktop_entry(name: str, launcher: Path, action: str, icon: Path | None) -> str:
    lines = ["[Desktop Entry]", "Type=Application", f"Name={name}", f"Exec={_exec_quote(str(launcher))} {action}",
             "Terminal=false", "Categories=Utility;"]
    if icon is not None:
        lines.append(f"Icon={icon}")
    return "\n".join(lines) + "\n"


def _install_linux(launcher: Path, names: dict[str, list[str]], only_missing: bool) -> list[Path]:
    apps = linux_applications_dir()
    apps.mkdir(parents=True, exist_ok=True)
    icon = _stable_icon("hokma.png")
    written = []
    for action in ACTIONS:
        path = apps / DESKTOP_FILES[action]
        if only_missing and path.exists():
            continue
        path.write_text(desktop_entry(names[action][0], launcher, action, icon), encoding="utf-8")
        path.chmod(0o755)
        written.append(path)
    return written


def _remove_legacy_linux() -> list[Path]:
    """Delete the old desktop files; returns the ones that could not be deleted."""
    left = []
    for name in LEGACY_DESKTOP_FILES:
        path = linux_applications_dir() / name
        if not path.exists() and not path.is_symlink():
            continue
        try:
            path.unlink()
        except OSError:
            left.append(path)
    return left


# ---- macOS ------------------------------------------------------------------------------------------------------
def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def applescript(launcher: Path, action: str) -> str:
    """The whole script of one application: run the launcher, show nothing, open no Terminal."""
    return f"do shell script quoted form of {_applescript_string(str(launcher))} & {_applescript_string(f' {action} >/dev/null 2>&1')}"


def macos_applications_dir() -> Path:
    return Path.home() / "Applications" / "Hokma"


def _remove_legacy_macos() -> list[Path]:
    """Only a real directory named `<old name>.app` directly inside the Hokma applications folder is removed; returns
    the ones that could not be deleted."""
    folder = macos_applications_dir()
    left = []
    for name in legacy_labels():
        app = folder / f"{name}.app"
        if app.is_symlink() or not app.is_dir():
            continue
        try:
            shutil.rmtree(app)
        except OSError:
            left.append(app)
    return left


def _install_macos(launcher: Path, names: dict[str, list[str]], only_missing: bool) -> list[Path]:
    folder = macos_applications_dir()
    folder.mkdir(parents=True, exist_ok=True)
    icon = _asset("hokma.icns")
    written = []
    for action in ACTIONS:
        if only_missing and any((folder / f"{n}.app").exists() for n in names[action]):
            continue
        app = folder / f"{names[action][0]}.app"
        if app.exists():
            shutil.rmtree(app)
        result = subprocess.run(["osacompile", "-o", str(app), "-e", applescript(launcher, action)],
                                capture_output=True, text=True, timeout=OSACOMPILE_TIMEOUT, **spawn_kwargs())
        if result.returncode != 0:
            raise ShortcutError((result.stderr or result.stdout).strip() or "osacompile failed")
        if icon.is_file():
            shutil.copyfile(icon, app / "Contents" / "Resources" / "applet.icns")
        written.append(app)
    return written


# ---- Windows ----------------------------------------------------------------------------------------------------
VBS = '''\
' Generated by Hokma. Starts the launcher with a hidden window so no console appears.
Dim launcher, action
launcher = "{launcher}"
If WScript.Arguments.Count < 1 Then WScript.Quit 2
action = WScript.Arguments(0)
If action <> "start" Then WScript.Quit 2
CreateObject("WScript.Shell").Run "cmd.exe /d /c " & Chr(34) & Chr(34) & launcher & Chr(34) & " " & action & Chr(34), 0, False
'''

# Reads its input from the HOKMA_LNK_SPEC environment variable (JSON), so no path or name is ever pasted into code.
POWERSHELL = r'''
$ErrorActionPreference = "Stop"
$spec = $env:HOKMA_LNK_SPEC | ConvertFrom-Json
$folders = @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop")) | Where-Object { $_ }
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in $folders) {
    foreach ($n in $spec.remove) {
        $old = Join-Path $folder ($n + ".lnk")
        Remove-Item -LiteralPath $old -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $old) { Write-Output ("LEFT:" + $old) }
    }
}
foreach ($folder in $folders) {
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    foreach ($item in $spec.items) {
        if ($spec.only_missing) {
            $exists = $false
            foreach ($n in $item.names) { if (Test-Path -LiteralPath (Join-Path $folder ($n + ".lnk"))) { $exists = $true } }
            if ($exists) { continue }
        }
        $lnk = $shell.CreateShortcut((Join-Path $folder ($item.names[0] + ".lnk")))
        $lnk.TargetPath = $spec.target
        $lnk.Arguments = $item.arguments
        $lnk.WorkingDirectory = $spec.workdir
        $lnk.IconLocation = $spec.icon
        $lnk.WindowStyle = 7
        $lnk.Description = $item.names[0]
        $lnk.Save()
    }
}
'''


def _stable_icon(name: str) -> Path | None:
    """Copy of an icon in `<data>/bin`, which no update touches (the app folder is replaced by each update)."""
    source = _asset(name)
    if not source.is_file():
        return None
    dest = config.hokma_data_dir() / "bin" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    return dest


def windows_spec(launcher: Path, names: dict[str, list[str]], only_missing: bool, vbs: Path, icon: Path | None) -> dict:
    system = os.environ.get("SystemRoot") or r"C:\Windows"
    return {
        "target": str(Path(system) / "System32" / "wscript.exe"),
        "workdir": str(Path.home()),
        "icon": f"{icon},0" if icon is not None else "",
        "only_missing": only_missing,
        "remove": legacy_labels(),
        "items": [{"action": a, "names": names[a], "arguments": f'//B //Nologo "{vbs}" {a}'} for a in ACTIONS],
    }


def _install_windows(launcher: Path, names: dict[str, list[str]], only_missing: bool) -> tuple[list[Path], list[Path]]:
    vbs = config.hokma_data_dir() / "bin" / "hokma-hidden.vbs"
    vbs.parent.mkdir(parents=True, exist_ok=True)
    vbs.write_text(VBS.format(launcher=str(launcher)), encoding="utf-16")     # UTF-16 with BOM: accents survive
    spec = windows_spec(launcher, names, only_missing, vbs, _stable_icon("hokma.ico"))
    encoded = base64.b64encode(POWERSHELL.encode("utf-16-le")).decode("ascii")
    env = dict(os.environ, HOKMA_LNK_SPEC=json.dumps(spec))
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-EncodedCommand", encoded], capture_output=True, text=True, env=env,
                            timeout=POWERSHELL_TIMEOUT, **spawn_kwargs())
    if result.returncode != 0:
        raise ShortcutError((result.stderr or result.stdout).strip() or "powershell failed")
    left = [Path(line[5:].strip()) for line in result.stdout.splitlines() if line.startswith("LEFT:")]
    return [vbs], left


# ---- Entry point ------------------------------------------------------------------------------------------------
class InstallResult(list):
    """What `install` wrote (a list of paths), plus `legacy_left`: old shortcuts that could not be deleted."""

    def __init__(self, written=(), legacy_left=()) -> None:
        super().__init__(written)
        self.legacy_left: list[Path] = list(legacy_left)


def remove_legacy() -> list[Path]:
    """Delete the old Shut down and Restart shortcuts (both languages) on Linux and macOS; returns the ones that could
    not be deleted. On Windows the PowerShell script of `install` does it. Never raises for a file that resists."""
    if sys.platform == "win32":
        return []
    if sys.platform == "darwin":
        return _remove_legacy_macos()
    return _remove_legacy_linux()


def install(only_missing: bool = False) -> InstallResult:
    """Create the shortcut for this system, deleting the old Shut down and Restart ones first (always, with or without
    `only_missing`). Returns what was written; `legacy_left` lists old shortcuts that could not be deleted (a warning,
    never an error). With `only_missing`, a shortcut that exists (in any language) is left alone. Raises ShortcutError."""
    launcher = config.launcher_path()
    if not launcher.is_file():
        raise ShortcutError(f"the launcher {launcher} does not exist")
    names = all_labels()
    legacy_labels()                                           # a bad message file fails before anything is touched
    try:
        if sys.platform == "win32":
            written, left = _install_windows(launcher, names, only_missing)
            return InstallResult(written, left)
        left = remove_legacy()
        if sys.platform == "darwin":
            return InstallResult(_install_macos(launcher, names, only_missing), left)
        return InstallResult(_install_linux(launcher, names, only_missing), left)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ShortcutError(str(exc)) from exc
