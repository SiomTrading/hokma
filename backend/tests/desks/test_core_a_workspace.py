"""Desk folder (OA-12, OA-21) and templates (OA-22, OA-23, OA-24, OA-25)."""
import json
import os
import shutil
from pathlib import Path

import pytest

from hokma.desks.core import workspace as ws
from hokma.projects import gitops

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ("desk-chat", "event-research", "quant-research")


def snapshot(root: Path):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(root).parts}


def test_bootstrap_creates_the_folder_with_its_structure_OA_12(tmp_path):
    report = ws.bootstrap_workspace(tmp_path / "desk", desk_id=7, desk_name="Swing desk", with_git=False)
    root = tmp_path / "desk"
    for name in ws.STRUCTURE:
        assert (root / name).is_dir()
    marker = json.loads((root / ws.MARKER).read_text())
    assert marker == {"schema_version": 1, "desk_id": 7, "desk_name": "Swing desk"}
    assert ".env" in (root / ".gitignore").read_text() and report.changed and report.git == "skipped"


def test_running_twice_changes_nothing_OA_12(tmp_path):
    root = tmp_path / "desk"
    ws.bootstrap_workspace(root, desk_id=7, desk_name="A", with_git=False)
    before = snapshot(root)
    again = ws.bootstrap_workspace(root, desk_id=7, desk_name="A", with_git=False)
    assert snapshot(root) == before and not again.created and not again.changed


def test_what_the_owner_wrote_is_never_touched_OA_12(tmp_path):
    root = tmp_path / "desk"
    (root / "reports").mkdir(parents=True)
    (root / "reports" / "mine.md").write_text("MINE")
    (root / ".gitignore").write_text("custom\n")
    ws.bootstrap_workspace(root, desk_id=1, desk_name="A", with_git=False)
    assert (root / "reports" / "mine.md").read_text() == "MINE" and (root / ".gitignore").read_text() == "custom\n"
    assert not (root / "reports" / ".gitkeep").exists() and (root / "journal" / ".gitkeep").exists()


def test_a_folder_of_another_desk_is_refused_OA_12(tmp_path):
    root = tmp_path / "desk"
    ws.bootstrap_workspace(root, desk_id=1, desk_name="A", with_git=False)
    with pytest.raises(ws.WorkspaceError, match="belongs to desk 1"):
        ws.bootstrap_workspace(root, desk_id=2, desk_name="B", with_git=False)
    (root / ws.MARKER).write_text("not json")
    with pytest.raises(ws.WorkspaceError, match="not valid JSON"):
        ws.bootstrap_workspace(root, desk_id=1, desk_name="A", with_git=False)


def test_a_symbolic_link_as_root_or_inside_is_refused_OA_12(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ws.WorkspaceError, match="symbolic link"):
        ws.bootstrap_workspace(link, desk_id=1, desk_name="A", with_git=False)
    root = tmp_path / "desk"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".desk").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ws.WorkspaceError):
        ws.bootstrap_workspace(root, desk_id=1, desk_name="A", with_git=False)
    assert not (outside / "workspace.json").exists()


@pytest.mark.skipif(not gitops.git_installed(), reason="git is not installed")
def test_history_is_created_once_with_controlled_output_OA_21(tmp_path, capfd):
    root = tmp_path / "desk"
    first = ws.bootstrap_workspace(root, desk_id=1, desk_name="A")
    assert first.git == "created" and (root / ".git").is_dir()
    (root / "notes" / "x.md").write_text("x")
    (root / ".env").write_text("SECRET=1")
    second = ws.bootstrap_workspace(root, desk_id=1, desk_name="A")
    assert second.git == "existing" and not second.changed
    out = capfd.readouterr()
    assert out.out == "" and out.err == ""
    tracked = gitops.log(root)
    assert tracked and any("desk folder" in line for line in tracked)
    assert not (root / ".env").read_text() == ""       # still there, but ignored by the history
    import subprocess
    listed = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True).stdout.split()
    assert ".env" not in listed and ".gitignore" in listed and ".desk/workspace.json" in listed


def test_history_failures_are_reported_not_raised_OA_21(tmp_path, monkeypatch):
    monkeypatch.setattr(gitops, "git_installed", lambda: False)
    assert ws.bootstrap_workspace(tmp_path / "a", desk_id=1, desk_name="A").git == "unavailable"
    monkeypatch.setattr(gitops, "git_installed", lambda: True)
    monkeypatch.setattr(gitops, "init_repo", lambda root, message: gitops.GitResult(False, "failed"))
    assert ws.bootstrap_workspace(tmp_path / "b", desk_id=1, desk_name="A").git == "failed"
    monkeypatch.setattr(gitops, "init_repo", lambda root, message: gitops.GitResult(False, "missing"))
    assert ws.bootstrap_workspace(tmp_path / "c", desk_id=1, desk_name="A").git == "unavailable"
    (tmp_path / "d" / ".git").mkdir(parents=True)
    monkeypatch.setattr(gitops, "repo_state", lambda root: gitops.GitResult(False, "unsafe"))
    assert ws.bootstrap_workspace(tmp_path / "d", desk_id=1, desk_name="A").git == "unsafe"


