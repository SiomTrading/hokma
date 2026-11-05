"""Projects (D6): folders, Git, files, paths that try to leave the project, save from the chat."""
import os
import subprocess

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from engine_support import png_bytes
from fastapi.testclient import TestClient

from hokma.i18n import t
from hokma.core.app import create_app
from hokma.projects import service
from hokma.projects.errors import ProjectError
from hokma.rpc.ws import SESSION_TOKEN

LINKS = t("projects.links.file")
H = {"X-Hokma-Token": SESSION_TOKEN}
posix_only = pytest.mark.skipif(os.name != "posix", reason="symlinks and O_NOFOLLOW")


@pytest.fixture
def client(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
    return TestClient(create_app())


def git_log(path):
    return subprocess.run(["git", "-C", str(path), "log", "--pretty=%s"], capture_output=True, text=True).stdout.split("\n")


def make(client, name="Alpha", kind="strategy"):
    r = client.post("/api/projects", headers=H, json={"name": name, "kind": kind})
    assert r.status_code == 201, r.text
    return r.json()


def upload(client, pid, name, data, mime="application/octet-stream"):
    return client.post(f"/api/projects/{pid}/files", headers=H, files={"file": (name, data, mime)})


# ---------------------------------------------------------------------------------------- create / list

def test_requires_token(client):
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects", headers={"X-Hokma-Token": "nope"}).status_code == 401


def test_create_project_folder_subfolders_git_and_row(client, db):
    p = make(client)
    root = os.path.join(os.environ["HOKMA_HOME"], "projects", "alpha")
    assert p["path"] == root and p["kind"] == "strategy" and p["git"]["ok"] is True
    for sub in ("contexto", "documentos", "imagenes", "audios", "codigo", "resultados"):
        assert os.path.isdir(os.path.join(root, sub))
    assert os.path.isdir(os.path.join(root, ".git"))
    assert git_log(root)[0] == "Create project"
    with db.read() as u:
        assert [x.slug for x in u.projects.list()] == ["alpha"]


def test_first_list_creates_main_project_with_the_fund_name(client):
    data = client.get("/api/projects", headers=H).json()
    assert [(p["name"], p["slug"]) for p in data["projects"]] == [("Fondo Prueba", "principal")]
    assert data["active_id"] == data["projects"][0]["id"] and data["projects"][0]["active"] is True
    assert client.get("/api/projects", headers=H).json()["projects"].__len__() == 1


def test_only_a_project_the_user_creates_marks_the_onboarding(client, db):
    client.get("/api/projects", headers=H)
    with db.read() as u:
        assert "first_project" not in u.onboarding.progress(("first_project",)).steps or not u.onboarding.progress(("first_project",)).steps["first_project"]
    make(client)
    with db.read() as u:
        assert u.onboarding.progress(("first_project",)).steps["first_project"] is True


def test_chat_only_sees_active_agents(db):
    from hokma.engine.dbstore import DbAgents
    with db.write() as u:
        u.agents.create_custom("analyst", "Analyst")
        u.agents.set_status("analyst", "active")
    assert DbAgents(db).exists("analyst", None) is True
    with db.write() as u:
        u.agents.set_status("analyst", "off")
    assert DbAgents(db).exists("analyst", None) is False


def test_same_name_gets_another_folder_and_bad_input_is_refused(client):
    a, b = make(client), make(client)
    assert a["path"] != b["path"] and b["slug"] == "alpha-2"
    assert client.post("/api/projects", headers=H, json={"name": "x", "kind": "nope"}).status_code == 400
    assert client.post("/api/projects", headers=H, json={"name": "  ", "kind": "other"}).status_code in (400, 422)


def test_rename_keeps_the_folder(client):
    p = make(client)
    r = client.patch(f"/api/projects/{p['id']}", headers=H, json={"name": "Beta"})
    assert r.status_code == 200 and r.json()["name"] == "Beta" and r.json()["path"] == p["path"]
    assert client.patch("/api/projects/999", headers=H, json={"name": "x"}).status_code == 404


def test_activate_is_used_by_the_chat_engine(client, db):
    from hokma.engine.dbstore import DbProjects
    a, b = make(client, "One"), make(client, "Two")
    assert DbProjects(db).default().path == a["path"]          # the first one is active
    r = client.post(f"/api/projects/{b['id']}/activate", headers=H)
    assert r.json()["active_id"] == b["id"]
    assert DbProjects(db).default().path == b["path"]
    assert DbProjects(db).get(None).name == "Two"
    assert client.post("/api/projects/999/activate", headers=H).status_code == 404


# ----------------------------------------------------------------------------------------------- files

def test_upload_goes_to_its_subfolder_with_a_commit_and_a_row(client, db):
    p = make(client)
    r = upload(client, p["id"], "grafico.png", png_bytes(), "image/png")
    assert r.status_code == 201 and r.json()["path"] == "imagenes/grafico.png" and r.json()["git"]["ok"]
    assert upload(client, p["id"], "nota.pdf", b"%PDF-1.4 x").json()["path"] == "documentos/nota.pdf"
    assert upload(client, p["id"], "voz.m4a", b"audio-bytes").json()["path"] == "audios/voz.m4a"
    assert upload(client, p["id"], "bot.py", b"print(1)").json()["path"] == "codigo/bot.py"
    assert upload(client, p["id"], "raro.xyz", b"data").json()["path"] == "contexto/raro.xyz"
    tree = client.get(f"/api/projects/{p['id']}/tree", headers=H).json()
    paths = [e["path"] for e in tree["entries"]]
    assert "imagenes/grafico.png" in paths and "documentos/nota.pdf" in paths and ".git" not in paths
    assert "Add file grafico.png" in tree["commits"] and tree["git"]["ok"]
    assert "Add file grafico.png" in git_log(p["path"])
    with db.read() as u:
        rows = u.attachments.list(project_id=p["id"])
    assert {r.name for r in rows} >= {"grafico.png"} or len(rows) == 5
    assert sorted(r.kind for r in rows) == ["audio", "file", "file", "file", "image"]


def test_same_content_is_not_duplicated_and_other_content_keeps_both(client):
    p = make(client)
    first = upload(client, p["id"], "a.txt", b"one").json()
    again = upload(client, p["id"], "a.txt", b"one").json()
    other = upload(client, p["id"], "a.txt", b"two").json()
    assert again["path"] == first["path"] and again["created"] is False
    assert other["path"] != first["path"] and other["path"].startswith("documentos/a-")


def test_upload_limits(client):
    p = make(client)
    assert upload(client, p["id"], "e.txt", b"").status_code == 400
    big = client.post(f"/api/projects/{p['id']}/files", headers={**H, "X-Filename": "big.bin"},
                      content=b"0" * (25 * 1024 * 1024 + 70 * 1024))
    assert big.status_code == 413


def test_hostile_file_names_stay_inside(client):
    p = make(client)
    for name in ("../../evil.txt", "..\\..\\evil.txt", "/etc/passwd", ".git/config", "a/../../b.txt"):
        r = upload(client, p["id"], name, b"x")
        assert r.status_code == 201
        assert not r.json()["path"].startswith((".git", "..", "/")) and ".." not in r.json()["path"]
    assert not os.path.exists(os.path.join(os.path.dirname(p["path"]), "evil.txt"))
    assert not os.path.exists(os.path.join(os.environ["HOKMA_HOME"], "evil.txt"))


# ------------------------------------------------------------------------------------------ safe paths

@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", "a//b", "./a", "..", ".git/config", "C:/x",
                                 "a\\b", "a/.", "x\x00y"])
