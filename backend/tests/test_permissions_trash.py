"""The Hokma trash (test P7): moving instead of deleting, restoring identical, and who can empty it."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hokma.kit import trash_mcp
from hokma.kit.settings import protected_paths
from hokma.permissions import trash
from permissions_support import HEADERS, git, owner, sha  # noqa: F401  (fixture)

MCP_FILE = Path(trash_mcp.__file__).resolve()


def protect() -> list[str]:
    return [str(p) for p in protected_paths()]


def move(owner, paths, reason="not needed anymore"):
    return trash.move(paths, reason, db=owner.db, roots=[str(owner.work)], protect=protect(), cwd=str(owner.work))


def test_move_puts_the_file_in_the_trash_and_restore_returns_it_identical(owner):
    (owner.work / "notas.md").write_text("contenido original\n")
    before = sha(owner.work / "notas.md")
    done = move(owner, ["notas.md"])
    assert not (owner.work / "notas.md").exists()
    stored = Path(done[0]["stored"])
    assert stored.parent == owner.data / "trash" / str(done[0]["id"]) and stored.read_text() == "contenido original\n"
    listing = owner.client.get("/api/trash", headers=HEADERS).json()
    item = listing["items"][0]
    assert listing["count"] == 1 and item["name"] == "notas.md" and item["reason"] == "not needed anymore"
    assert item["original_path"] == str(owner.work / "notas.md") and item["size_bytes"] == len("contenido original\n")
    assert item["project_id"] is None or isinstance(item["project_id"], int)
    res = owner.client.post(f"/api/trash/{item['id']}/restore", headers=HEADERS)
    assert res.status_code == 200 and res.json()["restored_to"] == str(owner.work / "notas.md")
    assert sha(owner.work / "notas.md") == before
    assert owner.client.get("/api/trash", headers=HEADERS).json()["count"] == 0
    assert not (owner.data / "trash" / str(item["id"])).exists()


def test_restore_never_overwrites_a_file_that_is_there_now(owner):
    (owner.work / "notas.md").write_text("old\n")
    done = move(owner, ["notas.md"])
    (owner.work / "notas.md").write_text("new work\n")
    restored = owner.client.post(f"/api/trash/{done[0]['id']}/restore", headers=HEADERS).json()
    assert restored["renamed"] is True and restored["restored_to"] == str(owner.work / "notas (restored).md")
    assert (owner.work / "notas.md").read_text() == "new work\n"
    assert (owner.work / "notas (restored).md").read_text() == "old\n"
    (owner.work / "x.md").write_text("1\n")
    first = move(owner, ["x.md"])
    (owner.work / "x.md").write_text("2\n")
    (owner.work / "x (restored).md").write_text("3\n")
    again = owner.client.post(f"/api/trash/{first[0]['id']}/restore", headers=HEADERS).json()
    assert Path(again["restored_to"]).name == "x (restored 2).md"


def test_folders_move_whole_and_come_back_whole(owner):
    (owner.work / "d" / "sub").mkdir(parents=True)
    (owner.work / "d" / "a.txt").write_text("a")
    (owner.work / "d" / "sub" / "b.txt").write_text("bb")
    done = move(owner, ["d"])
    assert not (owner.work / "d").exists()
    item = owner.client.get("/api/trash", headers=HEADERS).json()["items"][0]
    assert item["is_dir"] is True and item["size_bytes"] == 3
    owner.client.post(f"/api/trash/{done[0]['id']}/restore", headers=HEADERS)
    assert (owner.work / "d" / "sub" / "b.txt").read_text() == "bb"


@pytest.mark.parametrize("bad", ["/etc/hostname", "../outside.txt", ".hokma", ".hokma/extensions.json", "missing.md", ""])
def test_only_files_of_the_working_folder_can_go_to_the_trash(owner, bad):
    (owner.work / ".hokma").mkdir()
    (owner.work / ".hokma" / "extensions.json").write_text("{}")
    (owner.tmp / "outside.txt").write_text("x")
    with pytest.raises(trash.TrashError):
        move(owner, [bad])
    assert (owner.work / ".hokma" / "extensions.json").exists() and (owner.tmp / "outside.txt").exists()


def test_the_data_folder_the_working_folder_itself_and_links_out_are_refused(owner):
    (owner.data / "database.json").write_text("{}")
    (owner.tmp / "secret.txt").write_text("s")
    (owner.work / "link").symlink_to(owner.tmp / "secret.txt")
    with pytest.raises(trash.TrashError):
        trash.move([str(owner.data / "database.json")], "x", db=owner.db, roots=[str(owner.work)],
                   protect=protect(), cwd=str(owner.work))
    with pytest.raises(trash.TrashError):
        move(owner, [str(owner.work)])
    done = move(owner, ["link"])                            # the link moves, never its target
    assert (owner.tmp / "secret.txt").read_text() == "s" and not os.path.lexists(owner.work / "link")
    assert done


def test_one_bad_path_moves_nothing(owner):
    (owner.work / "a.md").write_text("a")
    with pytest.raises(trash.TrashError):
        move(owner, ["a.md", "/etc/hostname"])
    assert (owner.work / "a.md").exists() and owner.client.get("/api/trash", headers=HEADERS).json()["count"] == 0


def test_emptying_and_deleting_for_good_are_the_owners_with_a_confirmation(owner):
    (owner.work / "a.md").write_text("a")
    (owner.work / "b.md").write_text("b")
    ids = [move(owner, ["a.md"])[0]["id"], move(owner, ["b.md"])[0]["id"]]
    c = owner.client
    for headers in ({}, {"X-Hokma-Token": "wrong"}):                         # no token, no way
        assert c.post("/api/trash/empty", json={"confirm": True}, headers=headers).status_code == 401
        assert c.post(f"/api/trash/{ids[0]}/purge", json={"confirm": True}, headers=headers).status_code == 401
        assert c.post(f"/api/trash/{ids[0]}/restore", headers=headers).status_code == 401
        assert c.get("/api/trash", headers=headers).status_code == 401
    assert c.post("/api/trash/empty", json={}, headers=HEADERS).json()["detail"]["code"] == "confirm_required"
    assert c.post("/api/trash/empty", json={"confirm": False}, headers=HEADERS).status_code == 400
    assert c.post(f"/api/trash/{ids[0]}/purge", json={}, headers=HEADERS).json()["detail"]["code"] == "confirm_required"
    assert c.get("/api/trash", headers=HEADERS).json()["count"] == 2                # still all there
    assert c.post(f"/api/trash/{ids[0]}/purge", json={"confirm": True}, headers=HEADERS).status_code == 200
    assert not (owner.data / "trash" / str(ids[0])).exists() and (owner.data / "trash" / str(ids[1])).exists()
    assert c.post("/api/trash/empty", json={"confirm": True}, headers=HEADERS).json() == {"count": 1}
    assert not any((owner.data / "trash").iterdir())
    assert c.post(f"/api/trash/{ids[0]}/restore", headers=HEADERS).status_code == 404       # gone for good
    with owner.db.read() as u:
        titles = [e.title for e in u.journal.list()]
        kinds = {e.actor_kind for e in u.journal.list()}
    assert any("Deleted for good" in t for t in titles) and any("emptied" in t for t in titles) and kinds == {"human"}


def test_the_trash_cannot_be_deleted_by_the_agent(owner):
    from hokma.kit import hardblock

    ctx = hardblock.Ctx(str(owner.data), [str(owner.work)], protect(), cwd=str(owner.work))

    def rule(tool, tool_input):
        block = hardblock.evaluate({"tool_name": tool, "tool_input": tool_input, "cwd": str(owner.work)}, ctx)
        return block.rule if block else None

    assert rule("Bash", {"command": f"rm -rf {owner.data}/trash"}) == "delete"
    assert rule("Bash", {"command": f"ls {owner.data}/trash"}) is None             # task 3169: Hokma's folders are readable


# ------------------------------------------------------------------------------------- the MCP server

def rpc(proc, message):
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


@pytest.fixture
def mcp(owner):
    args = [sys.executable, "-I", str(MCP_FILE), "--data", str(owner.data), "--workdir", str(owner.work)]
    for p in protect():
        args += ["--protect", p]
    with owner.db.write() as u:
        conversation = u.conversations.create(engine="claude_code", title="t", model="sonnet")
    env = {**os.environ, "HOKMA_LANG": "en", "HOKMA_CONVERSATION_ID": str(conversation.id),
           "HOKMA_WORK_ROOT": "${HOKMA_WORK_ROOT:-}"}
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env, cwd=owner.work)
    assert rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})["result"]["serverInfo"]["name"] == "hokma"
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.conversation_id = conversation.id
    yield proc
    proc.stdin.close()
    proc.wait(timeout=10)


def call(mcp, paths, reason="done with it"):
    return rpc(mcp, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                     "params": {"name": "trash_move", "arguments": {"paths": paths, "reason": reason}}})["result"]


def test_p7_the_agent_asks_to_delete_and_uses_trash_move(owner, mcp):
    """'delete notas.md': the delete is refused by the hook, trash_move works, the owner restores it identical."""
    from hokma.kit import hardblock

    (owner.work / "notas.md").write_text("mis notas\n")
    before = sha(owner.work / "notas.md")
    ctx = hardblock.Ctx(str(owner.data), [str(owner.work)], protect(), cwd=str(owner.work))
    refused = hardblock.evaluate({"tool_name": "Bash", "tool_input": {"command": "rm notas.md"}, "cwd": str(owner.work)}, ctx)
    assert refused.rule == "delete" and (owner.work / "notas.md").exists()

    listed = rpc(mcp, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = [t["name"] for t in listed]
    assert names[0] == "trash_move"
    assert not [n for n in names if "restore" in n or "empty" in n or n.startswith("trash_") and n != "trash_move"]
    result = call(mcp, ["notas.md"])
    assert result["isError"] is False and "Settings > Trash" in result["content"][0]["text"]
    assert not (owner.work / "notas.md").exists()
    with owner.db.read() as u:
        row = u.trash.list()[0]
    assert row.conversation_id == mcp.conversation_id and row.reason == "done with it" and row.original_path == str(owner.work / "notas.md")

    res = owner.client.post(f"/api/trash/{row.id}/restore", headers=HEADERS)
    assert res.status_code == 200 and sha(owner.work / "notas.md") == before

    assert owner.client.post("/api/trash/empty", json={}, headers=HEADERS).status_code == 400   # needs the owner's confirm


def test_the_server_refuses_what_is_not_the_working_folder(owner, mcp):
    (owner.tmp / "elsewhere.txt").write_text("x")
    (owner.work / ".hokma").mkdir()
    for bad in (str(owner.tmp / "elsewhere.txt"), ".hokma", str(owner.data), "../elsewhere.txt"):
        out = call(mcp, [bad])
        assert out["isError"] is True, bad
    assert (owner.tmp / "elsewhere.txt").exists() and (owner.work / ".hokma").exists() and owner.data.exists()
    assert call(mcp, ["x"], reason="  ")["isError"] is True
    unknown = rpc(mcp, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "empty_trash", "arguments": {}}})
    assert "error" in unknown
    assert "error" in rpc(mcp, {"jsonrpc": "2.0", "id": 6, "method": "nope"})


def test_the_launch_wires_the_server_and_pre_approves_only_trash_move(owner):
    from hokma.kit.launch import HOKMA_SERVER, prepare_claude_launch
    from hokma.kit.settings import build_settings

    prepare_claude_launch()
    config = json.loads((owner.data / "claude-config" / "hokma-mcp.json").read_text())
    server = config["mcpServers"][HOKMA_SERVER]
    assert server["command"] == sys.executable and server["args"][:2] == ["-I", str(MCP_FILE)]
    assert build_settings()["permissions"]["allow"] == ["mcp__hokma__trash_move"]


# ------------------------------------------------------------------------- bug 1034: move and restore agree

@pytest.mark.parametrize("bad", [".git", ".claude", "sub/.git", "sub/.HOKMA", ".Git/config", "a/.claude/x.md"])
def test_move_refuses_the_repository_and_settings_folders(owner, bad):
    target = owner.work / bad
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(exist_ok=True)
    with pytest.raises(trash.TrashError) as err:
        move(owner, [bad])
    assert err.value.code == "protected"
    assert "protects repository and settings folders" in err.value.message
    assert target.exists()


def test_move_refuses_a_link_that_resolves_into_a_protected_folder(owner):
    (owner.work / ".git").mkdir()
    (owner.work / "inner.txt").write_text("x\n")
    (owner.work / "via").symlink_to(owner.work / ".git")
    (owner.work / ".git" / "config").write_text("c\n")
    with pytest.raises(trash.TrashError) as err:
        move(owner, ["via/config"])
    assert err.value.code == "protected"


def test_a_normal_file_still_moves_and_restores(owner):
    (owner.work / "ok.txt").write_text("fine\n")
    done = move(owner, ["ok.txt"])
    res = owner.client.post(f"/api/trash/{done[0]['id']}/restore", headers=HEADERS)
    assert res.status_code == 200 and (owner.work / "ok.txt").read_text() == "fine\n"


def test_whatever_move_accepts_restore_accepts(owner):
    names = ["a.txt", "dir/b.txt", "dir/deep/c.md", ".gitignore", ".github/ci.yml", ".claudeish", "my.git/x",
             ".git", ".claude", "sub/.git", ".hokma", "x/.CLAUDE/y"]
    for n in names:
        p = owner.work / n
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("data\n")
    for n in names:
        if not (owner.work / n).exists():
            continue
        try:
            done = move(owner, [n])
        except trash.TrashError:
            continue
        res = owner.client.post(f"/api/trash/{done[0]['id']}/restore", headers=HEADERS)
        assert res.status_code == 200, f"{n} moved but cannot be restored: {res.text}"
