"""E2E: the installer, the CLI and the uninstaller speak the language chosen at install time."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import installer_message

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.environ.get("HOKMA_E2E") != "1" or sys.platform == "win32",
                                reason="set HOKMA_E2E=1 (Linux/macOS, network) to run")


def _env(home: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HOKMA_", "UV_", "CLAUDE_CONFIG", "XDG_")) and k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(HOME=str(home), SHELL="/bin/bash", **extra)
    return env


def _run(args, env, **kw):
    return subprocess.run([str(a) for a in args], env=env, capture_output=True, text=True, timeout=900, **kw)


def test_english_install_cli_and_uninstall(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = _env(home)
    r = _run(["bash", ROOT / "installer" / "install.sh", "--lang", "en"], env)
    assert r.returncode == 0, r.stdout + r.stderr
    data, launcher = home / ".local" / "share" / "hokma", home / ".local" / "bin" / "hokma"
    assert (data / "language").read_text().strip() == "en"
    assert installer_message("inst.done", lang="en") in r.stdout
    assert installer_message("inst.done", lang="es") not in r.stdout

    doctor = _run([launcher, "doctor"], env)                      # no HOKMA_LANG: the saved choice applies
    assert "[ok]" in doctor.stdout and "[warn]" in doctor.stdout
    pending = _run([launcher, "ext", "requests"], env)
    assert json.loads(Path(ROOT / "backend/hokma/catalog/en.json").read_text())["cli.ext.none_pending"] in pending.stdout

    spanish = _run([launcher, "ext", "requests"], {**env, "HOKMA_LANG": "es"})      # the variable wins
    assert json.loads(Path(ROOT / "backend/hokma/catalog/es.json").read_text())["cli.ext.none_pending"] in spanish.stdout

    u = _run(["bash", ROOT / "installer" / "uninstall.sh"], env, stdin=subprocess.DEVNULL)
    assert u.returncode == 0 and installer_message("un.done", lang="en") in u.stdout


def test_unsupported_language_is_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = _run(["bash", ROOT / "installer" / "install.sh", "--lang", "fr"], _env(home))
    assert r.returncode != 0 and installer_message("inst.lang_invalid", "fr", lang="es") in r.stderr