def test_read_refuses_paths_that_leave_the_project(client, bad):
    p = make(client)
    r = client.get(f"/api/projects/{p['id']}/file", headers=H, params={"path": bad})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_path"


@posix_only
def test_symlinks_out_of_the_project_are_not_read_listed_or_written(client, tmp_path):
    p = make(client)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    folder = p["path"]
    os.symlink(outside, os.path.join(folder, "contexto", "out"))                  # folder link
    os.symlink(outside / "secret.txt", os.path.join(folder, "contexto", "s.txt"))  # file link
    for path in ("contexto/out/secret.txt", "contexto/s.txt"):
        r = client.get(f"/api/projects/{p['id']}/file", headers=H, params={"path": path})
        assert r.status_code == 400, path
    names = [e["path"] for e in client.get(f"/api/projects/{p['id']}/tree", headers=H).json()["entries"]]
    assert "contexto/out" not in names and "contexto/s.txt" not in names
    # a subfolder that became a link: the upload is refused and nothing lands outside
    os.rename(os.path.join(folder, "imagenes"), os.path.join(folder, "imagenes-old"))
    os.symlink(outside, os.path.join(folder, "imagenes"))
    r = upload(client, p["id"], "x.png", png_bytes(), "image/png")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_path"
    assert os.listdir(outside) == ["secret.txt"]
    # the links file turned into a link to an outside file: the append is refused
    os.symlink(outside / "secret.txt", os.path.join(folder, *LINKS.split("/")))
    r = client.post(f"/api/projects/{p['id']}/links", headers=H, json={"url": "https://example.com/x", "download": False})
    assert r.status_code == 400
    assert (outside / "secret.txt").read_text() == "secret"


@posix_only
def test_project_folder_that_is_a_link_is_not_used(client, tmp_path, db):
    p = make(client)
    real = tmp_path / "real"
    os.rename(p["path"], real)
    os.symlink(real, p["path"])
    assert client.get(f"/api/projects/{p['id']}/tree", headers=H).status_code == 400
    assert upload(client, p["id"], "a.txt", b"x").status_code == 400


