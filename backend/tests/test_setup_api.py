"""First run and Settings > General: the real routes against the real database, disk and git, with a stand-in CLI."""
import json
import os
import sys
import time
from pathlib import Path

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from fastapi.testclient import TestClient

from hokma.core.app import create_app
from hokma.core.config import hokma_data_dir, hokma_home
from hokma.db import current_version, get_database, migrate
from hokma.kit import owner as owner_names
from hokma.rpc.ws import SESSION_TOKEN
from hokma.setup import claude, database
from hokma.setup.messages import t

FIXTURE = Path(__file__).resolve().parent / "setup_fixtures" / "fake_claude_auth.py"
HEADERS = {"X-Hokma-Token": SESSION_TOKEN}


def lang_headers(lang: str) -> dict:
    return {**HEADERS, "Accept-Language": lang}


@pytest.fixture
def fake_cli(env, monkeypatch):  # noqa: F811
    bin_dir = env / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FIXTURE)!r}, run_name='__main__')\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    yield bin_dir
    claude.stop_login()


@pytest.fixture
def client(env):  # noqa: F811
    return TestClient(create_app())


def journal_titles() -> list[str]:
    with get_database().read() as u:
        return [e.title for e in u.journal.list()]


# ---------------------------------------------------------------- access

def test_every_route_needs_the_token(client):
    for method, path in (("get", "/api/setup"), ("post", "/api/setup"), ("get", "/api/claude"),
                         ("post", "/api/claude/login"), ("get", "/api/fs/dirs"), ("post", "/api/workdir"),
                         ("post", "/api/owner"), ("get", "/api/owner/preview")):
        assert getattr(client, method)(path).status_code == 401, path
    assert client.get("/api/setup", headers={"X-Hokma-Token": "wrong"}).status_code == 401
    assert client.get(f"/api/setup?token={SESSION_TOKEN}").status_code == 200


# ---------------------------------------------------------------- state and the 0.0.4 owner

def test_fresh_install_is_not_completed_and_has_no_names(client):
    state = client.get("/api/setup", headers=HEADERS).json()
    assert state["completed"] is False and state["profile"] is None
    assert state["owner"] == {"owner_name": "", "fund_name": ""}
    assert state["database"]["engine"] == "sqlite"
    assert [o["id"] for o in state["workdir"]["options"]][0] == "documents"
    assert current_version() > 0                                           # the schema was created on demand


def test_owner_of_0_0_4_sees_names_preloaded_and_setup_still_open(client):
    owner_names.save("Camila", "Fondo Cedro")                              # what 0.0.4 left in owner.json
    state = client.get("/api/setup", headers=HEADERS).json()
    assert state["completed"] is False
    assert state["owner"] == {"owner_name": "Camila", "fund_name": "Fondo Cedro"}
    assert state["profile"]["owner_name"] == "Camila"


# ---------------------------------------------------------------- Claude

def test_claude_not_installed(client, env, monkeypatch):  # noqa: F811
    empty = env / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    body = client.get("/api/claude", headers=HEADERS).json()
    assert body["state"] == "no_cli" and body["installed"] is False and body["plan"] is None
    assert client.post("/api/claude/login", headers=HEADERS).status_code == 409


def test_claude_without_session_then_login_with_code_shows_plan(client, fake_cli):
    body = client.get("/api/claude", headers=HEADERS).json()
    assert body["state"] == "no_session" and body["installed"] is True and body["version"] == "9.9.9"

    started = client.post("/api/claude/login", headers=HEADERS).json()
    assert started["running"] is True and started["url"].startswith("https://claude.com/")
    assert client.post("/api/claude/login/code", headers=HEADERS, json={"code": "bad code!"}).status_code == 400
    assert client.post("/api/claude/login/code", headers=HEADERS, json={"code": "good-code"}).json() == {"sent": True}

    for _ in range(50):
        body = client.get("/api/claude", headers=HEADERS).json()
        if body["state"] == "connected":
            break
        time.sleep(0.1)
    assert body["state"] == "connected" and body["plan"] == "Max"
    assert (hokma_data_dir() / "claude-config" / "logged_in").exists()      # Hokma's own config, not the personal one


def test_claude_plan_is_absent_when_the_cli_does_not_report_it(client, fake_cli, monkeypatch):
    cfg = hokma_data_dir() / "claude-config"
    cfg.mkdir(parents=True)
    (cfg / "logged_in").write_text("")
    body = client.get("/api/claude", headers=HEADERS).json()
    assert body["state"] == "connected" and body["plan"] is None


