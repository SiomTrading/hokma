"""Shared pieces for the tests of the always-on protections (hokma.kit.hardblock)."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hokma.kit import hardblock

HARDBLOCK_FILE = Path(hardblock.__file__).resolve()


def registered(hooks: Path) -> dict:
    """The settings the way Hokma writes them: both hooks, each `python -I <script> ... || exit 2`."""
    return {"disableAllHooks": False, "hooks": {"PreToolUse": [
        {"matcher": "Edit|MultiEdit|Write|NotebookEdit", "hooks": [
            {"type": "command", "command": f"cd /d && python -I {hooks}/guard.py --protect /p || exit 2"}]},
        {"matcher": ".*", "hooks": [
            {"type": "command", "command": f"cd /d && python -I {hooks}/hardblock.py --data /d || exit 2"}]}]}}


class Box:
    """A throwaway data folder and working folder with a few real files."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.data, self.work = tmp / "data", tmp / "work"
        (self.data / "claude-config").mkdir(parents=True)
        (self.data / "guard").mkdir()
        (self.data / "bin").mkdir()
        (self.data / "tools").mkdir()
        self.work.mkdir()
        (self.work / "informes").mkdir()
        (self.work / "informes" / "viejo.md").write_text("old report\n")
        (self.work / "notes.md").write_text("some notes\n")
        (self.work / "d").mkdir()
        (self.work / "d" / "inner.txt").write_text("inner\n")
        (self.work / "f").write_text("content of f\n")
        (self.data / "database.json").write_text('{"url": "postgresql://u:p@h/db"}\n')
        (self.data / "claude-config" / ".credentials.json").write_text("{}\n")
        self.hooks = tmp / "hooks"                       # the code of the hooks, as the installed package is
        self.hooks.mkdir()
        (self.hooks / "hardblock.py").write_text("# hook\n")
        self.settings = self.data / "claude-config" / "settings.json"
        self.settings.write_text(json.dumps(registered(self.hooks)))
        self.protect = [str(self.hooks), str(self.settings), str(self.data / "claude-config" / "settings.engine.json"),
                        str(self.data / "guard"), str(self.data / "bin"), str(self.data / "tools")]

    def ctx(self, cwd: Path | None = None) -> hardblock.Ctx:
        return hardblock.Ctx(str(self.data), [str(self.work)], self.protect, home=str(self.tmp / "home"),
                             cwd=str(cwd or self.work))

    def hit(self, tool: str, cwd: Path | None = None, **tool_input) -> str | None:
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd or self.work)}
        block = hardblock.evaluate(payload, self.ctx(cwd))
        return block.rule if block else None

    def bash(self, command: str, cwd: Path | None = None) -> str | None:
        return self.hit("Bash", cwd, command=command)

    def ps(self, command: str, cwd: Path | None = None) -> str | None:
        return self.hit("PowerShell", cwd, command=command)

    def tree_hash(self) -> dict[str, str]:
        out = {}
        for p in sorted(self.work.rglob("*")):
            if p.is_file():
                out[p.relative_to(self.work).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    def run_hook(self, payload: dict, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        """The hook as Claude Code runs it: a separate isolated interpreter, payload on stdin."""
        args = [sys.executable, "-I", str(HARDBLOCK_FILE), "--data", str(self.data), "--workdir", str(self.work)]
        for p in self.protect:
            args += ["--protect", p]
        env = {**os.environ, "HOKMA_LANG": "en", **(extra_env or {})}
        return subprocess.run(args, input=json.dumps(payload), capture_output=True, text=True, env=env, cwd=self.data)


@pytest.fixture
def box(tmp_path, monkeypatch) -> Box:
    monkeypatch.setenv("HOKMA_LANG", "en")
    return Box(tmp_path)
