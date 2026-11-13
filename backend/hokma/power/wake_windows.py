"""Windows wake-up: one Task Scheduler task (`WakeToRun`, interactive logon of the user, no administrator) that fires
a few minutes before the next routine. Registered with `schtasks /Create /XML`, never with a visible console."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from hokma.core.proc import spawn_kwargs

TASK_NAME = "HokmaWake"
_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><TimeTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT1M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>-c pass</Arguments></Exec></Actions>
</Task>
"""


def _command() -> str:
    """A windowless interpreter that exits at once: the task only exists to wake the machine."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else sys.executable)


def build_xml(at: datetime, command: str | None = None) -> str:
    """Task definition firing once at `at` (an aware datetime, converted to the local clock)."""
    local = at.astimezone().replace(tzinfo=None, microsecond=0)
    return _XML.format(start=local.isoformat(), command=command or _command())


def create_argv(xml_path: str) -> list[str]:
    return ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"]


def delete_argv() -> list[str]:
    return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]


class WindowsWake:
    def __init__(self, run: Callable[..., Any] = subprocess.run, winreg: Any = None) -> None:
        self._run = run
        self._winreg = winreg

    def _exec(self, argv: list[str]) -> Any:
        return self._run(argv, capture_output=True, text=True, timeout=30, **spawn_kwargs())

    def _modern_standby(self) -> bool:
        try:
            winreg = self._winreg or __import__("winreg")
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Power") as key:
                return bool(winreg.QueryValueEx(key, "CsEnabled")[0])
        except OSError:
            return False

    def _timers_off(self) -> bool:
        """`powercfg` prints the AC and DC indexes last, as 0x........ (0 = Disable); the words are localized."""
        out = self._exec(["powercfg", "/q", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE"]).stdout or ""
        values = re.findall(r"0x[0-9a-fA-F]{8}", out)
        return len(values) >= 2 and int(values[-1], 16) == 0 and int(values[-2], 16) == 0

    def check(self) -> str | None:
        """None when a wake-up can work; otherwise why not."""
        if self._modern_standby():
            return "modern_standby"
        try:
            return "timers_off" if self._timers_off() else None
        except (OSError, subprocess.SubprocessError):
            return None

    def schedule(self, at: datetime) -> bool:
        fd, path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-16") as f:
                f.write(build_xml(at))
            return self._exec(create_argv(path)).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
        finally:
            os.unlink(path)

    def cancel(self) -> None:
        try:
            self._exec(delete_argv())
        except (OSError, subprocess.SubprocessError):
            pass