def test_index_is_bounded_and_skips_history_and_links_OA_12(tmp_path):
    root = tmp_path / "desk"
    ws.bootstrap_workspace(root, desk_id=1, desk_name="A", with_git=False)
    (root / ".git").mkdir()
    (root / ".git" / "hidden").write_text("x" * 50)
    for i in range(5):
        p = root / "reports" / f"r{i}.md"
        p.write_text("x" * (i + 1))
        os.utime(p, (4_000_000_000 + i, 4_000_000_000 + i))
    (root / "notes" / "link.md").symlink_to(root / "reports" / "r0.md")
    index = ws.index_workspace(root)
    assert index.folders["reports"] == {"files": 5, "bytes": 15} and ".git" not in index.folders
    assert "notes" not in index.folders and index.total_files == 7      # five reports, the marker and the ignore file
    assert [p for p, _ in index.recent[:2]] == ["reports/r4.md", "reports/r3.md"]
    small = ws.index_workspace(root, max_entries=3)
    assert small.truncated and small.total_files == 3
    assert json.loads(json.dumps(index.to_dict()))["total_files"] == 7


# ---- templates ----

def test_template_manifests_are_current_OA_22_OA_24_OA_25():
    assert ws.list_templates() == list(TEMPLATES)
    for name in TEMPLATES:
        assert ws.verify_template(name) == [], name
        meta = ws.load_template(name)
        approved = {a["version"]: a["digest"] for a in meta["approved_versions"]}
        assert approved[meta["version"]] == ws.template_digest(meta["files"])


def test_quant_research_readme_splits_the_responsibilities_OA_23():
    text = (ROOT / "kit/template/desk/templates/quant-research/README.md").read_text()
    for role in ("Desk chief", "Data agent", "Quant modules", "Judge", "Reviewer", "Owner"):
        assert f"| {role} |" in text
    assert "No agent computes a metric by hand" in text


def test_event_research_lists_approved_versions_with_exact_digest_OA_24():
    meta = ws.load_template("event-research")
    assert meta["approved_versions"] and all(len(a["digest"]) == 64 for a in meta["approved_versions"])
    assert meta["lines"] == ["SWO", "LP"]


def test_desk_chat_template_serves_every_line_and_injects_every_rule_OA_25():
    meta = ws.load_template("desk-chat")
    assert meta["lines"] == ["RF", "DT", "SWC", "SWO", "LP"] and set(meta["inject_rules"]) == set(ws.COMMON_RULES)


@pytest.fixture
def kit(tmp_path):
    shutil.copytree(ROOT / "kit/template/desk", tmp_path / "kit/template/desk")
    return tmp_path


def prepared(tmp_path):
    root = tmp_path / "desk"
    ws.bootstrap_workspace(root, desk_id=1, desk_name="A", with_git=False)
    return root


def test_apply_copies_after_verifying_version_and_digest_OA_22(tmp_path):
    root = prepared(tmp_path)
    report = ws.apply_template("quant-research", root, expected_version="1.0.0")
    assert "research/brief.md" in report.copied and ".desk/template/README.md" in report.copied
    assert (root / "research" / "evidence.md").is_file()
    receipt = json.loads((root / ws.RECEIPT).read_text())
    assert receipt["name"] == "quant-research" and receipt["version"] == "1.0.0" and len(receipt["digest"]) == 64


def test_apply_twice_changes_nothing_OA_22(tmp_path):
    root = prepared(tmp_path)
    ws.apply_template("desk-chat", root, expected_version="1.0.0")
    before = snapshot(root)
    again = ws.apply_template("desk-chat", root, expected_version="1.0.0")
    assert again.already_applied and snapshot(root) == before


def test_wrong_version_copies_nothing_OA_22(tmp_path):
    root = prepared(tmp_path)
    before = snapshot(root)
    with pytest.raises(ws.TemplateError, match="not the expected 9.9.9"):
        ws.apply_template("quant-research", root, expected_version="9.9.9")
    assert snapshot(root) == before


def test_a_changed_template_is_not_applied_OA_24(kit, tmp_path):
    (kit / "kit/template/desk/templates/event-research/README.md").write_text("tampered\n")
    root = prepared(tmp_path)
    before = snapshot(root)
    with pytest.raises(ws.TemplateError, match="content changed"):
        ws.apply_template("event-research", root, expected_version="1.0.0", root=kit)
    assert snapshot(root) == before


def test_stale_approved_digest_is_reported_OA_24(kit):
    path = kit / "kit/template/desk/templates/desk-chat/template.json"
    meta = json.loads(path.read_text())
    meta["approved_versions"][0]["digest"] = "0" * 64
    path.write_text(json.dumps(meta))
    assert any("no approved digest" in p for p in ws.verify_template("desk-chat", root=kit))
    ws.write_template_manifest("desk-chat", root=kit)
    assert ws.verify_template("desk-chat", root=kit) == []


def test_existing_files_are_kept_and_a_second_template_needs_an_upgrade_OA_22(tmp_path):
    root = prepared(tmp_path)
    (root / "research").mkdir()
    (root / "research" / "brief.md").write_text("MY BRIEF")
    report = ws.apply_template("quant-research", root, expected_version="1.0.0")
    assert "research/brief.md" in report.skipped and (root / "research" / "brief.md").read_text() == "MY BRIEF"
    with pytest.raises(ws.TemplateError, match="explicit upgrade"):
        ws.apply_template("desk-chat", root, expected_version="1.0.0")


def test_template_needs_a_prepared_folder_and_a_real_name_OA_22(tmp_path):
    with pytest.raises(ws.TemplateError, match="bootstrap_workspace"):
        ws.apply_template("desk-chat", tmp_path, expected_version="1.0.0")
    for bad in ("../x", "nope", ".hidden", ""):
        with pytest.raises(ws.TemplateError):
            ws.load_template(bad)


def test_line_endings_do_not_change_the_digest_OA_24(kit):
    readme = kit / "kit/template/desk/templates/desk-chat/README.md"
    readme.write_bytes(readme.read_bytes().replace(b"\n", b"\r\n"))
    assert ws.verify_template("desk-chat", root=kit) == []
