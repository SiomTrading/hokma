"""D10: extensions, news, system status, the guide and the home numbers (HTTP level, real database and files)."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from dbsupport import db, env  # noqa: F401  (fixtures)
from hokma import __version__
from hokma.core.app import create_app
from hokma.kit import extensions, requests
from hokma.learn import content
from hokma.rpc.ws import SESSION_TOKEN
from hokma.status import catalog, news

HEADERS = {"X-Hokma-Token": SESSION_TOKEN}


@pytest.fixture
def client(db):  # noqa: F811
    os.makedirs(os.environ["HOKMA_HOME"], exist_ok=True)
    news.clear_cache()
    catalog.clear_cache()
    return TestClient(create_app(), headers=HEADERS)


def seed_plugin(plugin_id="tool@acme", enabled=False):
    extensions.add_marketplace("acme", {"source": "github", "repo": "acme/plugins"})
    extensions.set_plugin(plugin_id, enabled)


# ---------- token ----------

@pytest.mark.parametrize("path", ["/api/status", "/api/news", "/api/extensions", "/api/learn/topics",
                                  "/api/learn/progress", "/api/home", "/api/extensions/marketplace"])
def test_routes_need_the_token(db, path):  # noqa: F811
    bare = TestClient(create_app())
    assert bare.get(path).status_code == 401
    assert bare.get(path, headers={"X-Hokma-Token": "wrong"}).status_code == 401
    assert bare.get(path, params={"token": "wrong-" + chr(233)}).status_code == 401
    assert bare.get(path, params={"token": SESSION_TOKEN}).status_code in (200, 502)


# ---------- system status ----------

def test_status_has_doctor_checks_and_database(client):
    body = client.get("/api/status").json()
    names = [c["name"] for c in body["checks"]]
    assert "Database" in names and "Working folder" in names and "git" in names
    database = next(c for c in body["checks"] if c["name"] == "Database")
    assert database["status"] == "ok" and "sqlite" in database["detail"]
    assert body["version"] == __version__
    assert sum(body["summary"].values()) == len(body["checks"])


def test_status_warns_when_there_is_no_database(env):  # noqa: F811
    body = TestClient(create_app(), headers=HEADERS).get("/api/status").json()
    database = next(c for c in body["checks"] if c["name"] == "Database")
    assert database["status"] == "warn"


# ---------- news ----------

def test_news_reads_the_changelog_and_flags_a_newer_release(client, monkeypatch):
    monkeypatch.setattr(news, "fetch_latest", lambda: news.Latest("99.0.0", "- A real note", "2026-11-05T10:00:00Z"))
    body = client.get("/api/news").json()
    assert body["installed"]["version"] == __version__
    assert body["changelog"] and body["changelog"][0]["version"].count(".") == 2
    assert body["newer"] is True and body["latest"]["notes"] == "- A real note"


def test_news_is_quiet_when_up_to_date_and_survives_a_network_failure(client, monkeypatch):
    monkeypatch.setattr(news, "fetch_latest", lambda: news.Latest(__version__))
    assert client.get("/api/news", params={"refresh": "true"}).json()["newer"] is False

    def boom():
        raise OSError("offline")

    monkeypatch.setattr(news, "fetch_latest", boom)
    body = client.get("/api/news", params={"refresh": "true"}).json()
    assert body["latest"] is None and body["latest_error"] and body["changelog"]


def test_news_uses_the_update_source_override(client, monkeypatch, tmp_path):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"version": "9.9.9", "url": "x.tar.gz", "sha256": "a" * 64}))
    monkeypatch.setenv("HOKMA_UPDATE_SOURCE", str(manifest))
    body = client.get("/api/news").json()
    assert body["latest"]["version"] == "9.9.9" and body["newer"] is True


def test_parse_changelog():
    parsed = news.parse_changelog("# Changelog\n\n## 0.0.2\n\n- b\n\n## 0.0.1\n\n- a\n")
    assert parsed == [{"version": "0.0.2", "body": "- b"}, {"version": "0.0.1", "body": "- a"}]


# ---------- extensions ----------

def test_extensions_listing_matches_the_file_and_the_table(client, db):  # noqa: F811
    seed_plugin()
    extensions.set_mcp("files", {"command": "/usr/bin/true"})
    body = client.get("/api/extensions").json()
    assert [p["id"] for p in body["plugins"]] == ["tool@acme"] and body["plugins"][0]["enabled"] is False
    assert "tool@acme" in body["plugins"][0]["summary"]
    assert body["servers"][0]["name"] == "files" and body["marketplaces"][0] == {"name": "acme", "source": "github:acme/plugins"}
    with db.read() as u:
        assert {(e.name, e.kind) for e in u.extensions.list()} == {("tool", "plugin"), ("files", "mcp")}


def test_toggle_needs_confirmation_then_changes_the_file_and_records_an_approval(client, db):  # noqa: F811
    seed_plugin()
    assert client.post("/api/extensions/toggle", json={"id": "tool@acme", "enabled": True}).status_code == 400
    assert extensions.load()[0]["enabledPlugins"]["tool@acme"] is False

    done = client.post("/api/extensions/toggle", json={"id": "tool@acme", "enabled": True, "confirmed": True})
    assert done.status_code == 200
    assert extensions.load()[0]["enabledPlugins"]["tool@acme"] is True
    with db.read() as u:
        request = u.extension_requests.list()[0]
        assert request.status == "applied" and request.action == "enable"
        approval = u.approvals.latest_for("extension", request.id)
        assert approval.verdict == "approved" and approval.channel == "dashboard" and approval.decided_by_kind == "human"
        assert u.extensions.find("acme", "tool").enabled is True

    client.post("/api/extensions/toggle", json={"id": "tool@acme", "enabled": False, "confirmed": True})
    assert extensions.load()[0]["enabledPlugins"]["tool@acme"] is False
    with db.read() as u:
        assert [r.action for r in u.extension_requests.list()] == ["disable", "enable"]


def test_toggle_rejects_bad_ids_and_unknown_plugins(client):
    assert client.post("/api/extensions/toggle", json={"id": "../x@y", "enabled": True, "confirmed": True}).status_code == 422
    unknown = client.post("/api/extensions/toggle", json={"id": "ghost@nowhere", "enabled": True, "confirmed": True})
    assert unknown.status_code == 404


def test_agent_request_is_listed_and_approved(client, db):  # noqa: F811
    seed_plugin()
    req = requests.request_plugin("extra@acme")
    listed = client.get("/api/extensions").json()["requests"]
    assert listed[0]["id"] == req.id and listed[0]["origin"] == "inbox" and "extra@acme" in listed[0]["summary"]

    assert client.post(f"/api/extensions/requests/{req.id}/approve").status_code == 200
    assert extensions.load()[0]["enabledPlugins"]["extra@acme"] is True
    assert client.get("/api/extensions").json()["requests"] == []
    with db.read() as u:
        row = u.extension_requests.list()[0]
        assert row.status == "applied" and row.requested_by_kind == "agent"
        assert u.approvals.latest_for("extension", row.id).channel == "dashboard"


def test_agent_request_is_rejected(client, db):  # noqa: F811
    req = requests.request_mcp("srv", {"command": "/usr/bin/true"})
    assert client.post(f"/api/extensions/requests/{req.id}/reject").status_code == 200
    assert "srv" not in extensions.load()[0]["mcpServers"]
    assert client.get("/api/extensions").json()["requests"] == []
    with db.read() as u:
        assert u.extension_requests.list()[0].status == "rejected"


def test_request_from_the_database_is_approved(client, db):  # noqa: F811
    from hokma.db import Actor
    seed_plugin()
    with db.write() as u:
        row = u.extension_requests.create("install", "plugin", "more", "acme", "need it", actor=Actor.agent("joel"))
    listed = client.get("/api/extensions").json()["requests"]
    assert listed[0]["id"] == f"db-{row.id}" and listed[0]["reason"] == "need it"
    assert client.post(f"/api/extensions/requests/db-{row.id}/approve").status_code == 200
    assert extensions.load()[0]["enabledPlugins"]["more@acme"] is True
    assert client.post(f"/api/extensions/requests/db-{row.id}/approve").status_code == 409


def test_unknown_request_ids(client):
    assert client.post("/api/extensions/requests/deadbeef/approve").status_code == 404
    assert client.post("/api/extensions/requests/db-9999/reject").status_code == 404
    assert client.post("/api/extensions/requests/not-an-id/approve").status_code == 422


def test_add_marketplace_by_link(client):
    ok = client.post("/api/extensions/marketplaces", json={"repo": "acme/plugins"})
    assert ok.status_code == 201 and ok.json()["name"] == "plugins"
    assert "plugins" in extensions.load()[0]["extraKnownMarketplaces"]
    assert client.post("/api/extensions/marketplaces", json={"repo": "not a repo"}).status_code == 422


# ---------- Anthropic marketplace ----------

SAMPLE = {"plugins": [
    {"name": "alpha", "displayName": "Alpha", "description": "Reads charts", "category": "development"},
    {"name": "beta", "description": "Writes reports", "category": "productivity"},
    {"name": "bad name!", "description": "ignored"},
]}


def test_marketplace_lists_real_entries_and_installs_one(client, monkeypatch):
    monkeypatch.setattr(catalog, "fetch", lambda: catalog.parse(SAMPLE))
    body = client.get("/api/extensions/marketplace").json()
    assert body["added"] is False and body["total"] == 2
    assert [i["id"] for i in body["items"]] == ["alpha@claude-plugins-official", "beta@claude-plugins-official"]
    assert client.get("/api/extensions/marketplace", params={"q": "reports"}).json()["total"] == 1

    assert client.post("/api/extensions/marketplace").status_code == 201
    source = extensions.load()[0]["extraKnownMarketplaces"]["claude-plugins-official"]["source"]
    assert source == {"source": "github", "repo": "anthropics/claude-plugins-official"}

    turn_on = client.post("/api/extensions/toggle", json={"id": "alpha@claude-plugins-official", "enabled": True, "confirmed": True})
    assert turn_on.status_code == 200
    after = client.get("/api/extensions/marketplace").json()
    assert after["added"] is True and after["items"][0]["installed"] and after["items"][0]["enabled"]


def test_marketplace_failure_is_a_clear_error(client, monkeypatch):
    def boom():
        raise catalog.CatalogError("no connection")

    monkeypatch.setattr(catalog, "fetch", boom)
    answer = client.get("/api/extensions/marketplace", params={"refresh": "true"})
    assert answer.status_code == 502 and answer.json()["detail"] == "no connection"


# ---------- the guide ----------

def write_topic(root, language, name, text):
    folder = root / "docs" / "learn" / language
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


def test_guide_ships_both_languages_with_the_same_topics():
    en = [t.slug for t in content.topics("en")]
    es = [t.slug for t in content.topics("es")]
    assert en == es and len(en) == 10 and en[0] == "first-steps"
    assert content.get("es", "first-steps").title == "Primeros pasos"


def test_guide_working_folder_overrides_the_app_copy(client, env):  # noqa: F811
    write_topic(env / "work", "es", "01-first-steps.md", "# Mi guia propia\n\ntexto")
    assert client.get("/api/learn/topics", params={"lang": "es"}).json()[0] == {"slug": "first-steps", "title": "Mi guia propia"}
    assert client.get("/api/learn/topics/first-steps", params={"lang": "en"}).json()["title"] == "First steps"
    assert client.get("/api/learn/topics/nothing").status_code == 404
    assert client.get("/api/learn/topics/..%2Fx").status_code in (404, 422)


def test_guide_never_follows_a_link_planted_in_the_working_folder(client, env):  # noqa: F811
    secret = env / "secret.md"
    secret.write_text("# stolen", encoding="utf-8")
    folder = env / "work" / "docs" / "learn" / "en"
    folder.mkdir(parents=True)
    os.symlink(secret, folder / "05-tasks.md")
    topics = {t["slug"]: t["title"] for t in client.get("/api/learn/topics", params={"lang": "en"}).json()}
    assert topics["tasks"] == "Scheduled tasks"


def test_guide_search_covers_all_files_and_ignores_accents(client):
    hits = client.get("/api/learn/search", params={"q": "guia", "lang": "es"}).json()
    assert hits == [] or all("slug" in h for h in hits)
    found = client.get("/api/learn/search", params={"q": "marketplace", "lang": "es"}).json()
    assert "extensions" in [h["slug"] for h in found]
    assert all(h["snippet"] for h in found)
    folded = client.get("/api/learn/search", params={"q": "programadas", "lang": "es"}).json()
    assert folded and folded[0]["slug"] == "tasks"
    assert client.get("/api/learn/search", params={"q": "x"}).json() == []
    assert client.get("/api/learn/search", params={"q": "zzzzqqqq"}).json() == []


# ---------- first day ----------

def test_first_day_progress_goes_from_zero_to_three(client, db):  # noqa: F811
    assert client.get("/api/learn/progress").json() == {
        "ready": True, "done": 0, "total": 3,
        "steps": {"first_conversation": False, "first_project": False, "first_image": False}}

    with db.write() as u:
        conversation = u.conversations.create(engine="claude_code")
        u.messages.add(conversation.id, "user", "hello")
    assert client.get("/api/learn/progress").json()["done"] == 1

    with db.write() as u:
        u.onboarding.mark("first_project")
    assert client.get("/api/learn/progress").json()["steps"]["first_project"] is True

    with db.write() as u:
        u.attachments.add(kind="image", stored_path="/x/a.png", mime_type="image/png", size_bytes=10, sha256="a" * 64)
    body = client.get("/api/learn/progress").json()
    assert body["done"] == 3 and all(body["steps"].values())
    with db.read() as u:
        assert set(u.onboarding.done()) == {"first_conversation", "first_project", "first_image"}


def test_first_day_before_the_database_exists(env):  # noqa: F811
    body = TestClient(create_app(), headers=HEADERS).get("/api/learn/progress").json()
    assert body["ready"] is False and body["done"] == 0


# ---------- home ----------

def test_home_numbers(client, db):  # noqa: F811
    with db.write() as u:
        u.profile.save(owner_name="Ana Perez", fund_name="Fondo Uno", weekly_token_cap=1000)
        u.projects.create("P", "/p")
    body = client.get("/api/home").json()
    assert body["ready"] and body["owner_name"] == "Ana Perez" and body["fund_name"] == "Fondo Uno"
    assert body["projects"] == 1 and body["agents"]["active"] == 4 and body["usage"]["cap"] == 1000
    assert body["usage"]["pct"] == 0.0


def test_home_without_database(env):  # noqa: F811
    assert TestClient(create_app(), headers=HEADERS).get("/api/home").json() == {"ready": False}