def test_read_and_preview_types(client):
    p = make(client)
    upload(client, p["id"], "n.md", "hola \u00f1".encode())
    upload(client, p["id"], "p.html", b"<script>alert(1)</script>")
    upload(client, p["id"], "i.png", png_bytes(), "image/png")
    upload(client, p["id"], "b.bin", b"\x00\x01\xff\xfe")
    r = client.get(f"/api/projects/{p['id']}/file", headers=H, params={"path": "documentos/n.md"})
    assert r.text == "hola \u00f1" and r.headers["content-type"].startswith("text/plain")
    assert r.headers["x-content-type-options"] == "nosniff" and "sandbox" in r.headers["content-security-policy"]
    html = client.get(f"/api/projects/{p['id']}/file", headers=H, params={"path": "contexto/p.html"})
    assert html.headers["content-type"].startswith("text/plain")
    assert client.get(f"/api/projects/{p['id']}/file", headers=H, params={"path": "imagenes/i.png"}).headers["content-type"] == "image/png"
    assert client.get(f"/api/projects/{p['id']}/file", headers=H, params={"path": "contexto/b.bin"}).headers["content-type"] == "application/octet-stream"
    assert client.get(f"/api/projects/{p['id']}/file", headers=H, params={"path": "contexto/none.txt"}).status_code == 404
    cut = client.get(f"/api/projects/{p['id']}/file", headers=H, params={"path": "documentos/n.md", "limit": 2})
    assert cut.content == b"ho"


def test_wav_duration_in_tree(client, tmp_path):
    import wave
    p = make(client)
    path = os.path.join(p["path"], "audios", "t.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 16000)
    entry = next(e for e in client.get(f"/api/projects/{p['id']}/tree", headers=H).json()["entries"] if e["name"] == "t.wav")
    assert entry["duration"] == 2.0


# ------------------------------------------------------------------------------------------------ git

def test_git_config_with_a_program_disables_git_but_files_are_saved(client, tmp_path):
    p = make(client)
    marker = tmp_path / "ran"
    with open(os.path.join(p["path"], ".git", "config"), "a") as f:
        f.write(f"[core]\n\tfsmonitor = touch {marker}\n[filter \"x\"]\n\tclean = touch {marker}\n")
    r = upload(client, p["id"], "a.txt", b"x")
    assert r.status_code == 201 and r.json()["git"] == {"ok": False, "reason": "unsafe"}
    assert os.path.isfile(os.path.join(p["path"], "documentos", "a.txt")) and not marker.exists()
    assert client.get(f"/api/projects/{p['id']}/tree", headers=H).json()["git"]["ok"] is False


@posix_only
def test_hooks_in_the_repository_do_not_run(client, tmp_path):
    p = make(client)
    marker = tmp_path / "hook-ran"
    hook = os.path.join(p["path"], ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write(f"#!/bin/sh\ntouch {marker}\n")
    os.chmod(hook, 0o755)
    assert upload(client, p["id"], "a.txt", b"x").json()["git"]["ok"] is True
    assert not marker.exists()


# ------------------------------------------------------------------------------------------ save from chat

def test_save_attachment_and_text_from_the_chat(client, db):
    p = make(client)
    a = client.post("/api/attachments", headers=H, files={"file": ("foto.png", png_bytes(), "image/png")}).json()
    r = client.post(f"/api/projects/{p['id']}/save", headers=H, json={
        "attachments": [a["id"]], "notes": [{"name": "voz-transcripcion", "text": "Review tomorrow"}]})
    assert r.status_code == 201, r.text
    assert r.json()["saved"] == ["imagenes/foto.png", "documentos/voz-transcripcion.md"]
    assert (open(os.path.join(p["path"], "documentos", "voz-transcripcion.md")).read()) == "Review tomorrow"
    assert git_log(p["path"])[0] == "Save 2 item(s) from the chat"
    assert client.post(f"/api/projects/{p['id']}/save", headers=H, json={"attachments": ["9999"]}).status_code == 404
    assert client.post(f"/api/projects/{p['id']}/save", headers=H, json={}).status_code == 400


# ------------------------------------------------------------------------------------------------- i18n

def test_errors_come_in_the_active_language(client, monkeypatch):
    p = make(client)
    monkeypatch.setenv("HOKMA_LANG", "es")
    r = client.get(f"/api/projects/{p['id']}/file", headers=H, params={"path": "../x"})
    assert "no est\u00e1 permitida" in r.json()["detail"]["message"]
    monkeypatch.setenv("HOKMA_LANG", "en")
    r = client.get(f"/api/projects/{p['id']}/file", headers=H, params={"path": "../x"})
    assert "not allowed" in r.json()["detail"]["message"]


def test_catalog_keys_exist_in_both_languages():
    import json
    from hokma.i18n import CATALOG_DIR
    es, en = (json.load(open(CATALOG_DIR / f"{x}.json", encoding="utf-8")) for x in ("es", "en"))
    keys = {k for k in en if k.startswith("projects.")}
    assert keys == {k for k in es if k.startswith("projects.")} and len(keys) > 30
    for code in ("bad_path", "url_private", "type_blocked", "too_large"):
        assert f"projects.err.{code}" in keys
    assert isinstance(service.SUBFOLDERS, tuple) and ProjectError
