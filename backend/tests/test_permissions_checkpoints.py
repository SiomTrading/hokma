"""Restore points (test P10): the safety net for what the list of protections cannot see."""
import os
import subprocess
import sys

import pytest

from hokma.kit import hardblock
from hokma.kit.settings import protected_paths
from hokma.permissions import checkpoints
from permissions_support import HEADERS, git, owner, sha  # noqa: F401  (fixture)


def refs(work) -> list[str]:
    return git(work, "for-each-ref", "--format=%(refname)", "refs/hokma/checkpoints/").splitlines()


def test_a_snapshot_touches_nothing_of_the_students_git(owner):
    work = owner.repo()
    (work / "untracked.txt").write_text("new, not added\n")
    (work / ".gitignore").write_text("*.log\n")
    (work / "debug.log").write_text("ignored\n")
    state = (git(work, "rev-parse", "HEAD"), git(work, "branch", "--show-current"), git(work, "status", "--porcelain"),
            git(work, "log", "--oneline"), (work / ".git" / "index").read_bytes())
    result = checkpoints.take(work, "12")
    assert result["taken"] is True and len(refs(work)) == 1
    after = (git(work, "rev-parse", "HEAD"), git(work, "branch", "--show-current"), git(work, "status", "--porcelain"),
             git(work, "log", "--oneline"), (work / ".git" / "index").read_bytes())
    assert after == state                                     # no commit, no branch move, the real index is untouched
    commit = refs(work)[0]
    files = git(work, "ls-tree", "-r", "--name-only", commit).splitlines()
    assert "untracked.txt" in files and "notes.md" in files and "debug.log" not in files   # untracked in, ignored out
    assert "conversation=12" in git(work, "log", "-1", "--format=%s", commit)


def test_no_new_snapshot_when_nothing_changed(owner):
    work = owner.repo()
    assert checkpoints.take(work)["taken"] is True
    again = checkpoints.take(work)
    assert again == {"taken": False, "reason": "unchanged"} and len(refs(work)) == 1
    (work / "notes.md").write_text("changed\n")
    assert checkpoints.take(work)["taken"] is True and len(refs(work)) == 2


def test_a_folder_without_git_has_no_restore_points_and_says_so(owner):
    assert checkpoints.take(owner.work) == {"taken": False, "reason": "no_repo"}
    res = owner.client.get("/api/checkpoints", headers=HEADERS)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "no_git"


def test_retention_is_seven_days_or_two_hundred(owner, monkeypatch):
    work = owner.repo()
    base = 1_800_000_000
    day = 24 * 3600
    for n, age in enumerate((9 * day, 8 * day, 2 * day, 3600)):
        (work / "notes.md").write_text(f"version {n}\n")
        assert checkpoints.take(work, now=base - age)["taken"] is True
    (work / "notes.md").write_text("version now\n")
    assert checkpoints.take(work, now=base)["taken"] is True
    assert len(refs(work)) == 3                                # the two older than 7 days were let go
    monkeypatch.setattr(checkpoints, "KEEP_COUNT", 2)
    (work / "notes.md").write_text("version later\n")
    assert checkpoints.take(work, now=base + 10)["taken"] is True
    assert len(refs(work)) == 2


def test_view_changes_and_restore_one_file_identical(owner):
    work = owner.repo()
    original = sha(work / "informes" / "viejo.md")
    point = checkpoints.take(work)["id"]
    (work / "informes" / "viejo.md").unlink()
    (work / "informes").rmdir()
    (work / "data.csv").write_text("a,b\n9,9\n")
    (work / "created-later.txt").write_text("not in the snapshot\n")
    seen = owner.client.get(f"/api/checkpoints/{point}/changes", headers=HEADERS).json()
    assert seen["deleted"] == ["informes/viejo.md"] and seen["changed"] == ["data.csv"] and seen["truncated"] is False
    listing = owner.client.get("/api/checkpoints", headers=HEADERS).json()
    assert [p["id"] for p in listing["points"]] == [point]
    res = owner.client.post(f"/api/checkpoints/{point}/restore-file", json={"path": "informes/viejo.md"}, headers=HEADERS)
    assert res.status_code == 200 and res.json() == {"restored": ["informes/viejo.md"], "renamed": False}
    assert sha(work / "informes" / "viejo.md") == original


