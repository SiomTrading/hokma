"""E2E: a hostile student repo during a real `hokma update` and a re-install. Hokma never runs git on it."""
import os
import subprocess
import sys

import pytest

from helpers import before
from test_installer_e2e_update import NEXT, ROOT, Box, _new_kit, run  # helpers of the sibling module

pytestmark = pytest.mark.skipif(os.environ.get("HOKMA_E2E") != "1" or sys.platform == "win32",
                                reason="set HOKMA_E2E=1 (Linux/macOS, network) to run")


def _plant(tmp_path):
    marker, victim, plant = tmp_path / "PWNED", tmp_path / "victim.txt", tmp_path / "plant.sh"
    victim.write_text("keep\n")
    plant.write_text(f"#!/bin/sh\necho ran >> {marker}\nexit 0\n")
    plant.chmod(0o755)
    return marker, victim, plant


def _git_state(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in root.rglob("*") if p.is_file())


def test_update_with_planted_hooks_config_includes_and_symlinks_runs_nothing(tmp_path):
    box = Box(tmp_path)
    marker, victim, plant = _plant(tmp_path)
    hooks = box.work / ".git" / "hooks"
    for name in ("pre-commit", "commit-msg", "post-commit", "reference-transaction", "post-index-change"):
        (hooks / name).write_text(f"#!/bin/sh\necho {name} >> {marker}\nexit 0\n")
        (hooks / name).chmod(0o755)
    with open(box.work / ".git" / "config", "a") as f:
        f.write(f'[core]\n\tfsmonitor = {plant}\n\tsshCommand = {plant}\n\thooksPath = {hooks}\n'
                f'[hook "pre-commit"]\n\tcommand = {plant}\n[include]\n\tpath = {plant}\n'
                f'[filter "x"]\n\tclean = {plant}\n\tsmudge = {plant}\n[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = {plant}\n')
    (box.work / ".gitattributes").write_text("* filter=x\n")
    (box.work / "CLAUDE.md").unlink()
    (box.work / "CLAUDE.md").symlink_to(victim)                 # a kit file turned into a link to the outside
    git_before = _git_state(box.work / ".git")

    r = box.update(box.release(NEXT, mutate=_new_kit))
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.version() == f"hokma {NEXT}"
    assert not marker.exists(), "something planted in the repo ran during the update"
    assert victim.read_text() == "keep\n", "the kit wrote through a symlink"
    assert (box.work / "NEW-KIT.md").read_text() == f"new in {NEXT}\n"
    assert before("kit.skipped", "path", "error") in r.stdout and "CLAUDE.md" in r.stdout
    assert _git_state(box.work / ".git") == git_before, "Hokma touched the student's .git"


@pytest.mark.parametrize("kind", ["gitdir-file", "symlink"])
def test_reinstall_never_runs_git_through_a_redirected_dot_git(tmp_path, kind):
    """The initial repo is created only when NOTHING named .git exists; a redirected one is the student's."""
    box = Box(tmp_path)
    outside = tmp_path / "outside.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outside)], check=True)
    dot_git = box.work / ".git"
    run(["mv", dot_git, tmp_path / "moved.git"], box.env)
    if kind == "gitdir-file":
        dot_git.write_text(f"gitdir: {outside}\n")
    else:
        dot_git.symlink_to(outside)
    outside_before = _git_state(outside)

    r = run(["bash", ROOT / "installer" / "install.sh"], box.env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git_state(outside) == outside_before, "git ran through the redirected .git"
