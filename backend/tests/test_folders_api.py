"""Disk folders of a project (SPEC-010 P7): the project's own folder registers by itself, the rules of section 4.3
reject what must not be read, and a pass over the folder indexes what the student drops in it."""
import os
import time
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from docs_support import make_pdf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.docs import diskfolders, folder_scan, picker
from hokma.docs.api import router as docs_router
from hokma.core import session
from hokma.projects import service
from hokma.projects.api import router as projects_router
from hokma.rpc.ws import SESSION_TOKEN

H = {"X-Hokma-Token": SESSION_TOKEN}


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "es")
    monkeypatch.setattr(session, "owner_matches", lambda header: True)
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
    app = FastAPI()
    app.include_router(projects_router)
    app.include_router(docs_router)
    with TestClient(app) as c:
        yield c


def new_project(client, name="Proyecto"):
    return client.post("/api/projects", headers=H, json={"name": name}).json()


def folders(client, pid):
    return client.get(f"/api/projects/{pid}/folders", headers=H).json()["folders"]


def project_docs(db, pid):
    with db.read() as u:
        lib = u.documents.find_project_library(pid)
        return u.documents.list_documents(lib.id) if lib else []


def search(db, pid, query):
    with db.read() as u:
        lib = u.documents.find_project_library(pid)
        return [h for h in u.documents.search([lib.id], query)] if lib else []


# ---------------------------------------------------------------------------------------------- own folder

def test_new_project_registers_its_own_folder(client, db):
    project = new_project(client)
    rows = folders(client, project["id"])
    assert len(rows) == 1
    assert rows[0]["own"] is True and rows[0]["label"] is None
    assert rows[0]["path"] == project["path"] and rows[0]["status"] == "scanning"


def test_own_folder_is_registered_once(client, db):
    pid = new_project(client)["id"]
    for _ in range(3):
        folders(client, pid)
        service.list_projects()
    assert len(folders(client, pid)) == 1


def test_existing_projects_get_their_own_folder(client, db):
    pid = new_project(client)["id"]
    with db.write() as u:
        for f in u.library.list_project_folders(pid):
            u.library.remove_project_folder(f.id)
    assert diskfolders.ensure_all_own_folders(db) >= 1
    assert [f["own"] for f in folders(client, pid)] == [True]


def test_own_folder_cannot_be_removed(client):
    project = new_project(client)
    fid = folders(client, project["id"])[0]["id"]
    r = client.delete(f"/api/projects/{project['id']}/folders/{fid}", headers=H)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "disk_own_folder"
    assert len(folders(client, project["id"])) == 1


# ---------------------------------------------------------------------------------------------- the scan

