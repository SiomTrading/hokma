"""Only `backend/hokma/backtest/_lib.py` imports the backtest engine (SPEC-020 rule 4, N-27)."""
import subprocess

import pytest

from backtest.support import ROOT, imports_of, is_engine_module

ALLOWED = {"backend/hokma/backtest/_lib.py"}


def python_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line and (ROOT / line).is_file()]


def engine_importers(files: dict[str, str]) -> list[str]:
    out = []
    for rel, source in files.items():
        try:
            modules = [m for _, m in imports_of(source)]
        except SyntaxError:
            continue
        if any(is_engine_module(m) for m in modules) and rel not in ALLOWED:
            out.append(rel)
    return sorted(out)


def test_no_python_file_outside_the_lib_imports_the_engine():
    files = {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in python_files()}
    assert "backend/hokma/backtest/_lib.py" in files
    assert engine_importers(files) == []


def test_the_lib_does_import_it(tmp_path):
    source = (ROOT / "backend/hokma/backtest/_lib.py").read_text(encoding="utf-8")
    assert any(is_engine_module(m) for _, m in imports_of(source))


@pytest.mark.parametrize("rel", ["backend/hokma/backtest/engine.py", "backend/hokma/backtest/fills.py", "backend/hokma/exec/x.py", "backend/tests/backtest/test_x.py"])
@pytest.mark.parametrize("form", ["import {top}", "import {top}.backtest.engine", "from {top} import version", "from {top}.model.data import Bar",
                                  "from {top}.backtest.models import FillModel", "import numpy, {top}"])
def test_the_scan_fails_when_a_file_outside_the_lib_imports_the_engine(rel, form):
    source = (ROOT / "backend/hokma/backtest/_lib.py").read_text(encoding="utf-8")
    top = next(m for _, m in imports_of(source) if is_engine_module(m)).split(".")[0]
    assert engine_importers({rel: form.format(top=top) + "\n"}) == [rel]
    assert engine_importers({"backend/hokma/backtest/_lib.py": form.format(top=top) + "\n"}) == []


def test_other_imports_do_not_trip_the_scan():
    assert engine_importers({"backend/hokma/backtest/engine.py": "import numpy\nfrom hokma.backtest import _lib\nimport trader_tools\n"}) == []
