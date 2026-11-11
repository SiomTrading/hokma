"""Task 3156: adding a repository by its address installs or lists its plugins, the installed list says what really
loads, one repository is one marketplace, and a marketplace can be removed."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from dbsupport import db, env  # noqa: F401  (fixtures)
from hokma.core.app import create_app
from hokma.kit import extensions, plugin_state
from hokma.kit.settings import build_settings
from hokma.rpc.ws import SESSION_TOKEN
from hokma.status import plugin_sources

REPOS: dict[str, dict] = {}


def fake_fetch(url: str):
    """Serves REPOS[repo][file] for raw.githubusercontent.com/<repo>/HEAD/.claude-plugin/<file>."""
    rest = url.split("raw.githubusercontent.com/")[1]
    repo = "/".join(rest.split("/")[:2])
    return REPOS.get(repo, {}).get(rest.rsplit("/", 1)[1])


@pytest.fixture
def client(db, monkeypatch):  # noqa: F811
    os.makedirs(os.environ["HOKMA_HOME"], exist_ok=True)
    REPOS.clear()
    monkeypatch.setattr(plugin_sources, "fetch_json", fake_fetch)
    return TestClient(create_app(), headers={"X-Hokma-Token": SESSION_TOKEN})


def _config(env):
    return env / "data" / "claude-config"


def test_marketplace_with_one_plugin_installs_it_right_away(client):
    REPOS["thedotmack/claude-mem"] = {"marketplace.json": {"name": "thedotmack", "plugins": [{"name": "claude-mem", "description": "memory"}]}}
    done = client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    assert done.status_code == 201
    body = done.json()
    assert body["name"] == "thedotmack" and body["installed"] == ["claude-mem@thedotmack"]
    data, _ = extensions.load()
    assert data["enabledPlugins"] == {"claude-mem@thedotmack": True}
    listed = client.get("/api/extensions").json()
    assert [(p["id"], p["enabled"]) for p in listed["plugins"]] == [("claude-mem@thedotmack", True)]
    assert listed["plugins"][0]["state"] == "active"           # Claude Code installed it and installed_plugins.json says so (task 3161)


def test_a_plugin_added_by_url_is_on_and_can_be_uninstalled_from_the_catalog(client):
    REPOS["thedotmack/claude-mem"] = {"marketplace.json": {"name": "thedotmack", "plugins": [{"name": "claude-mem"}]}}
    client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    offered = client.get("/api/extensions/marketplaces/thedotmack/plugins").json()["plugins"]
    assert [(p["id"], p["installed"], p["enabled"]) for p in offered] == [("claude-mem@thedotmack", True, True)]
    gone = client.post("/api/extensions/uninstall", json={"id": "claude-mem@thedotmack", "confirmed": True})
    assert gone.status_code == 200 and gone.json()["uninstalled"] is True
    assert extensions.load()[0]["enabledPlugins"] == {}
    assert [p["installed"] for p in client.get("/api/extensions/marketplaces/thedotmack/plugins").json()["plugins"]] == [False]


def test_marketplace_with_several_plugins_lists_them_without_installing(client):
    REPOS["acme/tools"] = {"marketplace.json": {"name": "acme-tools", "plugins": [{"name": "a", "description": "A"}, {"name": "b"}]}}
    body = client.post("/api/extensions/marketplaces", json={"repo": "acme/tools"}).json()
    assert body["installed"] == [] and [p["id"] for p in body["plugins"]] == ["a@acme-tools", "b@acme-tools"]
    assert extensions.load()[0]["enabledPlugins"] == {}
    # the same list is available later for a marketplace already registered
    later = client.get("/api/extensions/marketplaces/acme-tools/plugins").json()
    assert [p["installed"] for p in later["plugins"]] == [False, False]
    on = client.post("/api/extensions/toggle", json={"id": "b@acme-tools", "enabled": True, "confirmed": True})
    assert on.status_code == 200
    assert [p["installed"] for p in client.get("/api/extensions/marketplaces/acme-tools/plugins").json()["plugins"]] == [False, True]


def test_a_repository_that_is_a_plugin_is_installed_through_a_one_plugin_marketplace(client, env, monkeypatch):
    # the test data folder lives in the temp folder, which the agent can write; the real one does not
    monkeypatch.setattr("hokma.kit.writable.temp_dirs", lambda: [])
    REPOS["bradautomates/claude-video"] = {"plugin.json": {"name": "claude-video", "description": "video"}}
    body = client.post("/api/extensions/marketplaces", json={"repo": "bradautomates/claude-video"}).json()
    assert body["kind"] == "plugin" and body["installed"] == ["claude-video@claude-video"]
    data, _ = extensions.load()
    source = data["extraKnownMarketplaces"]["claude-video"]["source"]
    assert source["source"] == "directory" and source["path"].startswith(str(env / "data"))
    shim = json.loads(open(os.path.join(source["path"], ".claude-plugin", "marketplace.json")).read())
    assert shim["plugins"][0]["source"] == {"source": "github", "repo": "bradautomates/claude-video"}
    shown = client.get("/api/extensions").json()["marketplaces"]
    assert shown[0]["source"] == "github:bradautomates/claude-video" and shown[0]["plugins"] == 1
    # adding it again changes nothing
    client.post("/api/extensions/marketplaces", json={"repo": "bradautomates/claude-video"})
    assert list(extensions.load()[0]["extraKnownMarketplaces"]) == ["claude-video"]


def test_a_repository_that_is_neither_is_refused_and_nothing_is_saved(client):
    REPOS["x/y"] = {}
    answer = client.post("/api/extensions/marketplaces", json={"repo": "x/y"})
    assert answer.status_code == 422 and "marketplace.json" in answer.json()["detail"]
    assert extensions.load()[0]["extraKnownMarketplaces"] == {}


def test_unreachable_github_is_a_clear_error(client, monkeypatch):
    def boom(url):
        raise plugin_sources.SourceError(502, "no connection")
    monkeypatch.setattr(plugin_sources, "fetch_json", boom)
    answer = client.post("/api/extensions/marketplaces", json={"repo": "x/y"})
    assert answer.status_code == 502 and answer.json()["detail"] == "no connection"


def test_the_repo_written_two_ways_is_one_marketplace(env):
    assert extensions.source_key({"source": "git", "url": "https://github.com/TheDotMack/Claude-Mem.git"}) == \
        extensions.source_key({"source": "github", "repo": "thedotmack/claude-mem"})
    extensions.add_marketplace("claude-mem", {"source": "github", "repo": "thedotmack/claude-mem"})
    extensions.add_marketplace("other", {"source": "git", "url": "https://github.com/thedotmack/claude-mem"})
    assert list(extensions.load()[0]["extraKnownMarketplaces"]) == ["claude-mem"]


def test_existing_duplicates_are_migrated_keeping_the_name_the_plugins_use(env):
    os.makedirs(os.environ["HOKMA_HOME"], exist_ok=True)
    path = extensions.extensions_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "enabledPlugins": {"claude-mem@thedotmack": True, "caveman@caveman": False},
        "extraKnownMarketplaces": {
            "claude-mem": {"source": {"source": "github", "repo": "thedotmack/claude-mem"}},
            "thedotmack": {"source": {"source": "github", "repo": "thedotmack/claude-mem"}},
            "caveman": {"source": {"source": "github", "repo": "JuliusBrussee/caveman"}},
        }}))
    data, _ = extensions.load()
    assert sorted(data["extraKnownMarketplaces"]) == ["caveman", "thedotmack"]
    assert data["enabledPlugins"] == {"claude-mem@thedotmack": True, "caveman@caveman": False}
    assert extensions.migrate() == []
    assert sorted(json.loads(path.read_text())["extraKnownMarketplaces"]) == ["caveman", "thedotmack"]


def test_unreferenced_duplicate_keeps_the_name_that_is_not_the_folder_guess(env):
    path = extensions.extensions_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"extraKnownMarketplaces": {
        "claude-mem": {"source": {"source": "github", "repo": "thedotmack/claude-mem"}},
        "thedotmack": {"source": {"source": "github", "repo": "thedotmack/claude-mem"}}}}))
    assert list(extensions.load()[0]["extraKnownMarketplaces"]) == ["thedotmack"]


def test_claude_live_twin_does_not_come_back_as_a_second_entry(env):
    extensions.add_marketplace("claude-mem", {"source": "github", "repo": "thedotmack/claude-mem"})
    live = _config(env)
    (live / "plugins").mkdir(parents=True)
    (live / "settings.json").write_text(json.dumps({"enabledPlugins": {"claude-mem@thedotmack": True}}))
    (live / "plugins" / "known_marketplaces.json").write_text(json.dumps(
        {"thedotmack": {"source": {"source": "github", "repo": "thedotmack/claude-mem"}, "installLocation": "/x"}}))
    settings = build_settings()
    assert list(settings["extraKnownMarketplaces"]) == ["thedotmack"] and settings["enabledPlugins"] == {"claude-mem@thedotmack": True}
    extensions.absorb_live()
    assert list(extensions.load()[0]["extraKnownMarketplaces"]) == ["thedotmack"]


def test_existing_entry_with_a_guessed_name_takes_the_real_one_on_add(client):
    extensions.add_marketplace("claude-mem", {"source": "github", "repo": "thedotmack/claude-mem"})
    REPOS["thedotmack/claude-mem"] = {"marketplace.json": {"name": "thedotmack", "plugins": [{"name": "claude-mem"}]}}
    client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    data, _ = extensions.load()
    assert list(data["extraKnownMarketplaces"]) == ["thedotmack"] and data["enabledPlugins"] == {"claude-mem@thedotmack": True}


def test_remove_marketplace_drops_its_plugins_and_does_not_come_back(client, env):
    REPOS["a/b"] = {"marketplace.json": {"name": "bee", "plugins": [{"name": "one"}]}}
    client.post("/api/extensions/marketplaces", json={"repo": "a/b"})
    live = _config(env)
    (live / "plugins").mkdir(parents=True, exist_ok=True)
    (live / "plugins" / "known_marketplaces.json").write_text(json.dumps({"bee": {"source": {"source": "github", "repo": "a/b"}}}))
    (live / "settings.json").write_text(json.dumps({"enabledPlugins": {"one@bee": True}, "extraKnownMarketplaces": {"bee": {"source": {"source": "github", "repo": "a/b"}}}}))
    done = client.delete("/api/extensions/marketplaces/bee")
    assert done.status_code == 200 and done.json()["plugins_removed"] == ["one@bee"]
    extensions.absorb_live()
    data, _ = extensions.load()
    assert data["extraKnownMarketplaces"] == {} and data["enabledPlugins"] == {}
    assert client.delete("/api/extensions/marketplaces/bee").status_code == 404


def test_state_comes_from_what_the_engine_loaded(env):
    extensions.add_marketplace("m", {"source": "github", "repo": "o/m"})
    for pid, on in (("ok@m", True), ("off@m", False), ("wait@m", True), ("gone@m", True), ("bad@m", True), ("orphan@zzz", True)):
        extensions.set_plugin(pid, on)
    plugins = _config(env) / "plugins"
    (plugins / "cache" / "ok").mkdir(parents=True)
    (plugins / "cache" / "bad" / ".claude-plugin").mkdir(parents=True)
    (plugins / "cache" / "bad" / ".claude-plugin" / "plugin.json").write_text("{not json")
    (plugins / "cache" / "off").mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        "ok@m": [{"scope": "user", "installPath": str(plugins / "cache" / "ok")}],
        "off@m": [{"installPath": str(plugins / "cache" / "off")}],
        "gone@m": [{"installPath": str(plugins / "cache" / "missing")}],
        "bad@m": [{"installPath": str(plugins / "cache" / "bad")}]}}))
    data, _ = extensions.load()
    installs = plugin_state.engine_installs()
    state = {pid: plugin_state.state_of(pid, on, data, installs) for pid, on in data["enabledPlugins"].items()}
    assert {k: v["state"] for k, v in state.items()} == {
        "ok@m": "active", "off@m": "disabled", "wait@m": "pending", "gone@m": "error", "bad@m": "error", "orphan@zzz": "error"}
    assert state["gone@m"]["reason"] and state["bad@m"]["reason"] and "zzz" in state["orphan@zzz"]["reason"]
    assert state["ok@m"]["reason"] == ""


def test_state_in_version_one_format(env):
    extensions.add_marketplace("m", {"source": "github", "repo": "o/m"})
    extensions.set_plugin("p@m", True)
    plugins = _config(env) / "plugins"
    (plugins / "p").mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(json.dumps({"version": 1, "plugins": {"p@m": {"installPath": str(plugins / "p")}}}))
    data, _ = extensions.load()
    assert plugin_state.state_of("p@m", True, data, plugin_state.engine_installs())["state"] == "active"


def test_a_manifest_cannot_take_the_name_of_another_repository_or_the_official_one(client):
    extensions.add_marketplace("acme", {"source": "github", "repo": "acme/real"})
    REPOS["evil/fake"] = {"marketplace.json": {"name": "acme", "plugins": [{"name": "x"}]}}
    answer = client.post("/api/extensions/marketplaces", json={"repo": "evil/fake"})
    assert answer.status_code == 409
    REPOS["evil/official"] = {"marketplace.json": {"name": "claude-plugins-official", "plugins": [{"name": "x"}]}}
    assert client.post("/api/extensions/marketplaces", json={"repo": "evil/official"}).status_code == 409
    data, _ = extensions.load()
    assert list(data["extraKnownMarketplaces"]) == ["acme"] and data["extraKnownMarketplaces"]["acme"]["source"]["repo"] == "acme/real"
    assert data["enabledPlugins"] == {}


def test_a_different_ref_is_a_different_marketplace_and_shims_never_share_a_folder(env, monkeypatch):
    a = {"source": "github", "repo": "o/r"}
    assert extensions.source_key(a) != extensions.source_key({**a, "ref": "dev"})
    extensions.add_marketplace("main", a)
    extensions.add_marketplace("dev", {**a, "ref": "dev"})
    assert sorted(extensions.load()[0]["extraKnownMarketplaces"]) == ["dev", "main"]
    # "a/b-c" and "a-b/c" used to flatten to the same folder name
    assert plugin_sources.write_shim("a/b-c", "p") != plugin_sources.write_shim("a-b/c", "p")