def test_login_that_fails_shows_the_real_output_without_secrets(client, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN_FAIL", "1")
    for lang in ("en", "es"):
        lead = claude.t("setup.err.login_exited", code=3, output="").text(lang).split("Claude")[0]
        resp = client.post("/api/claude/login", headers=lang_headers(lang))
        assert resp.status_code == 500
        message = resp.json()["detail"]["message"]
        assert message.startswith(lead)
        assert "Error: OAuth flow failed" in message          # the real output, not discarded
        assert "https://claude.com/cai/oauth/authorize" in message
        for secret in ("SECRETSTATE123", "sk-ant-api03", "A1b2C3d4A1b2C3d4", "\x1b"):
            assert secret not in message
        claude.stop_login()


def test_login_that_dies_later_reports_its_output_in_the_status(client, fake_cli):
    assert client.post("/api/claude/login", headers=HEADERS).json()["running"] is True
    client.post("/api/claude/login/code", headers=HEADERS, json={"code": "wrong"})
    for _ in range(50):
        info = client.get("/api/claude", headers=lang_headers("en")).json()["login"]
        if "error" in info:
            break
        time.sleep(0.1)
    assert info["running"] is False and "Invalid code" in info["error"]


def test_login_code_without_a_login_in_progress(client, fake_cli):
    assert client.post("/api/claude/login/code", headers=HEADERS, json={"code": "good-code"}).status_code == 409


def test_claude_status_is_stored_in_the_profile(client, fake_cli):
    client.post("/api/owner", headers=HEADERS, json={"owner_name": "Ana"})
    cfg = hokma_data_dir() / "claude-config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "logged_in").write_text("pro")
    client.get("/api/claude", headers=HEADERS)
    with get_database().read() as u:
        profile = u.profile.get()
    assert (profile.claude_status, profile.claude_plan) == ("connected", "Pro")


# ---------------------------------------------------------------- folder browser

def test_fs_lists_only_visible_directories(client, env):  # noqa: F811
    base = env / "browse"
    (base / "beta").mkdir(parents=True)
    (base / "Alpha").mkdir()
    (base / ".hidden").mkdir()
    (base / "file.txt").write_text("x")
    body = client.get("/api/fs/dirs", params={"path": str(base)}, headers=HEADERS).json()
    assert [d["name"] for d in body["dirs"]] == ["Alpha", "beta"]
    assert body["parent"] == str(env)
    start = client.get("/api/fs/dirs", headers=HEADERS).json()
    assert start["path"] is None and start["roots"][0]["kind"] == "home"
    assert client.get("/api/fs/dirs", params={"path": str(base / "file.txt")}, headers=HEADERS).status_code == 400
    assert client.get("/api/fs/dirs", params={"path": "relative/path"}, headers=HEADERS).status_code == 400


def test_fs_new_folder_rules(client, env):  # noqa: F811
    base = env / "mk"
    base.mkdir()
    ok = client.post("/api/fs/dirs", headers=HEADERS, json={"parent": str(base), "name": "Fondo"})
    assert ok.status_code == 201 and (base / "Fondo").is_dir()
    assert client.post("/api/fs/dirs", headers=HEADERS, json={"parent": str(base), "name": "Fondo"}).status_code == 409
    for bad in ("", "..", "a/b", "a\\b", "con", "x" * 101, "name."):
        assert client.post("/api/fs/dirs", headers=HEADERS, json={"parent": str(base), "name": bad}).status_code == 400
    inside = client.post("/api/fs/dirs", headers=HEADERS, json={"parent": str(hokma_data_dir().parent), "name": "data"})
    assert inside.status_code in (400, 409)


# ---------------------------------------------------------------- working folder

def test_workdir_is_created_with_git_kit_and_pointer(client, env):  # noqa: F811
    target = env / "mi fondo"
    body = client.post("/api/workdir", headers=HEADERS, json={"path": str(target)}).json()
    assert body["created"] is True and body["git_initialized"] is True and body["git"] is True
    assert (target / ".git").is_dir()
    assert (target / "CLAUDE.md").is_file() and (target / "HOKMA.md").is_file()
    assert (hokma_data_dir() / "workdir").read_text().strip() == str(target.resolve())
    assert hokma_home() == target.resolve()
    assert (target / ".hokma").is_dir() or (hokma_data_dir() / "claude-config" / "settings.json").exists()
    assert any("Working folder" in t for t in journal_titles())


def test_workdir_agents_are_read_into_the_database(client, env):  # noqa: F811
    client.post("/api/workdir", headers=HEADERS, json={"path": str(env / "w")})
    state = client.get("/api/setup", headers=HEADERS).json()
    assert state["agents"]["total"] >= 5
    assert state["workdir"]["manual"] is True


def test_workdir_existing_repo_is_not_touched(client, env):  # noqa: F811
    target = env / "repo"
    (target / ".git").mkdir(parents=True)
    (target / ".git" / "marker").write_text("mine")
    body = client.post("/api/workdir", headers=HEADERS, json={"path": str(target)}).json()
    assert body["git_initialized"] is False and body["created"] is False
    assert (target / ".git" / "marker").read_text() == "mine"
    assert not (target / ".git" / "HEAD").exists()


def test_workdir_changed_gets_the_whole_kit_and_the_old_one_is_left_alone(client, env):  # noqa: F811
    first, second = env / "one", env / "two"
    client.post("/api/workdir", headers=HEADERS, json={"path": str(first)})
    (first / "my-notes.md").write_text("notes")
    client.post("/api/workdir", headers=HEADERS, json={"path": str(second)})
    assert (second / "HOKMA.md").is_file() and (second / "agents" / "hokma.md").is_file()
    assert (first / "my-notes.md").read_text() == "notes" and (first / "HOKMA.md").is_file()
    assert hokma_home() == second.resolve()


def test_workdir_rejects_the_data_folder_its_parents_and_children(client, env):  # noqa: F811
    data = hokma_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    for bad in (data, data / "inner", data / "runtime" / "a", data.parent, Path(data.anchor)):
        r = client.post("/api/workdir", headers=HEADERS, json={"path": str(bad)})
        assert r.status_code == 400, bad
        assert r.json()["detail"]["code"] in ("path_protected", "path_root")
    assert not (data / "workdir").exists()


def test_workdir_rejects_bad_input(client, env):  # noqa: F811
    for body in ({}, {"path": ""}, {"path": 7}, {"path": "relative"}):
        assert client.post("/api/workdir", headers=HEADERS, json=body).status_code == 400
    a_file = env / "afile"
    a_file.write_text("x")
    r = client.post("/api/workdir", headers=HEADERS, json={"path": str(a_file)})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "not_directory"


def test_workdir_through_a_symlink_into_data_is_rejected(client, env):  # noqa: F811
    data = hokma_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    link = env / "innocent"
    link.symlink_to(data)
    assert client.post("/api/workdir", headers=HEADERS, json={"path": str(link)}).status_code == 400


# ---------------------------------------------------------------- owner

def test_owner_is_cleaned_limited_stored_and_filled_into_the_kit(client, env):  # noqa: F811
    client.post("/api/workdir", headers=HEADERS, json={"path": str(env / "w")})
    long_name = "A" * 100
    body = client.post("/api/owner", headers=HEADERS,
                       json={"owner_name": "  # Camila   Rios  ", "fund_name": f"{{{{x}}}} <b>{long_name}"}).json()
    assert body["profile"]["owner_name"] == "Camila Rios"
    fund = body["profile"]["fund_name"]
    assert len(fund) <= owner_names.MAX_LENGTH and "{" not in fund and "<" not in fund
    assert owner_names.load() == {"owner_name": "Camila Rios", "fund_name": fund}
    manual = (env / "w" / "HOKMA.md").read_text()
    assert "Camila Rios" in manual and "{{owner_name}}" not in manual and fund in manual
    assert any("Names saved" in t for t in journal_titles())


def test_owner_without_fund_uses_the_default_text_and_name_is_required(client, env):  # noqa: F811
    client.post("/api/workdir", headers=HEADERS, json={"path": str(env / "w")})
    for bad in ({}, {"owner_name": ""}, {"owner_name": "   "}, {"owner_name": "###"}, {"owner_name": 5}):
        r = client.post("/api/owner", headers=HEADERS, json=bad)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "name_required"
    state = client.post("/api/owner", headers=HEADERS, json={"owner_name": "Luis"}).json()
    assert state["profile"]["fund_name"] is None
    assert "the fund" in (env / "w" / "HOKMA.md").read_text()


def test_owner_rename_updates_the_manual_in_the_workdir(client, env):  # noqa: F811
    client.post("/api/workdir", headers=HEADERS, json={"path": str(env / "w")})
    client.post("/api/owner", headers=HEADERS, json={"owner_name": "Luis", "fund_name": "Uno"})
    assert "Uno" in (env / "w" / "HOKMA.md").read_text()
    client.post("/api/owner", headers=HEADERS, json={"owner_name": "Luis", "fund_name": "Dos"})
    text = (env / "w" / "HOKMA.md").read_text()
    assert "Dos" in text and "Uno" not in text


def test_owner_preview_matches_what_kit_sync_writes(client, env):  # noqa: F811
    preview = client.get("/api/owner/preview", params={"owner_name": "Camila", "fund_name": "Cedro"},
                         headers=HEADERS).json()
    assert "Camila" in preview["text"] and "Cedro" in preview["text"] and "{{" not in preview["text"]
    assert not preview["text"].lstrip().startswith("<!--")
    empty = client.get("/api/owner/preview", headers=HEADERS).json()
    assert "the owner" in empty["text"] and "the fund" in empty["text"]
    client.post("/api/workdir", headers=HEADERS, json={"path": str(env / "w")})
    client.post("/api/owner", headers=HEADERS, json={"owner_name": "Camila", "fund_name": "Cedro"})
    written = (env / "w" / "HOKMA.md").read_text()
    assert preview["text"].strip() in written


def test_owner_bad_language_or_theme(client, env):  # noqa: F811
    assert client.post("/api/owner", headers=HEADERS, json={"owner_name": "A", "language": "fr"}).status_code == 400
    assert client.post("/api/owner", headers=HEADERS, json={"owner_name": "A", "theme": "pink"}).status_code == 400


# ---------------------------------------------------------------- completing and profile

def test_complete_needs_the_owner_first(client, env):  # noqa: F811
    r = client.post("/api/setup", headers=HEADERS)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_profile"
    client.post("/api/owner", headers=HEADERS, json={"owner_name": "Ana"})
    state = client.post("/api/setup", headers=HEADERS).json()
    assert state["completed"] is True and state["profile"]["setup_completed_at"]
    again = client.post("/api/setup", headers=HEADERS).json()
    assert again["profile"]["setup_completed_at"] == state["profile"]["setup_completed_at"]


def test_theme_and_language_are_saved_in_the_profile(client, env):  # noqa: F811
    assert client.patch("/api/profile", headers=HEADERS, json={"theme": "light"}).status_code == 409
    client.post("/api/owner", headers=HEADERS, json={"owner_name": "Ana"})
    state = client.patch("/api/profile", headers=HEADERS, json={"theme": "light", "language": "en"}).json()
    assert state["profile"]["theme"] == "light" and state["profile"]["language"] == "en"
    assert (hokma_data_dir() / "language").read_text().strip() == "en"
    assert client.patch("/api/profile", headers=HEADERS, json={"theme": "pink"}).status_code == 400
    assert client.patch("/api/profile", headers=HEADERS, json={"language": "fr"}).status_code == 400


# ---------------------------------------------------------------- database engine (D11 owns test and migrate)

def test_state_reports_the_engine_really_in_use(client, env, monkeypatch):  # noqa: F811
    state = client.get("/api/setup", headers=HEADERS).json()
    assert state["database"] == {"engine": "sqlite", "location": None}
    monkeypatch.setattr(database, "engine_in_use", lambda: "postgres")
    monkeypatch.setattr(database, "location_shown", lambda: "postgresql://u@h:5432/db")
    assert client.get("/api/setup", headers=HEADERS).json()["database"] == {"engine": "postgres", "location": "postgresql://u@h:5432/db"}


def test_profile_follows_the_engine_after_a_switch_and_it_is_journaled(client, env, monkeypatch):  # noqa: F811
    client.post("/api/owner", headers=HEADERS, json={"owner_name": "Ana"})
    assert client.get("/api/setup", headers=HEADERS).json()["profile"]["database_engine"] == "sqlite"
    monkeypatch.setattr(database, "engine_in_use", lambda: "postgres")
    state = client.patch("/api/profile", headers=HEADERS, json={"database_engine": "whatever"}).json()
    assert state["profile"]["database_engine"] == "postgres"                  # the value sent is not trusted
    assert any("Database: postgres" in t_ for t_ in journal_titles())
    client.patch("/api/profile", headers=HEADERS, json={"database_engine": "x"})
    assert sum("Database:" in t_ for t_ in journal_titles()) == 1             # nothing changed, nothing journaled


def test_the_database_routes_are_not_duplicated_here(client, env):  # noqa: F811
    paths = [(r.path, tuple(sorted(r.methods))) for r in client.app.routes if hasattr(r, "methods")]
    assert len([p for p in paths if p[0] == "/api/database/test"]) == 1


# ---------------------------------------------------------------- the whole flow, both languages

@pytest.mark.parametrize("lang", ["es", "en"])
def test_first_run_end_to_end(client, env, fake_cli, lang):  # noqa: F811
    h = lang_headers(lang)
    target = env / "Documentos" / "Hokma"

    # opens: nothing is set up
    state = client.get("/api/setup", headers=h).json()
    assert state["completed"] is False and state["profile"] is None

    # 1. Claude
    assert client.get("/api/claude", headers=h).json()["state"] == "no_session"
    client.post("/api/claude/login", headers=h)
    client.post("/api/claude/login/code", headers=h, json={"code": "good-code"})
    for _ in range(50):
        if client.get("/api/claude", headers=h).json()["state"] == "connected":
            break
        time.sleep(0.1)

    # 2. folder: a protected one is refused in the user's language, then a good one
    bad = client.post("/api/workdir", headers=h, json={"path": str(hokma_data_dir())})
    assert bad.status_code == 400 and bad.json()["detail"]["message"] == t("setup.err.path_protected").text(lang)
    ok = client.post("/api/workdir", headers=h, json={"path": str(target)})
    assert ok.status_code == 200

    # 3. database: local, nothing to do
    assert client.get("/api/setup", headers=h).json()["database"]["engine"] == "sqlite"

    # 4. names (the panel sends its language and theme)
    owner = client.post("/api/owner", headers=h, json={"owner_name": "Camila Rios", "fund_name": "Fondo Cedro",
                                                       "language": lang, "theme": "light"})
    assert owner.status_code == 200

    # 5. done
    state = client.post("/api/setup", headers=h).json()
    assert state["completed"] is True

    # in the database
    with get_database().read() as u:
        profile = u.profile.get()
        titles = [e.title for e in u.journal.list()]
        agents = u.agents.count()
    assert profile.owner_name == "Camila Rios" and profile.fund_name == "Fondo Cedro"
    assert profile.workdir == str(target.resolve()) and profile.database_engine == "sqlite"
    assert profile.language == lang and profile.theme == "light"
    assert (profile.claude_status, profile.claude_plan) == ("connected", "Max")
    assert profile.setup_completed_at
    assert t("setup.journal.done").text(lang) in titles
    assert t("setup.journal.workdir", path=str(target.resolve())).text(lang) in titles
    assert t("setup.journal.owner", owner="Camila Rios", fund=" / Fondo Cedro").text(lang) in titles
    assert agents >= 5

    # on disk
    assert (target / ".git").is_dir()
    assert "Camila Rios" in (target / "HOKMA.md").read_text() and "Fondo Cedro" in (target / "HOKMA.md").read_text()
    assert (target / "agents" / "hokma.md").is_file() and (target / "CLAUDE.md").is_file()
    assert owner_names.load() == {"owner_name": "Camila Rios", "fund_name": "Fondo Cedro"}
    assert (hokma_data_dir() / "language").read_text().strip() == lang

    # it survives a restart: a new app, same data
    again = TestClient(create_app()).get("/api/setup", headers=h).json()
    assert again["completed"] is True and again["workdir"]["path"] == str(target.resolve())


def test_settings_change_folder_and_names_after_setup(client, env):  # noqa: F811
    client.post("/api/workdir", headers=HEADERS, json={"path": str(env / "a")})
    client.post("/api/owner", headers=HEADERS, json={"owner_name": "Ana", "fund_name": "Uno"})
    client.post("/api/setup", headers=HEADERS)
    client.post("/api/workdir", headers=HEADERS, json={"path": str(env / "b")})
    client.post("/api/owner", headers=HEADERS, json={"owner_name": "Ana Maria", "fund_name": "Dos"})
    state = client.get("/api/setup", headers=HEADERS).json()
    assert state["completed"] is True                                      # relaunching never unsets it
    assert state["workdir"]["path"] == str((env / "b").resolve())
    assert state["profile"]["workdir"] == str((env / "b").resolve())
    assert state["profile"]["owner_name"] == "Ana Maria" and state["profile"]["fund_name"] == "Dos"
    assert "Dos" in (env / "b" / "HOKMA.md").read_text()
    assert "Uno" in (env / "a" / "HOKMA.md").read_text()                   # the old folder is not touched


def test_setup_state_never_leaks_the_session_token_or_urls(client, env):  # noqa: F811
    client.post("/api/owner", headers=HEADERS, json={"owner_name": "Ana"})
    assert SESSION_TOKEN not in json.dumps(client.get("/api/setup", headers=HEADERS).json())
