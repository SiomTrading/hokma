"""The Library CI job, the smoke scripts and the 0.0.31 version (SPEC-010 P14). The jobs themselves only run on GitHub;
these checks keep their shape from drifting and run the load smoke here for real."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
SMOKE = (ROOT / ".github" / "workflows" / "model-smoke.yml").read_text(encoding="utf-8")
VERSION = "0.0.31"


def job(name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S[^\n]*:\n|\Z)", CI, re.M | re.S)
    assert match, f"job {name} missing from ci.yml"
    return match.group(1)


def test_library_job_runs_on_the_four_platforms():
    block = job("library-tests")
    for runner in ("windows-latest", "macos-latest", "macos-15-intel", "ubuntu-latest"):
        assert runner in block
    assert "fail-fast: false" in block
    assert "github.repository == 'SiomTrading/hokma-dev'" in block   # never the students' repository


def test_library_job_runs_every_library_test_without_the_slow_ones():
    block = job("library-tests")
    assert 'pytest -m "not slow"' in block
    for pattern in ("test_library_*.py", "test_folder_*.py", "test_model_download.py", "test_embedder.py",
                    "test_vector_store.py", "test_hybrid_search.py"):
        assert pattern in block
        assert list((ROOT / "backend" / "tests").glob(pattern)), f"{pattern} matches no test file"


def test_library_job_loads_sqlite_vec_with_the_python_hokma_installs():
    block = job("library-tests")
    assert "UV_PYTHON_PREFERENCE: only-managed" in block
    assert block.index("uv python install 3.12") < block.index("vec-load-smoke") < block.index("Library tests")
    assert "scripts/vec_load_smoke.py" in block
    assert "3.12" in (ROOT / ".python-version").read_text()


def test_model_smoke_is_manual_only_on_three_platforms():
    assert re.search(r"^on:\n  workflow_dispatch:\n", SMOKE, re.M)
    assert "push:" not in SMOKE and "pull_request" not in SMOKE and "schedule" not in SMOKE
    for runner in ("windows-latest", "macos-latest", "ubuntu-latest"):
        assert runner in SMOKE
    assert "scripts/model_smoke.py" in SMOKE
    assert (ROOT / "scripts" / "model_smoke.py").is_file()


def test_vec_load_smoke_passes_here_and_reports_a_failure(tmp_path):
    script = ROOT / "scripts" / "vec_load_smoke.py"
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120)
    if done.returncode != 0 and "no_load_extension" in done.stderr:
        pytest.skip("this Python cannot load SQLite extensions (reported by the script itself)")
    assert done.returncode == 0, done.stderr
    assert "nearest-vector" in done.stdout
    # A Python without the module must fail loudly: hide it with a blocking finder.
    blocker = tmp_path / "block.py"
    blocker.write_text("import sys\nsys.modules['sqlite_vec'] = None\nimport runpy\n"
                       f"runpy.run_path({str(script)!r}, run_name='__main__')\n", encoding="utf-8")
    failed = subprocess.run([sys.executable, str(blocker)], capture_output=True, text=True, timeout=120)
    assert failed.returncode == 1
    assert "module_missing" in failed.stderr


def test_every_version_place_says_the_new_version():
    init = (ROOT / "backend" / "hokma" / "__init__.py").read_text(encoding="utf-8")
    assert f'__version__ = "{VERSION}"' in init
    assert re.search(rf'^version = "{re.escape(VERSION)}"', (ROOT / "pyproject.toml").read_text(), re.M)
    assert re.search(rf'name = "hokma"\nversion = "{re.escape(VERSION)}"', (ROOT / "uv.lock").read_text())
    package = json.loads((ROOT / "dashboard" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "dashboard" / "package-lock.json").read_text(encoding="utf-8"))
    assert package["version"] == lock["version"] == lock["packages"][""]["version"] == VERSION
    assert (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").startswith(f"# Changelog\n\n## {VERSION}\n")