def test_restore_never_deletes_or_overwrites_what_exists_now(owner):
    work = owner.repo()
    point = checkpoints.take(work)["id"]
    (work / "notes.md").unlink()
    (work / "informes" / "viejo.md").unlink()
    (work / "data.csv").write_text("a,b\nmy newer work\n")
    (work / "created-later.txt").write_text("keep me\n")
    res = owner.client.post(f"/api/checkpoints/{point}/restore-deleted", json={}, headers=HEADERS).json()
    assert res["restored"] == ["informes/viejo.md", "notes.md"] and res["count"] == 2
    assert (work / "data.csv").read_text() == "a,b\nmy newer work\n"            # changed, not deleted: untouched
    assert (work / "created-later.txt").read_text() == "keep me\n"
    one = owner.client.post(f"/api/checkpoints/{point}/restore-file", json={"path": "data.csv"}, headers=HEADERS).json()
    assert one == {"restored": ["data (restored).csv"], "renamed": True}
    assert (work / "data.csv").read_text() == "a,b\nmy newer work\n"
    assert (work / "data (restored).csv").read_text() == "a,b\n1,2\n"


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/hostname", ".git/config", "a\\b", "", "nope/not-there.txt"])
def test_restore_file_rejects_paths_that_are_not_in_the_snapshot(owner, path):
    work = owner.repo()
    point = checkpoints.take(work)["id"]
    res = owner.client.post(f"/api/checkpoints/{point}/restore-file", json={"path": path}, headers=HEADERS)
    assert res.status_code == 400 and res.json()["detail"]["code"] == "bad_path"
    assert owner.client.post("/api/checkpoints/20200101T000000Z-abcdef0/restore-deleted", json={},
                             headers=HEADERS).status_code == 404
    assert owner.client.post("/api/checkpoints/not-an-id/restore-deleted", json={}, headers=HEADERS).status_code == 404


def test_checkpoint_routes_need_the_token(owner):
    owner.repo()
    for method, path, body in (("get", "/api/checkpoints", None),
                               ("get", "/api/checkpoints/20200101T000000Z-abcdef0/changes", None),
                               ("post", "/api/checkpoints/20200101T000000Z-abcdef0/restore-file", {"path": "x"}),
                               ("post", "/api/checkpoints/20200101T000000Z-abcdef0/restore-deleted", {})):
        for headers in ({}, {"X-Hokma-Token": "wrong"}):
            res = getattr(owner.client, method)(path, headers=headers, **({"json": body} if body is not None else {}))
            assert res.status_code == 401


def test_p10_the_evasion_outside_the_list_deletes_and_the_net_brings_it_back(owner):
    """`getattr(os, 're' + 'move')` is not on the list. The hook lets it run (the risk that remains) and the restore
    point taken before the turn shows the file as gone and brings it back identical."""
    work = owner.repo()
    precious = work / "informes" / "viejo.md"
    before = sha(precious)
    (work / "evade.py").write_text("import os\nprint('deleting')\ngetattr(os, 're' + 'move')('informes/viejo.md')\n")
    point = checkpoints.take(work, "5")["id"]                 # what the engine does before the turn

    protect = [str(p) for p in protected_paths()]
    ctx = hardblock.Ctx(str(owner.data), [str(work)], protect, cwd=str(work))
    verdict = hardblock.evaluate({"tool_name": "Bash", "tool_input": {"command": "python evade.py"}, "cwd": str(work)}, ctx)
    assert verdict is None                                    # documented residual risk: the text does not name a delete

    subprocess.run([sys.executable, "evade.py"], cwd=work, check=True, capture_output=True)
    assert not precious.exists()

    seen = owner.client.get(f"/api/checkpoints/{point}/changes", headers=HEADERS).json()
    assert "informes/viejo.md" in seen["deleted"]
    res = owner.client.post(f"/api/checkpoints/{point}/restore-file", json={"path": "informes/viejo.md"}, headers=HEADERS)
    assert res.status_code == 200
    assert sha(precious) == before


def test_the_engine_takes_a_point_before_each_turn_only_in_auto_mode(tmp_path, monkeypatch):
    import asyncio

    from hokma.engine.engine import ChatEngine
    from hokma.engine.memory import MemoryStore, WorkdirProjects
    from hokma.kit.launch import LaunchSpec
    from engine_support import Env

    env = Env(tmp_path, monkeypatch)
    from hokma.db import migrate

    migrate()                                                  # the points Hokma takes are recorded in its database
    work = env.home
    git(work, "init", "-q")
    (work / "a.txt").write_text("one\n")
    mode = ["ask"]
    engine = ChatEngine(store=MemoryStore(), projects=WorkdirProjects(work),
                        launch=lambda: LaunchSpec(args=[], env={"CLAUDE_CONFIG_DIR": str(env.data / "cfg")}, cwd=str(work)),
                        session_token="t", stop_grace=0.5, permission_mode=lambda: mode[0], blocks=lambda: [])

    async def one_turn(text):
        from hokma.engine.engine import Connection

        sent = []

        async def send(raw):
            sent.append(raw)

        conn = Connection(send)
        await engine.handle(conn, '{"type":"send","text":"%s"}' % text)
        for _ in range(400):
            await asyncio.sleep(0.05)
            if any('"type": "done"' in s for s in sent):
                return
        raise AssertionError("no done")

    async def go():
        await one_turn("hello in ask mode")
        assert refs(work) == []
        mode[0] = "auto"
        await one_turn("hello in auto mode")
        assert len(refs(work)) == 1
        await one_turn("again, nothing changed")
        assert len(refs(work)) == 1
        (work / "a.txt").write_text("two\n")
        await one_turn("after a change")
        assert len(refs(work)) == 2
        await engine.shutdown()

    asyncio.run(go())
    starts = env.entries("start")
    assert starts and "bypassPermissions" in starts[-1]["argv"] and "default" in starts[0]["argv"]
