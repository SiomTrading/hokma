"""The system's own folder dialog, opened from Hokma (SPEC-010 section 4.3). The student never types a command.

Windows uses the .NET folder dialog through PowerShell, macOS uses `choose folder`, Linux uses zenity or kdialog. When
the machine has none of them the answer says so and the screen offers a field to paste the path.
"""
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

DIALOG_SECONDS = 600

_WINDOWS_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
    "$d.ShowNewFolderButton = $true;"
    "$top = New-Object System.Windows.Forms.Form; $top.TopMost = $true;"
    "if ($d.ShowDialog($top) -eq [System.Windows.Forms.DialogResult]::OK) "
    "{ [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; [Console]::Out.Write($d.SelectedPath) }")


@dataclass
class Pick:
    available: bool
    path: str | None = None          # None with available=True means the student closed the dialog

    def public(self) -> dict:
        return {"available": self.available, "path": self.path, "cancelled": self.available and self.path is None}


def _command() -> list[str] | None:
    if sys.platform == "win32":
        shell = shutil.which("powershell") or shutil.which("pwsh")
        return [shell, "-NoProfile", "-STA", "-Command", _WINDOWS_SCRIPT] if shell else None
    if sys.platform == "darwin":
        return ["osascript", "-e", "POSIX path of (choose folder)"] if shutil.which("osascript") else None
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    if shutil.which("zenity"):
        return ["zenity", "--file-selection", "--directory"]
    if shutil.which("kdialog"):
        return ["kdialog", "--getexistingdirectory", os.path.expanduser("~")]
    return None


def pick_folder() -> Pick:
    """Open the dialog and wait for the answer. Blocking: call it from a worker thread."""
    command = _command()
    if command is None:
        return Pick(available=False)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        done = subprocess.run(command, capture_output=True, timeout=DIALOG_SECONDS, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return Pick(available=False)
    text = done.stdout.decode("utf-8", errors="replace").strip()
    if done.returncode != 0 or not text:
        return Pick(available=True)
    if len(text) > 1 and text.endswith(("/", "\\")) and text not in ("/",) and not text.endswith(":\\"):
        text = text.rstrip("/\\")
    return Pick(available=True, path=text)
