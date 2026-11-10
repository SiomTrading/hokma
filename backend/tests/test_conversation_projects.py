"""Where a conversation runs (task 3083, B-02 to B-10 and the General switch).

Real SQLite repositories, the real projects service and the chat engine with the fake `claude`. Several projects:
Alpha, Beta and the main one, switching the active project and "General" (no active project) and checking where new
conversations, old conversations and the terminal land.
"""
import os

import pytest
from engine_support import connect, env, hard_timeout, posix_only, turn  # noqa: F401
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hardblock_support import box  # noqa: F401  (fixture)

from hokma.db import migrate, open_database
from hokma.engine.api import create_router
from hokma.engine.conversations_api import create_router as create_conversations_router
from hokma.engine.dbstore import DbAgents, DbChatStore, DbProjects
from hokma.engine.engine import ChatEngine
from hokma.projects.api import router as projects_router
from hokma.rpc.ws import SESSION_TOKEN
from hokma.terminal.session import resolve_cwd

pytestmark = posix_only
H = {"X-Hokma-Token": SESSION_TOKEN}


class World:
    def __init__(self, env):
        self.env = env
        migrate(env.data / "hokma.db")
        self.db = open_database(env.data / "hokma.db")
        with self.db.write() as u:
            u.profile.save(owner_name="Ana", fund_name="Main")
        self.engine = ChatEngine(store=DbChatStore(self.db), projects=DbProjects(self.db), agents=DbAgents(self.db),
                                 launch=lambda: env.launch_spec, session_token=SESSION_TOKEN, stop_grace=0.5)
        app = FastAPI()
        app.include_router(create_router(lambda: self.engine))
        app.include_router(create_conversations_router(lambda: self.db, lambda: self.engine))
        app.include_router(projects_router)
        self.client = TestClient(app)
        self.client.__enter__()
        first = self.client.get("/api/projects", headers=H).json()
        self.main = str(first["projects"][0]["id"])
        self.alpha = self.create("Alpha")
        self.beta = self.create("Beta")

    def create(self, name):
        return str(self.client.post("/api/projects", headers=H, json={"name": name, "kind": "strategy"}).json()["id"])

    def activate(self, project_id):
        self.client.post(f"/api/projects/{project_id}/activate", headers=H)

    def general(self):
        assert self.client.post("/api/projects/deactivate", headers=H).status_code == 200

    def folder(self, project_id):
        return str(self.env.home / "projects" / {self.main: "principal", self.alpha: "alpha", self.beta: "beta"}[project_id])

    def say(self, text="hola", conversation_id=None, **fields):
        with connect(self.client) as ws:
            events = turn(ws, text, **({"conversation_id": conversation_id} if conversation_id else {}), **fields)
        conv = [e for e in events if e["type"] == "conversation"][-1]
        return conv, os.path.realpath(self.env.entries("start")[-1]["cwd"])

    def row(self, conversation_id):
        with self.db.read() as u:
            return u.session.one("SELECT project_id, project_mode FROM conversations WHERE id = :i", {"i": int(conversation_id)})

    def active(self):
        return self.client.get("/api/projects", headers=H).json()["active_id"]


@pytest.fixture
def world(env):
    w = World(env)
    yield w
    w.client.__exit__(None, None, None)


def real(path):
    return os.path.realpath(path)


def test_b02_b04_a_new_conversation_is_general_even_with_an_active_project(world):
    world.activate(world.alpha)
    for fields in ({}, {"project_id": None}):
        conv, cwd = world.say(**fields)
        assert conv["project"] is None and conv["project_mode"] == "fixed" and conv["created"] is True
        assert cwd == real(world.env.home)
        assert world.row(conv["conversation_id"]) == {"project_id": None, "project_mode": "fixed"}


def test_b03_a_new_conversation_names_a_project_that_is_not_the_active_one(world):
    world.activate(world.alpha)
    conv, cwd = world.say(project_id=world.beta)
    assert conv["project"] == {"id": world.beta, "name": "Beta"} and conv["project_mode"] == "fixed"
    assert cwd == real(world.folder(world.beta))
    assert world.row(conv["conversation_id"]) == {"project_id": int(world.beta), "project_mode": "fixed"}


def test_b05_the_working_folder_of_a_general_conversation_is_writable_and_nothing_outside(box):
    assert box.hit("Write", file_path=str(box.work / "nota-general.md"), content="x") is None
    assert box.hit("Write", file_path="/tmp/outside-the-folder.txt", content="x") == "outside_workdir"


def test_b06_an_old_conversation_follows_the_active_project(world):
    """A conversation made by 0.0.6 (project_mode follow, no project) keeps tracking the active project."""
    with world.db.write() as u:
        old = u.conversations.create(engine="claude_code", title="old", model="sonnet", project_mode="follow")
    world.activate(world.alpha)
    conv, cwd = world.say("sigue", conversation_id=str(old.id))
    assert conv["project"]["name"] == "Alpha" and conv["project_mode"] == "follow" and cwd == real(world.folder(world.alpha))
    world.activate(world.beta)
    conv, cwd = world.say("sigue", conversation_id=str(old.id))
    assert conv["project"]["name"] == "Beta" and cwd == real(world.folder(world.beta))


