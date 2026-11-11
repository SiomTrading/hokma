"""Task 3161: installing a plugin is Claude Code downloading it, not a line in a list. The CLI runs with Hokma's
config folder and filtered environment, the result is confirmed in installed_plugins.json, a failure keeps its real
reason with a Retry, what 0.0.19 left without files is installed once at start, other engines never read Active and
claude-mem without its worker is reported instead of Active."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from dbsupport import db, env, fake_claude  # noqa: F401  (fixtures)
from hokma.core.app import create_app
from hokma.kit import extensions
from hokma.rpc.ws import SESSION_TOKEN
from hokma.status import plugin_install, plugin_sources

REPOS: dict[str, dict] = {}


def fake_fetch(url: str):
    rest = url.split("raw.githubusercontent.com/")[1]
    repo = "/".join(rest.split("/")[:2])
    return REPOS.get(repo, {}).get(rest.rsplit("/", 1)[1])


@pytest.fixture
def client(db, fake_claude, monkeypatch):  # noqa: F811
    os.makedirs(os.environ["HOKMA_HOME"], exist_ok=True)
    REPOS.clear()
    REPOS["thedotmack/claude-mem"] = {"marketplace.json": {"name": "thedotmack", "plugins": [{"name": "claude-mem"}]}}
    monkeypatch.setattr(plugin_sources, "fetch_json", fake_fetch)
    return TestClient(create_app(), headers={"X-Hokma-Token": SESSION_TOKEN})


def _card(client, plugin_id="claude-mem@thedotmack"):
    return next(p for p in client.get("/api/extensions").json()["plugins"] if p["id"] == plugin_id)


def test_install_runs_marketplace_add_then_install_and_the_card_is_active(client, fake_claude):
    done = client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    assert done.status_code == 201 and done.json()["installed"] == ["claude-mem@thedotmack"]
    assert fake_claude.calls == [["plugin", "marketplace", "add", "--json", "thedotmack/claude-mem"],
                                 ["plugin", "install", "--json", "claude-mem@thedotmack"]]
    card = _card(client)
    assert card["state"] == "active" and card["retry"] is False      # confirmed in installed_plugins.json


def test_a_known_marketplace_is_not_added_again(client, fake_claude):
    client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    client.post("/api/extensions/uninstall", json={"id": "claude-mem@thedotmack", "confirmed": True})
    fake_claude.calls.clear()
    assert client.post("/api/extensions/toggle", json={"id": "claude-mem@thedotmack", "enabled": True, "confirmed": True}).status_code == 200
    assert fake_claude.calls == [["plugin", "install", "--json", "claude-mem@thedotmack"]]


def test_a_failed_install_keeps_the_real_reason_and_retry_installs(client, fake_claude):
    fake_claude.fail["claude-mem@thedotmack"] = "git clone failed: no network"
    done = client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    assert done.status_code == 201
    assert any("git clone failed: no network" in w for w in done.json()["warnings"])
    card = _card(client)
    assert card["state"] == "error" and "git clone failed: no network" in card["reason"] and card["retry"] is True
    # still an error after the page is opened again: the reason is stored, not guessed
    assert "no network" in _card(client)["reason"]
    failed = client.post("/api/extensions/retry", json={"id": "claude-mem@thedotmack"})
    assert failed.status_code == 502 and "no network" in failed.json()["detail"]
    fake_claude.fail.clear()
    assert client.post("/api/extensions/retry", json={"id": "claude-mem@thedotmack"}).status_code == 200
    assert _card(client)["state"] == "active"


def test_success_without_the_file_is_an_error(client, fake_claude):
    fake_claude.silent = True
    client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    card = _card(client)
    assert card["state"] == "error" and "not recorded" in card["reason"]


def test_a_timeout_is_an_error_with_a_retry(client, fake_claude):
    fake_claude.timeout = True
    client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    card = _card(client)
    assert card["state"] == "error" and card["retry"] is True and "timeout" in card["reason"]


def test_uninstall_runs_claude_plugin_uninstall(client, fake_claude):
    client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    fake_claude.calls.clear()
    assert client.post("/api/extensions/uninstall", json={"id": "claude-mem@thedotmack", "confirmed": True}).status_code == 200
    assert fake_claude.calls == [["plugin", "uninstall", "--json", "claude-mem@thedotmack"]]
    assert plugin_install.installed_folder("claude-mem@thedotmack") is None


def test_removing_a_marketplace_uninstalls_its_plugins(client, fake_claude):
    client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    fake_claude.calls.clear()
    assert client.delete("/api/extensions/marketplaces/thedotmack").status_code == 200
    assert ["plugin", "uninstall", "--json", "claude-mem@thedotmack"] in fake_claude.calls
    assert ["plugin", "marketplace", "remove", "thedotmack"] in fake_claude.calls


def test_other_engines_never_read_active(client, db):  # noqa: F811
    client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    with db.write() as unit:
        conn = unit.engine_connections.add("codex", "Codex", "subscription")
        unit.engine_connections.set_default(conn.id)
    card = _card(client)
    assert card["state"] == "claude_only" and "Claude Code" in card["reason"]


def test_claude_mem_without_its_worker_is_reported_not_active(client, monkeypatch):
    client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    monkeypatch.setattr(plugin_install, "worker_alive", lambda port: False)
    card = _card(client)
    assert card["state"] == "attention" and "worker" in card["reason"] and "37777" in card["reason"]
    monkeypatch.setattr(plugin_install, "worker_alive", lambda port: True)
    assert _card(client)["state"] == "active"


def test_claude_mem_without_hooks_is_reported(client):
    client.post("/api/extensions/marketplaces", json={"repo": "thedotmack/claude-mem"})
    (plugin_install.installed_folder("claude-mem@thedotmack") / "hooks" / "hooks.json").unlink()
    card = _card(client)
    assert card["state"] == "attention" and "hooks" in card["reason"]


def _approved(db, name, marketplace="thedotmack"):  # noqa: F811
    """What 0.0.19 left behind for a dashboard install: an applied, human-approved request."""
    from hokma.db import Actor

    with db.write() as unit:
        row = unit.extensions.add(name, marketplace, "plugin")
        req = unit.extension_requests.create("install", "plugin", name, marketplace, "dashboard", actor=Actor.human("owner"))
        unit.extension_requests.approve(req.id, decided_by="owner")
        unit.extension_requests.mark_applied(req.id, row.id)


def test_migration_installs_once_what_was_only_in_the_list(db, fake_claude):  # noqa: F811
    extensions.add_marketplace("thedotmack", {"source": "github", "repo": "thedotmack/claude-mem"})
    extensions.set_plugin("claude-mem@thedotmack", True)
    _approved(db, "claude-mem")
    extensions.set_plugin("off@thedotmack", False)
    assert plugin_install.pending_migration() == ["claude-mem@thedotmack"]
    assert plugin_install.migrate() == {"claude-mem@thedotmack": "ok"}
    assert plugin_install.installed_folder("claude-mem@thedotmack") is not None
    fake_claude.calls.clear()
    extensions.set_plugin("later@thedotmack", True)
    assert plugin_install.migrate() == {} and fake_claude.calls == []          # once
    state = json.loads((plugin_install._state_path()).read_text(encoding="utf-8"))
    assert state["migrated"] == "0.0.20"


def test_migration_failure_is_recorded_and_not_repeated(db, fake_claude):  # noqa: F811
    extensions.add_marketplace("thedotmack", {"source": "github", "repo": "thedotmack/claude-mem"})
    extensions.set_plugin("claude-mem@thedotmack", True)
    _approved(db, "claude-mem")
    fake_claude.fail["claude-mem@thedotmack"] = "403 from github"
    assert "403 from github" in plugin_install.migrate()["claude-mem@thedotmack"]
    assert plugin_install.failures()["claude-mem@thedotmack"] == "403 from github"
    assert plugin_install.pending_migration() == []


def test_migration_never_installs_what_the_owner_did_not_approve(db, fake_claude):  # noqa: F811
    """An entry absorbed from Claude's live settings.json has no approved request: it is not downloaded."""
    extensions.add_marketplace("thedotmack", {"source": "github", "repo": "thedotmack/claude-mem"})
    extensions.set_plugin("sneaky@thedotmack", True)
    assert plugin_install.pending_migration() == []
    assert plugin_install.migrate() == {} and fake_claude.calls == []


