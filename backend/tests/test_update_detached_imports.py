"""The detached updater runs on the base Python, where only the standard library exists (bug 1111).

One process imports everything the updater can reach, with every module outside the standard library and outside
`hokma` refused, then builds the default source. A third-party import on that path fails here, not on a student's PC."""
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

PROBE = r'''
import importlib, importlib.abc, sys

sys.path.insert(0, BACKEND_DIR)

class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        if top != "hokma" and top not in sys.stdlib_module_names:
            raise ImportError("outside the standard library: " + name)
        return None

sys.modules["keyring"] = None
sys.meta_path.insert(0, Refuse())
for name in ("hokma.update.detached", "hokma.update.source", "hokma.update.github", "hokma.update.runner",
             "hokma.update.state", "hokma.update.archive", "hokma.update.safe_exec", "hokma.core.enginelock",
             "hokma.core.supervisor", "hokma.core.control", "hokma.core.session"):
    importlib.import_module(name)
from hokma.update.detached import resolve_source
source = resolve_source(None)
assert type(source).__name__ == "GitHubReleaseSource", type(source)
print("ok")
'''


def test_updater_path_imports_only_the_standard_library():
    code = "BACKEND_DIR = %r\n" % str(BACKEND) + PROBE
    r = subprocess.run([sys.executable, "-I", "-B", "-c", code], cwd=BACKEND, capture_output=True, text=True,
                       env={"PYTHONPATH": str(BACKEND)}, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-1500:]


def test_saved_token_lookup_without_keyring_falls_back(monkeypatch):
    from hokma.update import github

    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.setitem(sys.modules, "hokma.connections.store", None)
    monkeypatch.setattr(github, "_token_from_git", lambda: "from-git")
    assert github.github_token() == "from-git"
