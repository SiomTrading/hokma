"""Agents API: the real kit agents, model changes in base and file, on/off, create, counters."""
import sys

import pytest

from agents_support import HEADERS, client, db, make_client, work  # noqa: F401
from hokma.agents import files as fl
from hokma.agents.service import AgentService
from hokma.i18n import t

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in assertions")
KIT = {"axel", "diego", "hokma", "hugo", "iris", "joel", "joel-dev-frontend", "joel-dev-python", "joel-mcp-builder",
       "vera"}


def by_slug(payload):
    items = payload["agents"] + ([payload["orchestrator"]] if payload["orchestrator"] else [])
    return {i["slug"]: i for i in items}


def test_every_route_needs_the_token(work):
    with make_client() as anon:
        for method, path in (("get", "/api/agents"), ("get", "/api/agents/summary"), ("get", "/api/usage/weekly"),
                             ("get", "/api/scheduled-jobs")):
            assert getattr(anon, method)(path).status_code == 401
        assert anon.put("/api/agents/hokma/model", json={"model": "opus"}).status_code == 401
        assert anon.put("/api/usage/cap", json={"cap": 5}).status_code == 401


def test_kit_agents_are_listed_and_the_first_state_is_given_once(client, work):
    data = client.get("/api/agents").json()
    assert data["total"] == len(KIT) and set(by_slug(data)) == KIT
    assert data["orchestrator"]["slug"] == "hokma" and data["orchestrator"]["active"]
    # base kit and Joel's sub-agents on; the rest of the specialists wait for their release
    assert {s for s, a in by_slug(data).items() if a["active"]} == {
        "hokma", "diego", "joel", "vera", "joel-dev-frontend", "joel-dev-python", "joel-mcp-builder"}
    assert data["active"] == 7
    assert client.get("/api/agents/summary").json() == {"active": 7, "total": 10}
    assert work.loaded() == {"hokma", "diego", "joel", "vera", "joel-dev-frontend", "joel-dev-python", "joel-mcp-builder"}
    assert work.parked() == {"axel", "hugo", "iris"}


def test_the_owner_choice_survives_a_restart(client, work):
    assert client.put("/api/agents/iris/status", json={"status": "active"}).status_code == 200
    assert client.put("/api/agents/diego/status", json={"status": "off"}).status_code == 200
    AgentService().reconcile()                                   # what the next start does
    data = by_slug(client.get("/api/agents").json())
    assert data["iris"]["active"] and not data["diego"]["active"]
    assert "iris" in work.loaded() and "diego" in work.parked() and "diego" not in work.loaded()


def test_model_change_updates_the_database_and_the_file(client, work, db):
    out = client.put("/api/agents/vera/model", json={"model": "opus"})
    assert out.status_code == 200 and out.json()["model"] == "opus" and out.json()["tier"] == "deep"
    with db.read() as u:
        assert u.agents.get("vera").model_tier == "deep"
    for rel in ("agents/vera.md", ".claude/agents/vera.md"):
        text = (work.home / rel).read_text()
        assert "\nmodel: opus\n" in text and "\nhokma-tier: deep\n" in text
    # the rest of the file is untouched
    assert "model: sonnet" not in (work.home / "agents" / "vera.md").read_text()
    assert client.get("/api/agents").json()["agents"]  # still listed
    assert client.put("/api/agents/vera/model", json={"model": "gpt"}).json()["detail"]["code"] == "model_invalid"
    assert client.put("/api/agents/nobody/model", json={"model": "opus"}).status_code == 404


def test_model_of_an_off_agent_goes_to_the_parked_copy(client, work):
    client.put("/api/agents/iris/model", json={"model": "haiku"})
    assert "model: haiku" in (work.home / ".claude" / "agents.off" / "iris.md").read_text()
    assert "iris" not in work.loaded()


def test_turn_off_and_on_moves_the_file_and_the_counter(client, work):
    before = client.get("/api/agents/summary").json()["active"]
    off = client.put("/api/agents/vera/status", json={"status": "off"})
    assert off.status_code == 200 and off.json()["active"] is False
    assert "vera" not in work.loaded() and "vera" in work.parked()
    assert client.get("/api/agents/summary").json()["active"] == before - 1
    on = client.put("/api/agents/vera/status", json={"status": "active"}).json()
    assert on["active"] and "vera" in work.loaded() and "vera" not in work.parked()
    assert client.get("/api/agents/summary").json()["active"] == before


def test_the_orchestrator_cannot_be_turned_off(client):
    res = client.put("/api/agents/hokma/status", json={"status": "off"})
    assert res.status_code == 409
    assert res.json()["detail"] == {"code": "orchestrator_required", "message": t("agents.error.orchestrator_required")}


def test_create_my_first_agent(client, work, db):
    body = {"name": "Analista de Gr\u00e1ficos", "description": "Lee gr\u00e1ficos: sin opinar.", "model": "haiku",
            "instructions": "Describe el gr\u00e1fico que te den.\nNada m\u00e1s."}
    res = client.post("/api/agents", json=body)
    assert res.status_code == 201, res.text
    made = res.json()
    assert made["slug"] == "analista-de-graficos" and made["is_custom"] and made["active"] and made["model"] == "haiku"
    for rel in ("agents/analista-de-graficos.md", ".claude/agents/analista-de-graficos.md"):
        text = (work.home / rel).read_text()
        assert text.startswith("---\nname: analista-de-graficos\n") and "model: haiku" in text
        assert text.rstrip().endswith("Describe el gr\u00e1fico que te den.\nNada m\u00e1s.")
    with db.read() as u:
        row = u.agents.get("analista-de-graficos")
        assert row.is_custom and row.status == "active" and row.file_path == "agents/analista-de-graficos.md"
    # it is what /agente completes from and accepts
    listed = client.get("/api/agents").json()
    assert "analista-de-graficos" in by_slug(listed) and listed["total"] == 11
    assert client.post("/api/agents", json=body).json()["detail"]["code"] == "agent_exists"
    # a restart keeps it custom and on
    AgentService().reconcile()
    again = by_slug(client.get("/api/agents").json())["analista-de-graficos"]
    assert again["is_custom"] and again["active"]


@pytest.mark.parametrize("patch,code", [({"name": "!!!"}, "name_invalid"), ({"description": "  "}, "description_required"),
                                        ({"instructions": ""}, "instructions_required"), ({"model": "gpt"}, "model_invalid")])
def test_create_validation(client, patch, code):
    body = {"name": "Uno", "description": "d", "model": "sonnet", "instructions": "i", **patch}
    res = client.post("/api/agents", json=body)
    assert res.status_code == 422 and res.json()["detail"]["code"] == code
    assert res.json()["detail"]["message"] == t(f"agents.error.{code}")


def test_extra_fields_are_refused(client):
    res = client.post("/api/agents", json={"name": "Uno", "description": "d", "model": "sonnet",
                                           "instructions": "i", "tools": "Bash"})
    assert res.status_code == 422


def test_custom_agent_works_in_the_chat_command(client, work, db):
    """The chat engine's agent list and its /agente check read the same rows."""
    client.post("/api/agents", json={"name": "Mi Agente", "description": "d", "model": "sonnet", "instructions": "i"})
    from hokma.engine.dbstore import DbAgents
    from hokma.engine.memory import ProjectRecord
    assert DbAgents(db).exists("mi-agente", ProjectRecord("1", "x", str(work.home)))