@pytest.mark.parametrize("plugin_id", ["-rf@x", "a b@x", "a@x;y", "a", "a@-x"])
def test_plugin_ids_are_validated_before_anything_runs(db, fake_claude, plugin_id):  # noqa: F811
    with pytest.raises(plugin_install.InstallError):
        plugin_install.install(plugin_id)
    assert fake_claude.calls == []


@pytest.mark.parametrize("source", [{"source": "github", "repo": "-x/y"}, {"source": "git", "url": "--upload-pack=x"},
                                    {"source": "directory", "path": "/etc"}, {"source": "file", "path": "/x"}])
def test_sources_are_validated(db, source):  # noqa: F811
    with pytest.raises(plugin_install.InstallError):
        plugin_install.source_argument(source)


def test_the_real_runner_uses_no_shell_the_filtered_env_and_hokmas_config(db, monkeypatch):  # noqa: F811
    import subprocess

    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    from hokma.core.config import claude_config_dir

    monkeypatch.setattr(plugin_install, "claude_path", lambda: "/bin/claude")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("HOKMA_SESSION_TOKEN", "secret")
    assert plugin_install.run_claude(["plugin", "list"], 5) == (0, "ok")
    assert seen["argv"] == ["/bin/claude", "plugin", "list"]
    assert not seen.get("shell") and seen["timeout"] == 5 and seen["stdin"] == subprocess.DEVNULL
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(claude_config_dir())
    assert "ANTHROPIC_API_KEY" not in seen["env"] and "HOKMA_SESSION_TOKEN" not in seen["env"]
