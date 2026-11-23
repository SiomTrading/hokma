"""Session commands of the kit are well formed, pass the repo rules and are delivered by the kit sync (SPEC-017: ED-045)."""
import importlib.util
from pathlib import Path

import pytest

from hokma.kit import sync

ROOT = Path(__file__).resolve().parents[3]
COMMANDS = ROOT / "kit" / "template" / "commands"
_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)

NAMES = ("checkpoint", "catchup", "handoff", "wrapup")


@pytest.mark.parametrize("name", NAMES)
def test_command_has_frontmatter_with_its_own_name_ED_045(name):
    text = (COMMANDS / f"{name}.md").read_text(encoding="utf-8")
    assert text.startswith("---\n") and f"name: {name}\n" in text and "description:" in text
    assert "order" not in text.split("---")[1]


def test_commands_pass_the_repository_rules_ED_045():
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "scripts/check_repo_rules.py"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_kit_sync_delivers_the_commands_ED_045(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))      # a clean baseline: a file the student deleted stays deleted
    work = tmp_path / "work"
    work.mkdir()
    sync.sync_kit(ROOT / "kit" / "template", version="test", workdir=work)
    for name in NAMES:
        assert any(p.name == f"{name}.md" for p in work.rglob("*.md")), name