def test_b07_project_command_freezes_an_old_conversation(world):
    with world.db.write() as u:
        old = u.conversations.create(engine="claude_code", title="old", model="sonnet", project_mode="follow")
    world.activate(world.main)
    with connect(world.client) as ws:
        ws.send_json({"type": "command", "conversation_id": str(old.id), "name": "project", "arg": "Alpha"})
        while ws.receive_json()["type"] != "command_result":
            pass
    assert world.row(old.id) == {"project_id": int(world.alpha), "project_mode": "fixed"}
    world.activate(world.beta)
    conv, cwd = world.say("sigue", conversation_id=str(old.id))
    assert conv["project"]["name"] == "Alpha" and cwd == real(world.folder(world.alpha))


def test_b08_deleting_the_project_of_a_fixed_conversation_leaves_it_general(world):
    world.activate(world.main)
    conv, _ = world.say(project_id=world.beta)
    with world.db.write() as u:
        u.session.execute("DELETE FROM projects WHERE id = :i", {"i": int(world.beta)})
    again, cwd = world.say("y ahora", conversation_id=conv["conversation_id"])
    assert again["project"] is None and cwd == real(world.env.home)           # not the active project (main)


def test_b09_client_ref_comes_back_on_the_creation_event_and_on_an_error(world):
    conv, _ = world.say(client_ref="abc_123")
    assert conv["client_ref"] == "abc_123"
    with connect(world.client) as ws:
        events = turn(ws, "x", client_ref="abc_123", model="gpt")
    assert events[-1]["type"] == "error" and events[-1]["code"] == "unknown_model" and events[-1]["client_ref"] == "abc_123"
    for bad in ("has space", "x" * 65, "", 7):
        conv, _ = world.say(client_ref=bad)
        assert "client_ref" not in conv                                         # ignored, not an error
    with connect(world.client) as ws:                                           # not for an existing conversation
        events = turn(ws, "again", conversation_id=conv["conversation_id"], client_ref="abc_123")
    assert "client_ref" not in [e for e in events if e["type"] == "conversation"][-1]


def test_b10_lists_carry_the_kind_and_the_project_mode(world):
    options = world.client.get("/api/chat/options", headers=H).json()
    assert {p["name"]: p["kind"] for p in options["projects"]}["Alpha"] == "strategy"
    conv, _ = world.say()
    listed = world.client.get("/api/conversations", headers=H).json()["conversations"]
    assert [c["project_mode"] for c in listed if c["id"] == conv["conversation_id"]] == ["fixed"]


def test_switching_between_two_projects_and_general_moves_old_chats_and_the_terminal_but_never_new_chats(world):
    """The whole path of the panel: Alpha, Beta, General. Old chats follow the active project, new ones are General."""
    with world.db.write() as u:
        old = u.conversations.create(engine="claude_code", title="old", model="sonnet", project_mode="follow")
    root = real(world.env.home)

    world.activate(world.alpha)
    assert world.active() == int(world.alpha)
    assert real(resolve_cwd(None)) == real(world.folder(world.alpha))           # the terminal opens in the active project
    assert world.say("a", conversation_id=str(old.id))[1] == real(world.folder(world.alpha))
    new_in_alpha, cwd = world.say("n1")
    assert new_in_alpha["project"] is None and cwd == root                      # new chat: General, not Alpha

    world.activate(world.beta)
    assert real(resolve_cwd(None)) == real(world.folder(world.beta))
    assert world.say("b", conversation_id=str(old.id))[1] == real(world.folder(world.beta))
    assert world.say("n2")[1] == root
    assert world.say("n3", project_id=world.alpha)[1] == real(world.folder(world.alpha))   # picking one in the selector

    world.general()                                                              # General: no active project at all
    assert world.active() is None
    listed = world.client.get("/api/projects", headers=H).json()
    assert not any(p["active"] for p in listed["projects"])
    assert real(resolve_cwd(None)) == root                                       # the terminal opens in the root
    conv, cwd = world.say("c", conversation_id=str(old.id))
    assert conv["project_mode"] == "follow" and cwd == root                      # the old chat came back to the root with it
    assert world.say("n4")[1] == root
    # the conversations that were fixed to a project did not move
    assert world.say("d", conversation_id=new_in_alpha["conversation_id"])[1] == root
    with_beta, _ = world.say("e", project_id=world.beta)
    assert world.say("f", conversation_id=with_beta["conversation_id"])[1] == real(world.folder(world.beta))

    world.activate(world.alpha)                                                  # and a project can be picked again
    assert world.say("g", conversation_id=str(old.id))[1] == real(world.folder(world.alpha))
    assert real(resolve_cwd(None)) == real(world.folder(world.alpha))
    assert world.say("n5")[1] == root


def test_general_is_stored_in_the_profile_and_cleared_by_choosing_a_project(world):
    world.general()
    with world.db.read() as u:
        profile = u.profile.get()
    assert profile.no_active_project is True and profile.active_project_id is None
    world.activate(world.beta)
    with world.db.read() as u:
        profile = u.profile.get()
    assert profile.no_active_project is False and profile.active_project_id == int(world.beta)