def test_scan_indexes_md_and_pdf_dropped_in_the_project_folder(client, db):
    project = new_project(client)
    pid, root = project["id"], Path(project["path"])
    (root / "documentos" / "nota.md").write_text("# Plan\n\nLa estrategia de zafiro usa cobertura delta.\n", encoding="utf-8")
    (root / "documentos" / "paper.pdf").write_bytes(make_pdf(["El teorema de cuarzo limita el riesgo."]))
    (root / "contexto" / "datos.bin").write_bytes(b"\x00\x01" * 100)         # not a supported format
    fid = folders(client, pid)[0]["id"]
    r = client.post(f"/api/projects/{pid}/folders/{fid}/rescan", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "ready" and r.json()["file_count"] == 2
    docs = {d.rel_path: d for d in project_docs(db, pid)}
    assert set(docs) == {"documentos/nota.md", "documentos/paper.pdf"}
    assert all(d.status == "ready" and d.stored_path == f"folder:{fid}:{d.rel_path}" for d in docs.values())
    assert search(db, pid, "zafiro") and search(db, pid, "cuarzo")


def test_scan_skips_git_hidden_links_and_the_data_folder(client, db, env):
    project = new_project(client)
    pid, root = project["id"], Path(project["path"])
    (root / ".git" / "x.md").parent.mkdir(exist_ok=True)
    (root / ".git" / "x.md").write_text("git secreto", encoding="utf-8")
    (root / ".oculto.md").write_text("oculto", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "m.md").write_text("modulo", encoding="utf-8")
    outside = env / "outside.md"
    outside.write_text("outside the folder", encoding="utf-8")
    os.symlink(outside, root / "link.md")
    (root / "ok.md").write_text("contenido visible", encoding="utf-8")
    folder_scan.scan_folder(folders(client, pid)[0]["id"], db)
    assert [d.rel_path for d in project_docs(db, pid)] == ["ok.md"]


def test_walk_ignores_hokma_data_dir_inside_the_folder(tmp_path, monkeypatch):
    data = tmp_path / "datos-hokma"
    data.mkdir()
    (data / "interno.md").write_text("interno de hokma", encoding="utf-8")
    (tmp_path / "uno.md").write_text("visible", encoding="utf-8")
    monkeypatch.setattr(folder_scan, "hokma_data_dir", lambda: data)
    found, over_cap, too_large = folder_scan.walk_supported(tmp_path)
    assert list(found) == ["uno.md"] and over_cap == 0 and too_large == 0


def test_walk_caps_the_files_and_counts_the_rest(tmp_path):
    for i in range(7):
        (tmp_path / f"f{i}.md").write_text(f"texto {i}", encoding="utf-8")
    found, over_cap, _ = folder_scan.walk_supported(tmp_path, limit=5)
    assert len(found) == 5 and over_cap == 2


def test_scan_does_not_read_a_file_swapped_for_a_link_after_the_walk(client, db, env, monkeypatch):
    project = new_project(client)
    pid, root = project["id"], Path(project["path"])
    secret = env / "secreto.md"
    secret.write_text("no debe leerse", encoding="utf-8")
    (root / "a.md").write_text("contenido normal", encoding="utf-8")
    real_walk = folder_scan.walk_supported

    def swapping(folder, **kw):
        found = real_walk(folder, **kw)
        (root / "a.md").unlink()
        os.symlink(secret, root / "a.md")                                     # replaced between the walk and the read
        return found

    monkeypatch.setattr(folder_scan, "walk_supported", swapping)
    folder_scan.scan_folder(folders(client, pid)[0]["id"], db)
    assert project_docs(db, pid) == [] and not search(db, pid, "leerse")


def test_scan_reindexes_only_on_content_change_and_forgets_removed_files(client, db):
    project = new_project(client)
    pid, root = project["id"], Path(project["path"])
    fid = folders(client, pid)[0]["id"]
    note = root / "nota.md"
    note.write_text("version uno con palabra-alfa", encoding="utf-8")
    assert folder_scan.scan_folder(fid, db).added == 1
    first_id = project_docs(db, pid)[0].id
    stamp = time.time() + 5
    os.utime(note, (stamp, stamp))                                          # touched, same content
    again = folder_scan.scan_folder(fid, db)
    assert (again.added, again.updated, again.unchanged) == (0, 0, 1) and project_docs(db, pid)[0].id == first_id
    note.write_text("version dos con palabra-beta", encoding="utf-8")
    third = folder_scan.scan_folder(fid, db)
    assert third.updated == 1 and search(db, pid, "beta") and not search(db, pid, "alfa")
    note.unlink()
    assert folder_scan.scan_folder(fid, db).removed == 1 and project_docs(db, pid) == []


def test_missing_folder_keeps_its_passages(client, db, env):
    project = new_project(client)
    pid = project["id"]
    outside = env / "otra"
    outside.mkdir()
    (outside / "a.md").write_text("pasaje persistente", encoding="utf-8")
    added = client.post(f"/api/projects/{pid}/folders", headers=H, json={"path": str(outside)})
    assert added.status_code == 201
    fid = added.json()["id"]
    folder_scan.scan_folder(fid, db)
    (outside / "a.md").unlink()
    outside.rmdir()
    assert folder_scan.scan_folder(fid, db).status == "missing"
    assert [d.rel_path for d in project_docs(db, pid)] == ["a.md"]
    assert client.get(f"/api/projects/{pid}/folders", headers=H).json()["folders"][1]["status"] == "missing"


def test_duplicate_content_in_two_folders_is_counted_not_indexed_twice(client, db, env):
    project = new_project(client)
    pid, root = project["id"], Path(project["path"])
    other = env / "copia"
    other.mkdir()
    (root / "a.md").write_text("mismo contenido", encoding="utf-8")
    (other / "b.md").write_text("mismo contenido", encoding="utf-8")
    fid_other = client.post(f"/api/projects/{pid}/folders", headers=H, json={"path": str(other)}).json()["id"]
    folder_scan.scan_folder(folders(client, pid)[0]["id"], db)
    assert folder_scan.scan_folder(fid_other, db).duplicates == 1
    assert len(project_docs(db, pid)) == 1


def test_removing_a_folder_drops_what_it_indexed_and_keeps_the_files(client, db, env):
    project = new_project(client)
    pid = project["id"]
    other = env / "extra"
    other.mkdir()
    (other / "a.md").write_text("contenido extra", encoding="utf-8")
    fid = client.post(f"/api/projects/{pid}/folders", headers=H, json={"path": str(other)}).json()["id"]
    folder_scan.scan_folder(fid, db)
    assert len(project_docs(db, pid)) == 1
    assert client.delete(f"/api/projects/{pid}/folders/{fid}", headers=H).status_code == 200
    assert project_docs(db, pid) == [] and (other / "a.md").exists()


# ---------------------------------------------------------------------------------------------- rejections

def add(client, pid, path):
    return client.post(f"/api/projects/{pid}/folders", headers=H, json={"path": str(path)})


def system_case():
    """A folder of the system that exists on this operating system."""
    if os.name == "nt":
        return (Path(os.environ.get("SystemRoot") or "C:\\Windows"), "disk_system")
    return (Path("/etc"), "disk_system")


def test_rejections_of_section_4_3(client, env, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(env / "home"))                     # Windows reads the home from here
    pid = new_project(client)["id"]
    (env / "home").mkdir(exist_ok=True)
    data = env / "data"
    data.mkdir(exist_ok=True)
    (data / "libraries").mkdir(exist_ok=True)
    cases = [(Path(Path.cwd().anchor), "disk_root"), (env / "home", "disk_home"), (data, "disk_data_dir"),
             (data / "libraries", "disk_data_dir"), (env / "no-existe", "disk_path_missing"),
             ("relative/folder", "bad_path"), system_case()]
    for path, code in cases:
        r = add(client, pid, path)
        assert r.json()["detail"]["code"] == code, (path, r.json())
        assert r.status_code in (400, 404)
    assert len(folders(client, pid)) == 1


def test_a_hidden_folder_is_rejected_with_a_clear_error(client, env):
    pid = new_project(client)["id"]
    for name in (".git", ".secretos"):
        hidden = env / name
        hidden.mkdir()
        r = add(client, pid, hidden)
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "disk_hidden"
        assert r.json()["detail"]["message"]


def test_a_file_is_not_a_folder(client, env):
    pid = new_project(client)["id"]
    f = env / "a.md"
    f.write_text("x", encoding="utf-8")
    assert add(client, pid, f).json()["detail"]["code"] == "disk_not_folder"


def test_duplicates_and_the_limit_of_five(client, env):
    pid = new_project(client)["id"]
    made = []
    for i in range(5):
        d = env / f"c{i}"
        d.mkdir()
        made.append(d)
        assert add(client, pid, d).status_code == 201
    assert add(client, pid, made[0]).json()["detail"]["code"] == "disk_duplicate"
    extra = env / "c5"
    extra.mkdir()
    over = add(client, pid, extra)
    assert over.status_code == 409 and over.json()["detail"]["code"] == "disk_too_many"
    assert len(folders(client, pid)) == 6                                    # five of the student's plus the own one


def test_adding_the_own_folder_again_is_a_duplicate(client):
    project = new_project(client)
    assert add(client, project["id"], project["path"]).json()["detail"]["code"] == "disk_duplicate"


def test_unknown_project_and_folder_are_404(client):
    assert client.get("/api/projects/999/folders", headers=H).status_code == 404
    pid = new_project(client)["id"]
    assert client.delete(f"/api/projects/{pid}/folders/999", headers=H).json()["detail"]["code"] == "disk_folder_not_found"
    assert client.post(f"/api/projects/{pid}/folders/999/rescan", headers=H).status_code == 404


def test_folder_of_another_project_is_not_reachable(client, env):
    a, b = new_project(client, "A")["id"], new_project(client, "B")["id"]
    d = env / "solo-a"
    d.mkdir()
    fid = add(client, a, d).json()["id"]
    assert client.delete(f"/api/projects/{b}/folders/{fid}", headers=H).status_code == 404


def test_routes_need_the_token(client):
    assert client.get("/api/projects/1/folders").status_code == 401
    assert client.post("/api/system/pick-folder").status_code == 401


def test_adding_a_folder_and_the_dialog_need_the_owner_confirmation(client, env, monkeypatch):
    pid = new_project(client)["id"]
    monkeypatch.setattr(session, "owner_matches", lambda header: False)
    d = env / "sin-dueno"
    d.mkdir()
    r = add(client, pid, d)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "owner_confirmation_required"
    assert client.post("/api/system/pick-folder", headers=H).status_code == 403
    assert len(folders(client, pid)) == 1


def test_scan_refuses_a_root_swapped_for_a_link(client, db, env):
    pid = new_project(client)["id"]
    real = env / "real"
    real.mkdir()
    (real / "a.md").write_text("contenido", encoding="utf-8")
    victim = env / "secreto"
    victim.mkdir()
    (victim / "s.md").write_text("no debe leerse", encoding="utf-8")
    fid = add(client, pid, real).json()["id"]
    real.rename(env / "real-movida")
    os.symlink(victim, real)
    result = folder_scan.scan_folder(fid, db)
    assert result.status == "failed" and project_docs(db, pid) == []


# ---------------------------------------------------------------------------------------------- picker

def test_pick_folder_reports_a_chosen_path(client, monkeypatch):
    monkeypatch.setattr(picker, "pick_folder", lambda: picker.Pick(True, "/tmp/x"))
    assert client.post("/api/system/pick-folder", headers=H).json() == {"available": True, "path": "/tmp/x", "cancelled": False}


def test_pick_folder_cancelled_and_unavailable(client, monkeypatch):
    monkeypatch.setattr(picker, "pick_folder", lambda: picker.Pick(True))
    assert client.post("/api/system/pick-folder", headers=H).json()["cancelled"] is True
    monkeypatch.setattr(picker, "pick_folder", lambda: picker.Pick(False))
    assert client.post("/api/system/pick-folder", headers=H).json() == {"available": False, "path": None, "cancelled": False}


def test_picker_command_per_platform(monkeypatch):
    monkeypatch.setattr(picker.sys, "platform", "darwin")
    monkeypatch.setattr(picker.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert picker._command()[0] == "osascript"
    monkeypatch.setattr(picker.sys, "platform", "win32")
    assert "FolderBrowserDialog" in picker._command()[-1]
    monkeypatch.setattr(picker.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert picker._command() is None
    monkeypatch.setenv("DISPLAY", ":0")
    assert picker._command()[0] == "zenity"
    monkeypatch.setattr(picker.shutil, "which", lambda name: "/usr/bin/kdialog" if name == "kdialog" else None)
    assert picker._command()[0] == "kdialog"


def test_picker_run_outcomes(monkeypatch):
    class Done:
        def __init__(self, code, out):
            self.returncode, self.stdout = code, out

    monkeypatch.setattr(picker, "_command", lambda: ["x"])
    monkeypatch.setattr(picker.subprocess, "run", lambda *a, **k: Done(0, "/home/ana/Papers/\n".encode()))
    assert picker.pick_folder().path == "/home/ana/Papers"
    monkeypatch.setattr(picker.subprocess, "run", lambda *a, **k: Done(1, b""))
    assert picker.pick_folder().public()["cancelled"] is True
    monkeypatch.setattr(picker, "_command", lambda: None)
    assert picker.pick_folder().available is False


# ------------------------------------------------------------------------- macOS paths, simulated on any system

HOME_MAC = "/Users/ana"


@pytest.mark.parametrize("text", [
    "/private/var/folders/zz/abc123/T/pytest-0/x", "/private/var/folders/zz/abc123/T", "/private/tmp/work",
    "/private/var/tmp/work", "/var/folders/zz/abc123/T/x", "/Users/ana/Documents/trading", "/Users/ana/Library/x"])
def test_macos_user_folders_are_not_system(text):
    assert diskfolders.is_posix_system_path(text, HOME_MAC) is False


@pytest.mark.parametrize("text", [
    "/System/Library", "/Library/Preferences", "/private/etc", "/private/etc/ssh", "/private/var/db", "/usr/local",
    "/bin", "/sbin", "/Applications/Safari.app", "/etc", "/var/log", "/private", "/dev"])
def test_macos_system_folders_stay_rejected(text):
    assert diskfolders.is_posix_system_path(text, HOME_MAC) is True


def test_a_home_under_a_system_root_is_still_the_users():
    assert diskfolders.is_posix_system_path("/root/proyectos/a", "/root") is False
    assert diskfolders.is_posix_system_path("/root/proyectos/a", "/home/ana") is True
