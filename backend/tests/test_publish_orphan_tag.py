"""publish_release.remove_orphan_tag deletes a remote tag only on an explicit "release not found"."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "publish_release.py"


def load_script():
    sys.path.insert(0, str(SCRIPT.parent.parent / "backend"))
    spec = importlib.util.spec_from_file_location("publish_release_orphan_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def orphan_case(monkeypatch, view_result):
    module = load_script()
    removed = []

    def fake_git(root, *a, **kw):
        if a[0] == "ls-remote":
            return subprocess.CompletedProcess(a, 0, "abc\trefs/tags/v0.3.0\n", "")
        removed.append(a)
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(module, "git", fake_git)
    monkeypatch.setattr(module, "run_isolated", lambda name, args, **kw: view_result)
    module.remove_orphan_tag(Path("."), "v0.3.0")
    return removed


def test_the_orphan_tag_is_deleted_only_on_an_explicit_release_not_found(monkeypatch):
    not_found = subprocess.CompletedProcess([], 1, "", "release not found")
    assert orphan_case(monkeypatch, not_found) == [("push", "origin", ":refs/tags/v0.3.0")]


@pytest.mark.parametrize("view", [
    subprocess.CompletedProcess([], 1, "", "error connecting to api.github.com"),
    subprocess.CompletedProcess([], 1, "", "HTTP 403: API rate limit exceeded"),
    subprocess.CompletedProcess([], 4, "", ""),
    None,
])
def test_any_other_answer_keeps_the_tag_and_warns(monkeypatch, capsys, view):
    assert orphan_case(monkeypatch, view) == []
    assert "nothing was deleted" in capsys.readouterr().err


def test_a_release_that_exists_keeps_the_tag(monkeypatch):
    assert orphan_case(monkeypatch, subprocess.CompletedProcess([], 0, "title: Hokma 0.3.0\n", "")) == []
