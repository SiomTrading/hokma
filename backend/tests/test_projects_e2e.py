"""Projects end to end (D6): create, drop a file, add a link, switch project and see the chat use it.

The app is the real one for projects (HTTP), the real SQLite repositories and the chat engine with the fake `claude`.
Runs once per interface language.
"""
import os
import subprocess

import pytest
from engine_support import connect, env, hard_timeout, png_bytes, posix_only, turn  # noqa: F401
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.db import migrate, open_database
from hokma.engine.api import create_router
from hokma.engine.dbstore import DbAgents, DbChatStore, DbProjects
from hokma.engine.engine import ChatEngine
from hokma.i18n import t
from hokma.projects.api import router as projects_router
from hokma.rpc.ws import SESSION_TOKEN

pytestmark = posix_only
H = {"X-Hokma-Token": SESSION_TOKEN}


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True).stdout


@pytest.mark.parametrize("lang", ["es", "en"])
def test_project_flow_from_creation_to_the_chat(env, monkeypatch, lang):
    monkeypatch.setenv("HOKMA_LANG", lang)
    migrate(env.data / "hokma.db")
    db = open_database(env.data / "hokma.db")
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
    engine = ChatEngine(store=DbChatStore(db), projects=DbProjects(db), agents=DbAgents(db),
                        launch=lambda: env.launch_spec, session_token=SESSION_TOKEN, stop_grace=0.5)
    app = FastAPI()
    app.include_router(create_router(lambda: engine))
    app.include_router(projects_router)

    with TestClient(app) as client:
        # first time: the main project exists, named after the fund
        first = client.get("/api/projects", headers=H).json()
        assert [p["name"] for p in first["projects"]] == ["Fondo Prueba"] and first["workdir"] == str(env.home)

        # create
        alpha = client.post("/api/projects", headers=H, json={"name": "Alpha", "kind": "strategy"}).json()
        folder = alpha["path"]
        assert folder == str(env.home / "projects" / "alpha")
        assert all(os.path.isdir(os.path.join(folder, d)) for d in ("contexto", "documentos", "imagenes", "audios", "codigo", "resultados"))
        assert os.path.isdir(os.path.join(folder, ".git")) and git(folder, "log", "--pretty=%s").strip() == t("projects.git.created")
        with db.read() as u:
            assert {p.slug for p in u.projects.list()} == {"principal", "alpha"}

        # drop a file
        client.post(f"/api/projects/{alpha['id']}/activate", headers=H)
        up = client.post(f"/api/projects/{alpha['id']}/files", headers=H, files={"file": ("grafico-nq.png", png_bytes(), "image/png")})
        assert up.status_code == 201 and up.json()["path"] == "imagenes/grafico-nq.png"
        tree = client.get(f"/api/projects/{alpha['id']}/tree", headers=H).json()
        assert "imagenes/grafico-nq.png" in [e["path"] for e in tree["entries"]]
        assert t("projects.git.add_file", name="grafico-nq.png") in git(folder, "log", "--pretty=%s").splitlines()
        assert "grafico-nq.png" in git(folder, "show", "--stat", "--pretty=", "HEAD")

        # add a link (not downloaded)
        link = client.post(f"/api/projects/{alpha['id']}/links", headers=H,
                           json={"url": "https://example.com/informe", "title": "Informe", "download": False}).json()
        assert link["downloaded"] is False
        links = open(os.path.join(folder, *t("projects.links.file").split("/")), encoding="utf-8").read()
        assert "[Informe](https://example.com/informe)" in links
        assert client.post(f"/api/projects/{alpha['id']}/links", headers=H,
                           json={"url": "http://example.com/x", "download": False}).status_code == 400

        # the chat uses the active project
        with connect(client) as ws:
            events = turn(ws, "hola")
        conv = [e for e in events if e["type"] == "conversation"][0]
        assert conv["project"]["name"] == "Alpha"
        assert os.path.realpath(env.entries("start")[-1]["cwd"]) == os.path.realpath(folder)
        # the attachment the chat copies lands inside the project too
        att = client.post("/api/attachments", headers=H, files={"file": ("plan.pdf", b"%PDF-1.4 plan", "application/pdf")}).json()
        with connect(client) as ws:
            turn(ws, "mira", attachments=[att["id"]])
        assert os.path.isfile(os.path.join(folder, "attachments", "plan.pdf"))
        tree = client.get(f"/api/projects/{alpha['id']}/tree", headers=H).json()
        assert "attachments/plan.pdf" in [e["path"] for e in tree["entries"]]

        # switch: the next conversation starts in the other project
        main_id = first["projects"][0]["id"]
        client.post(f"/api/projects/{main_id}/activate", headers=H)
        with connect(client) as ws:
            events = turn(ws, "otra")
        assert [e for e in events if e["type"] == "conversation"][0]["project"]["name"] == "Fondo Prueba"
        assert os.path.realpath(env.entries("start")[-1]["cwd"]) == os.path.realpath(env.home / "projects" / "principal")

        # save from the chat into the typed folder, then it is in the git history
        saved = client.post(f"/api/projects/{alpha['id']}/save", headers=H, json={"attachments": [att["id"]], "notes": [{"name": "nota", "text": "texto"}]})
        assert saved.status_code == 201 and "documentos/plan.pdf" in saved.json()["saved"]
        assert t("projects.git.save_chat", count=2) in git(folder, "log", "--pretty=%s").splitlines()

        # hostile path
        bad = client.get(f"/api/projects/{alpha['id']}/file", headers=H, params={"path": "../../etc/passwd"})
        assert bad.status_code == 400 and bad.json()["detail"]["message"] == t("projects.err.bad_path")
