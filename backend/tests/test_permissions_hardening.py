"""Hardening of the permission features (task 3045): links in the way of a restore, forged restore points, and the
Git files that hold the restore point refs."""
import os
import subprocess

import pytest

from hokma.permissions import checkpoints, trash
from hokma.kit.settings import protected_paths
from hardblock_support import box  # noqa: F401  (fixture)
from permissions_support import HEADERS, git, owner  # noqa: F401  (fixture)


def protect() -> list[str]:
    return [str(p) for p in protected_paths()]


def move(owner, paths):
    return trash.move(paths, "not needed", db=owner.db, roots=[str(owner.work)], protect=protect(), cwd=str(owner.work))


# ------------------------------------------------------------------------------------------------ finding 1

def test_restore_refuses_a_link_in_the_parent_that_leads_out_of_the_working_folder(owner, tmp_path):
    (owner.work / "sub").mkdir()
    (owner.work / "sub" / "report.md").write_text("secret report\n")
    done = move(owner, ["sub/report.md"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (owner.work / "sub").rmdir()
    (owner.work / "sub").symlink_to(outside)                       # the agent swaps the folder for a link
    res = owner.client.post(f"/api/trash/{done[0]['id']}/restore", headers=HEADERS)
    assert res.status_code == 400 and res.json()["detail"]["code"] == "outside"
    assert list(outside.iterdir()) == []                           # nothing was written out there
    assert os.path.lexists(done[0]["stored"])                      # and the item is still in the trash


def test_restore_still_works_when_the_parent_is_a_link_that_stays_inside(owner):
    (owner.work / "real").mkdir()
    (owner.work / "real" / "a.md").write_text("a\n")
    done = move(owner, ["real/a.md"])
    (owner.work / "alias").symlink_to(owner.work / "real")
    res = owner.client.post(f"/api/trash/{done[0]['id']}/restore", headers=HEADERS)
    assert res.status_code == 200 and (owner.work / "real" / "a.md").read_text() == "a\n"


def test_the_source_root_is_recorded_when_moving(owner):
    (owner.work / "n.md").write_text("n\n")
    done = move(owner, ["n.md"])
    with owner.db.read() as u:
        assert u.trash.source_root(done[0]["id"]) == os.path.realpath(owner.work)


# ------------------------------------------------------------------------------------------------ finding 2

def point_of(owner):
    work = owner.repo({"d/ok.txt": "ok\n", "sub/.claude/settings.json": "{}\n", "sub/.git2": "x\n",
                       "notes.md": "notes\n"})
    assert checkpoints.take(work)["taken"] is True
    return work, checkpoints.listing(work)[0]["id"]


def test_a_restore_never_writes_into_a_protected_place_reached_through_a_link(owner):
    work, point = point_of(owner)
    (work / ".hokma").mkdir()
    for child in (work / "d").iterdir():
        child.unlink()
    (work / "d").rmdir()
    (work / "d").symlink_to(work / ".hokma")                       # inside the project, but Hokma's own folder
    res = owner.client.post(f"/api/checkpoints/{point}/restore-file", json={"path": "d/ok.txt"}, headers=HEADERS)
    assert res.status_code == 400 and res.json()["detail"]["code"] == "protected"
    assert list((work / ".hokma").iterdir()) == []
    done = owner.client.post(f"/api/checkpoints/{point}/restore-deleted", json={}, headers=HEADERS).json()
    assert "d/ok.txt" in done["skipped"] and list((work / ".hokma").iterdir()) == []


def test_a_restore_refuses_nested_hokma_claude_and_git_parts(owner):
    work, point = point_of(owner)
    for rel in ("sub/.claude/settings.json", "x/.git/config", "a/.hokma/b", "sub/.CLAUDE/x"):
        res = owner.client.post(f"/api/checkpoints/{point}/restore-file", json={"path": rel}, headers=HEADERS)
        assert res.status_code == 400 and res.json()["detail"]["code"] == "bad_path", rel
    (work / "sub" / ".claude" / "settings.json").unlink()
    (work / "sub" / ".claude").rmdir()
    done = owner.client.post(f"/api/checkpoints/{point}/restore-deleted", json={}, headers=HEADERS).json()
    assert done["skipped"] == ["sub/.claude/settings.json"] and not (work / "sub" / ".claude").exists()


def test_a_ref_the_agent_forged_is_not_a_restore_point(owner):
    work = owner.repo()
    assert checkpoints.take(work)["taken"] is True
    real = checkpoints.listing(work)[0]["id"]
    # the agent builds its own commit holding a file under a protected-looking name and plants a ref for it
    (work / "bad.txt").write_text("planted\n")
    git(work, "add", "bad.txt")
    tree = git(work, "write-tree")
    commit = git(work, "commit-tree", tree, "-m", "hokma checkpoint")
    (work / ".git" / "refs" / "hokma" / "checkpoints").mkdir(parents=True, exist_ok=True)
    forged = "20990101T000000Z-" + commit[:7]
    (work / ".git" / "refs" / "hokma" / "checkpoints" / forged).write_text(commit + "\n")
    git(work, "reset", "-q")
    assert [p["id"] for p in checkpoints.listing(work)] == [real]
    (work / "bad.txt").unlink()
    res = owner.client.post(f"/api/checkpoints/{forged}/restore-file", json={"path": "bad.txt"}, headers=HEADERS)
    assert res.status_code == 404 and not (work / "bad.txt").exists()
    # and a genuine ref moved to another commit is ignored too
    refpath = work / ".git" / "refs" / "hokma" / "checkpoints" / real
    refpath.write_text(commit + "\n")
    assert checkpoints.listing(work) == []


# ------------------------------------------------------------------------------------------------ finding 3

REF_FILES = [".git/packed-refs", ".git/refs/hokma/checkpoints/20990101T000000Z-abcdef0", ".git/refs/hokma"]


@pytest.mark.parametrize("rel", REF_FILES)
def test_the_hook_blocks_writing_the_restore_point_refs_directly(box, rel):
    (box.work / ".git" / "refs").mkdir(parents=True, exist_ok=True)
    target = str(box.work / rel)
    assert box.hit("Write", file_path=target, content="abc\n") == "hokma_internal"
    assert box.hit("Edit", file_path=target, old_string="a", new_string="b") == "hokma_internal"
    assert box.hit("MultiEdit", file_path=target, edits=[{"old_string": "a", "new_string": "b"}]) == "hokma_internal"
    assert box.bash(f"echo abc > {rel}") == "hokma_internal"
    assert box.bash(f"cp notes.md {rel}") == "hokma_internal"
    assert box.bash(f"printf x >> {target}") == "hokma_internal"
    assert box.bash(f"cd .git && cp ../notes.md {rel[5:]}") == "hokma_internal"


def test_the_hook_still_allows_the_rest_of_git(box):
    (box.work / ".git").mkdir()
    assert box.bash("git status") is None and box.bash("git log --oneline | head -3") is None
    assert box.hit("Read", file_path=str(box.work / ".git" / "HEAD")) is None
    assert box.bash("git pack-refs --all") is None


# ------------------------------------------------------------------------------------------------ round two (3045)

def test_a_checkpoint_restore_refuses_a_project_link_that_lands_inside_git(owner):
    work = owner.repo({"docs/guide.txt": "guide\n", "notes.md": "notes\n"})
    assert checkpoints.take(work)["taken"] is True
    point = checkpoints.listing(work)[0]["id"]
    (work / "docs" / "guide.txt").unlink()
    (work / "docs").rmdir()
    (work / "docs").symlink_to(work / ".git")                      # a project link whose target is .git
    before = sorted(os.listdir(work / ".git"))
    res = owner.client.post(f"/api/checkpoints/{point}/restore-file", json={"path": "docs/guide.txt"}, headers=HEADERS)
    assert res.status_code == 400 and res.json()["detail"]["code"] == "bad_path"
    assert sorted(os.listdir(work / ".git")) == before
    done = owner.client.post(f"/api/checkpoints/{point}/restore-deleted", json={}, headers=HEADERS).json()
    assert "docs/guide.txt" in done["skipped"] and sorted(os.listdir(work / ".git")) == before


def test_a_checkpoint_restore_refuses_any_link_between_the_project_and_the_file(owner):
    work = owner.repo({"d/e/f.txt": "f\n"})
    assert checkpoints.take(work)["taken"] is True
    point = checkpoints.listing(work)[0]["id"]
    (work / "d" / "e" / "f.txt").unlink()
    (work / "d" / "e").rmdir()
    (work / "d" / "e").symlink_to(work / "d")                      # stays inside, still a link in the way
    with pytest.raises(checkpoints.CheckpointError) as caught:
        checkpoints.restore_file(work, point, "d/e/f.txt", protect())
    assert caught.value.code == "bad_path"


def test_a_checkpoint_restore_writes_to_the_checked_path_without_following_a_late_link(owner, tmp_path, monkeypatch):
    work = owner.repo({"a.txt": "one\n"})
    assert checkpoints.take(work)["taken"] is True
    point = checkpoints.listing(work)[0]["id"]
    (work / "a.txt").unlink()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n")
    real_open = os.open

    def plant(path, flags, *args, **kwargs):                       # a link appears between the check and the write
        if str(path).endswith("a.txt") and not os.path.lexists(path):
            (work / "a.txt").symlink_to(victim)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(checkpoints.os, "open", plant)
    with pytest.raises(checkpoints.CheckpointError) as caught:
        checkpoints.restore_file(work, point, "a.txt", protect())
    assert caught.value.code == "restore_failed"
    assert victim.read_text() == "keep\n"


def test_a_checkpoint_restore_opens_the_resolved_path_with_no_follow(owner, monkeypatch):
    work = owner.repo({"a.txt": "one\n"})
    assert checkpoints.take(work)["taken"] is True
    point = checkpoints.listing(work)[0]["id"]
    (work / "a.txt").unlink()
    seen = []
    real_open = os.open

    def spy(path, flags, *args, **kwargs):
        seen.append((str(path), flags))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(checkpoints.os, "open", spy)
    checkpoints.restore_file(work, point, "a.txt", protect())
    path, flags = seen[-1]
    assert path == os.path.join(os.path.realpath(work), "a.txt")
    assert flags & os.O_NOFOLLOW and flags & os.O_EXCL
    assert (work / "a.txt").read_text() == "one\n"


def test_a_trash_restore_refuses_a_project_link_that_lands_inside_git(owner):
    (owner.work / "docs").mkdir()
    (owner.work / "docs" / "x.md").write_text("x\n")
    done = move(owner, ["docs/x.md"])
    (owner.work / "docs").rmdir()
    (owner.work / ".git").mkdir()
    (owner.work / "docs").symlink_to(owner.work / ".git")
    before = sorted(os.listdir(owner.work / ".git"))
    res = owner.client.post(f"/api/trash/{done[0]['id']}/restore", headers=HEADERS)
    assert res.status_code == 400 and res.json()["detail"]["code"] == "protected"
    assert sorted(os.listdir(owner.work / ".git")) == before and os.path.lexists(done[0]["stored"])


def test_a_trash_restore_refuses_a_link_in_the_chain_that_stays_inside(owner):
    (owner.work / "real").mkdir()
    (owner.work / "real" / "sub").mkdir()
    (owner.work / "real" / "sub" / "a.md").write_text("a\n")
    done = move(owner, ["real/sub/a.md"])
    (owner.work / "real" / "sub").rmdir()
    (owner.work / "real" / "other").mkdir()
    (owner.work / "real" / "sub").symlink_to(owner.work / "real" / "other")
    with pytest.raises(trash.TrashError) as caught:
        trash.restore(owner.db, done[0]["id"], protect=protect())
    assert caught.value.code == "outside"
    assert os.listdir(owner.work / "real" / "other") == []
