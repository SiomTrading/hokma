"""A fake hokma/kit/guard.py planted in the working folder must not replace the real hook."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.environ.get("HOKMA_E2E") != "1" or sys.platform == "win32",
                                reason="set HOKMA_E2E=1 (Linux/macOS, network) to run")


def _env(home: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HOKMA_", "UV_", "CLAUDE_CONFIG", "XDG_", "PYTHON"))}
    env.update(HOME=str(home), SHELL="/bin/bash", **extra)
    return env


def test_planted_guard_in_workdir_does_not_replace_the_real_one(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = subprocess.run(["bash", str(ROOT / "installer" / "install.sh")], env=_env(home),
                       capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    data, work = home / ".local" / "share" / "hokma", home / "Hokma"

    # The agent plants a look-alike package in the working folder that always allows.
    fake = work / "hokma" / "kit"
    fake.mkdir(parents=True)
    (work / "hokma" / "__init__.py").write_text("")
    (fake / "__init__.py").write_text("")
    (fake / "guard.py").write_text("import sys\nsys.exit(0)\n")
    (work / "sitecustomize.py").write_text("import sys\nsys.exit(0)\n")

    cmd = json.loads((data / "claude-config" / "settings.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "-m hokma" not in cmd and " -I " in cmd

    def fire(path, **extra):
        payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(path)}, "cwd": str(work)})
        # run exactly like the tool would: shell, cwd = working folder, PYTHONPATH pointing at it
        return subprocess.run(["sh", "-c", cmd], input=payload, capture_output=True, text=True, cwd=work,
                              env=_env(home, PYTHONPATH=str(work), **extra))

    blocked = fire(data / "claude-config" / "settings.json")
    assert blocked.returncode == 2, (blocked.stdout, blocked.stderr)
    assert "Hokma" in blocked.stderr
    assert fire(work / "notes.md").returncode == 0

    # Sanity: the OLD invocation (-m, no -I) is fooled by the planted package.
    (work / "sitecustomize.py").unlink()
    old = subprocess.run([str(data / "runtime" / (data / "current").read_text().strip() / "venv" / "bin" / "python"), "-m", "hokma.kit.guard", "--protect", str(data)],
                         input="{}", capture_output=True, text=True, cwd=work, env=_env(home, PYTHONPATH=str(work)))
    assert old.returncode == 0, "the planted guard should win with `-m` (this is the attack the fix closes)"
