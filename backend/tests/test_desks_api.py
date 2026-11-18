"""Desks over HTTP (SPEC-014 P4): every route, its error codes, the token, and that the home usage route is untouched."""
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.agents.api import create_router as agents_router
from hokma.db import Actor
from hokma.desks.api import router as desks_router
from hokma.rpc.ws import SESSION_TOKEN

SLUGS = ("desk-chief", "hugo", "risk", "iris", "signals", "axel", "vera", "bull-case", "bear-case", "judge")
OWNER = Actor.human("owner")
HEADERS = {"X-Hokma-Token": SESSION_TOKEN}


@pytest.fixture
def desks_db(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
        for slug in SLUGS + ("extra",):
            u.agents.upsert_kit(slug, slug.title(), f"{slug} agent", "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
        u.agents.set_status("extra", "off")
    return db


@pytest.fixture
def client(desks_db):
    app = FastAPI()
    app.include_router(desks_router)
    app.include_router(agents_router())
    with TestClient(app) as c:
        c.headers.update(HEADERS)
        yield c


def new_desk(client, name="Futuros NQ", **kw):
    body = {"name": name, "capital": 10000, "template": "minimal", **kw}
    res = client.post("/api/desks", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def code(res):
    return res.json()["detail"]["code"]


# ------------------------------------------------------------------------------------------ token

ROUTES = [("get", "/api/desks"), ("post", "/api/desks"), ("get", "/api/desks/templates"), ("get", "/api/desks/1"),
          ("patch", "/api/desks/1"), ("put", "/api/desks/1/assets"), ("get", "/api/desks/1/connections"),
          ("put", "/api/desks/1/connections/coinbase"), ("get", "/api/desks/1/agents"),
          ("post", "/api/desks/1/agents"), ("put", "/api/desks/1/agents/hugo"),
          ("delete", "/api/desks/1/agents/hugo"), ("get", "/api/desks/1/usage"),
          ("post", "/api/desks/1/conversation")]


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_route_needs_the_token(desks_db, method, path):
    app = FastAPI()
    app.include_router(desks_router)
    with TestClient(app) as anon:
        res = getattr(anon, method)(path, json={}) if method in ("post", "put", "patch") else getattr(anon, method)(path)
        assert res.status_code == 401 and code(res) == "bad_token"
        wrong = getattr(anon, method)(path, headers={"X-Hokma-Token": "nope"})
        assert wrong.status_code == 401


def test_the_token_also_works_as_a_query_parameter(desks_db):
    app = FastAPI()
    app.include_router(desks_router)
    with TestClient(app) as anon:
        assert anon.get(f"/api/desks?token={SESSION_TOKEN}").status_code == 200


# ------------------------------------------------------------------------------------------ fund and create

def test_templates_come_before_the_id_route(client):
    items = {t["key"]: t for t in client.get("/api/desks/templates").json()["templates"]}
    assert set(items) == {"minimal", "standard", "debate", "custom"}
    assert [len(items[k]["agents"]) for k in ("minimal", "standard", "debate")] == [3, 7, 10]
    assert items["standard"]["suggested_budget"] == 300_000


def test_create_returns_the_detail_and_the_fund_lists_it(client):
    desk = new_desk(client, focus="NQ intraday", assets=["NQ=F", {"symbol": "ES=F", "note": "second"}],
                    objective_pct=3, mandate="Opening range", token_budget=5000)
    assert desk["mode"] == "simulation" and desk["capital"] == 10000 and desk["agents_total"] == 3
    assert [a["symbol"] for a in desk["assets"]] == ["NQ=F", "ES=F"]
    assert desk["budget"]["token_budget"] == 5000 and desk["budget"]["state"] == "ok"
    listed = client.get("/api/desks").json()["desks"]
    assert [d["id"] for d in listed] == [desk["id"]]
    row = listed[0]
    for field in ("id", "project_id", "name", "focus", "capital", "status", "template", "today", "drawdown",
                  "agents_total", "agents_working", "tokens_week", "token_budget", "budget_state"):
        assert field in row, field
    assert row["today"] == 0 and row["drawdown"] is None and row["agents_working"] == 0 and row["tokens_week"] == 0
    assert client.get(f"/api/desks/{desk['id']}").json()["name"] == "Futuros NQ"


def test_create_errors_have_stable_codes(client):
    cases = [({"name": "x", "capital": 5}, 400, "bad_capital"),
             ({"name": "", "capital": 1000}, 400, "bad_name"),
             ({"name": "x", "capital": 1000, "template": "huge"}, 400, "bad_template"),
             ({"name": "x", "capital": 1000, "token_budget": 0}, 400, "bad_budget"),
             ({"name": "x", "capital": 1000, "objective_pct": 500}, 400, "bad_objective"),
             ({"name": "x", "capital": 1000, "assets": ["bad symbol!"]}, 400, "bad_symbol"),
             ({"name": "x", "capital": 1000, "assets": [{"note": "no symbol"}]}, 400, "bad_symbol"),
             ({"name": "x", "capital": 1000, "assets": ["A", "A"]}, 400, "bad_assets"),
             ({"name": "x", "capital": 1000, "assets": "NQ"}, 400, "bad_assets")]
    for body, status, expected in cases:
        res = client.post("/api/desks", json=body)
        assert (res.status_code, code(res)) == (status, expected), body
        assert res.json()["detail"]["message"]
    assert client.post("/api/desks", json={"capital": 1000}).status_code == 422       # name missing
    assert client.get("/api/desks").json()["desks"] == []                              # nothing was left behind


def test_missing_agents_give_409(client, desks_db):
    with desks_db.write() as u:
        u.session.execute("DELETE FROM agents WHERE slug = 'judge'")
    res = client.post("/api/desks", json={"name": "Debate", "capital": 1000, "template": "debate"})
    assert res.status_code == 409 and code(res) == "agent_missing"


def test_unknown_desk_is_404_everywhere(client):
    for method, path, body in (("get", "/api/desks/99", None), ("patch", "/api/desks/99", {"name": "x"}),
                               ("put", "/api/desks/99/assets", {"assets": []}), ("get", "/api/desks/99/agents", None),
                               ("get", "/api/desks/99/usage", None), ("post", "/api/desks/99/conversation", None),
                               ("get", "/api/desks/99/connections", None)):
        res = getattr(client, method)(path, **({"json": body} if body is not None else {}))
        assert (res.status_code, code(res)) == (404, "not_found"), path


# ------------------------------------------------------------------------------------------ patch and assets

def test_patch_changes_only_what_is_sent(client):
    desk = new_desk(client, mandate="Old", token_budget=1000)
    res = client.patch(f"/api/desks/{desk['id']}", json={"capital": 25000, "mandate": "New"})
    assert res.status_code == 200
    out = res.json()
    assert (out["capital"], out["mandate"], out["name"], out["token_budget"]) == (25000, "New", "Futuros NQ", 1000)
    cleared = client.patch(f"/api/desks/{desk['id']}", json={"token_budget": None}).json()
    assert cleared["token_budget"] is None and cleared["budget"]["state"] == "ok"
    assert client.patch(f"/api/desks/{desk['id']}", json={"status": "paused"}).json()["status"] == "paused"


def test_patch_errors_and_archive(client):
    desk_id = new_desk(client)["id"]
    assert code(client.patch(f"/api/desks/{desk_id}", json={"status": "gone"})) == "bad_status"
    assert code(client.patch(f"/api/desks/{desk_id}", json={"capital": 1})) == "bad_capital"
    assert code(client.patch(f"/api/desks/{desk_id}", json={"name": None})) == "bad_name"
    assert client.patch(f"/api/desks/{desk_id}", json={"status": "archived"}).json()["status"] == "archived"
    assert client.get("/api/desks").json()["desks"] == []
    assert len(client.get("/api/desks?include_archived=true").json()["desks"]) == 1
    assert client.delete(f"/api/desks/{desk_id}").status_code == 405                  # desks are never deleted


def test_assets_are_replaced_validated_and_capped_at_twenty(client):
    desk_id = new_desk(client, assets=["NQ=F"])["id"]
    res = client.put(f"/api/desks/{desk_id}/assets", json={"assets": ["ES=F", "BTC-USD"]})
    assert res.status_code == 200 and [a["symbol"] for a in res.json()["assets"]] == ["ES=F", "BTC-USD"]
    assert [a["symbol"] for a in client.get(f"/api/desks/{desk_id}").json()["assets"]] == ["ES=F", "BTC-USD"]
    assert code(client.put(f"/api/desks/{desk_id}/assets", json={"assets": ["not valid"]})) == "bad_symbol"
    too_many = [f"S{i}" for i in range(21)]
    assert code(client.put(f"/api/desks/{desk_id}/assets", json={"assets": too_many})) == "bad_assets"
    ok = [f"S{i}" for i in range(20)]
    assert len(client.put(f"/api/desks/{desk_id}/assets", json={"assets": ok}).json()["assets"]) == 20
    assert client.put(f"/api/desks/{desk_id}/assets", json={"assets": []}).json()["assets"] == []
    assert [a["symbol"] for a in client.get(f"/api/desks/{desk_id}").json()["assets"]] == []


# ------------------------------------------------------------------------------------------ connections

def test_connections_join_the_global_list_with_the_choice_of_the_desk(client):
    desk_id = new_desk(client)["id"]
    items = client.get(f"/api/desks/{desk_id}/connections").json()["connections"]
    ids = [c["provider_id"] for c in items]
    assert "coinbase" in ids and len(ids) >= 2
    assert all(c["enabled"] is False for c in items)
    for field in ("name", "asset_classes", "needs_key", "key_ready", "enabled_global"):
        assert field in items[0]
    res = client.put(f"/api/desks/{desk_id}/connections/coinbase", json={"enabled": True})
    assert res.status_code == 200 and res.json() == {"provider_id": "coinbase", "enabled": True}
    chosen = {c["provider_id"]: c["enabled"] for c in client.get(f"/api/desks/{desk_id}/connections").json()["connections"]}
    assert chosen["coinbase"] is True and sum(chosen.values()) == 1
    assert client.put(f"/api/desks/{desk_id}/connections/coinbase", json={"enabled": False}).json()["enabled"] is False
    other = new_desk(client, "Otra")["id"]
    assert not any(c["enabled"] for c in client.get(f"/api/desks/{other}/connections").json()["connections"])


def test_connection_errors(client):
    desk_id = new_desk(client)["id"]
    res = client.put(f"/api/desks/{desk_id}/connections/nope", json={"enabled": True})
    assert (res.status_code, code(res)) == (404, "bad_connection")
    res = client.put(f"/api/desks/{desk_id}/connections/coinbase", json={"enabled": "yes"})
    assert (res.status_code, code(res)) == (400, "bad_status")
    res = client.put("/api/desks/99/connections/coinbase", json={"enabled": True})
    assert (res.status_code, code(res)) == (404, "not_found")


# ------------------------------------------------------------------------------------------ agents

def running_run(db, project_id, agent):
    with db.write() as u:
        task = u.tasks.create("work", actor=OWNER, project_id=project_id)
        u.task_runs.start(task.id, agent, model="haiku")


def test_agents_list_has_state_tokens_and_activity(client, desks_db):
    desk = new_desk(client)
    desk_id, project_id = desk["id"], desk["project_id"]
    client.put(f"/api/desks/{desk_id}/agents/risk", json={"active": False})
    running_run(desks_db, project_id, "hugo")
    with desks_db.write() as u:
        u.journal.add("action", "Read the chart", actor=Actor.agent("hugo"), project_id=project_id)
        u.journal.add("action", "Wrote a note", actor=Actor.agent("hugo"), project_id=project_id)
        conv = u.conversations.create(engine="claude_code", project_id=project_id, agent_slug="desk-chief")
        u.messages.add(conv.id, "assistant", "x", agent_slug="desk-chief", model="sonnet", tokens_in=70, tokens_out=30)
    agents = {a["agent_slug"]: a for a in client.get(f"/api/desks/{desk_id}/agents").json()["agents"]}
    assert set(agents) == {"desk-chief", "hugo", "risk"}
    assert (agents["hugo"]["status"], agents["desk-chief"]["status"], agents["risk"]["status"]) == ("working", "idle", "off")
    assert agents["desk-chief"]["tokens_week"] == 100 and agents["hugo"]["tokens_week"] == 0
    assert agents["hugo"]["last_action"]["title"] == "Wrote a note"
    assert [a["title"] for a in agents["hugo"]["activity"]] == ["Wrote a note", "Read the chart"]
    assert agents["desk-chief"]["last_action"] is None and agents["desk-chief"]["activity"] == []
    assert agents["hugo"]["can"] == "desks.role.data.can" and agents["hugo"]["cannot"] == "desks.role.data.cannot"
    listed = client.get("/api/desks").json()["desks"][0]
    assert listed["agents_working"] == 1 and listed["tokens_week"] == 100


def test_bring_an_agent_and_its_errors(client, desks_db):
    desk_id = new_desk(client)["id"]
    with desks_db.write() as u:
        for slug in ("iris", "vera"):
            u.agents.set_status(slug, "active")           # the app may have parked them on start
    res = client.post(f"/api/desks/{desk_id}/agents", json={"agent_slug": "iris", "model": "opus"})
    assert res.status_code == 201, res.text
    assert (res.json()["agent_slug"], res.json()["role"], res.json()["model"]) == ("iris", "research", "opus")
    dup = client.post(f"/api/desks/{desk_id}/agents", json={"agent_slug": "iris", "model": "opus"})
    assert (dup.status_code, code(dup)) == (409, "agent_in_desk")
    off = client.post(f"/api/desks/{desk_id}/agents", json={"agent_slug": "extra", "model": "haiku"})
    assert (off.status_code, code(off)) == (409, "agent_off")
    assert client.get("/api/agents").json() is not None
    missing = client.post(f"/api/desks/{desk_id}/agents", json={"agent_slug": "ghost", "model": "haiku"})
    assert (missing.status_code, code(missing)) == (404, "agent_missing")
    bad = client.post(f"/api/desks/{desk_id}/agents", json={"agent_slug": "vera", "model": "gpt"})
    assert (bad.status_code, code(bad)) == (400, "bad_model")
    chief = client.post(f"/api/desks/{desk_id}/agents", json={"agent_slug": "vera", "model": "haiku", "role": "chief"})
    assert (chief.status_code, code(chief)) == (400, "bad_role")
    assert client.post(f"/api/desks/{desk_id}/agents", json={"model": "haiku"}).status_code == 422
    custom = client.post(f"/api/desks/{desk_id}/agents", json={"agent_slug": "vera", "model": "haiku"})
    assert custom.status_code == 201 and custom.json()["role"] == "reviewer"


def test_the_model_changes_on_one_desk_only(client, desks_db):
    a, b = new_desk(client, "Mesa A")["id"], new_desk(client, "Mesa B")["id"]
    with desks_db.read() as u:
        before = u.agents.get("hugo").model_tier
    res = client.put(f"/api/desks/{a}/agents/hugo", json={"model": "opus"})
    assert res.status_code == 200 and res.json()["model"] == "opus"
    models = {d: {x["agent_slug"]: x["model"] for x in client.get(f"/api/desks/{d}/agents").json()["agents"]}
              for d in (a, b)}
    assert models[a]["hugo"] == "opus" and models[b]["hugo"] == "haiku"
    with desks_db.read() as u:
        assert u.agents.get("hugo").model_tier == before                       # Settings are not touched
    assert code(client.put(f"/api/desks/{a}/agents/hugo", json={"model": "gpt"})) == "bad_model"
    assert code(client.put(f"/api/desks/{a}/agents/ghost", json={"model": "opus"})) == "agent_not_in_desk"
    assert code(client.put(f"/api/desks/{a}/agents/hugo", json={"active": "no"})) == "bad_status"
    off = client.put(f"/api/desks/{a}/agents/hugo", json={"active": False}).json()
    assert off["active"] is False and off["model"] == "opus"


def test_the_chief_cannot_be_removed_or_turned_off(client):
    desk_id = new_desk(client)["id"]
    res = client.delete(f"/api/desks/{desk_id}/agents/desk-chief")
    assert (res.status_code, code(res)) == (409, "chief_required")
    res = client.put(f"/api/desks/{desk_id}/agents/desk-chief", json={"active": False})
    assert (res.status_code, code(res)) == (409, "chief_required")
    assert client.put(f"/api/desks/{desk_id}/agents/desk-chief", json={"model": "opus"}).status_code == 200


def test_remove_an_agent_keeps_the_global_agent(client, desks_db):
    desk_id = new_desk(client)["id"]
    res = client.delete(f"/api/desks/{desk_id}/agents/risk")
    assert res.status_code == 204 and res.content == b""
    assert "risk" not in {a["agent_slug"] for a in client.get(f"/api/desks/{desk_id}/agents").json()["agents"]}
    with desks_db.read() as u:
        assert u.agents.find("risk") is not None
    again = client.delete(f"/api/desks/{desk_id}/agents/risk")
    assert (again.status_code, code(again)) == (404, "agent_not_in_desk")


# ------------------------------------------------------------------------------------------ usage

def spend(db, project_id, agent, tin, tout):
    with db.write() as u:
        conv = u.conversations.create(engine="claude_code", project_id=project_id, agent_slug=agent)
        u.messages.add(conv.id, "assistant", "x", agent_slug=agent, model="sonnet", tokens_in=tin, tokens_out=tout)


def test_usage_has_the_states_and_does_not_mix_desks(client, desks_db):
    a = new_desk(client, "Mesa A", token_budget=1000)
    b = new_desk(client, "Mesa B", token_budget=1000)
    empty = client.get(f"/api/desks/{a['id']}/usage").json()
    assert empty["used"] == 0 and empty["state"] == "ok" and empty["by_agent"] == [] and empty["token_budget"] == 1000
    spend(desks_db, a["project_id"], "desk-chief", 500, 300)
    out = client.get(f"/api/desks/{a['id']}/usage").json()
    assert (out["used"], out["pct"], out["state"]) == (800, 80.0, "warn")
    assert out["by_agent"] == [{"key": "desk-chief", "tokens_in": 500, "tokens_out": 300}] and len(out["by_day"]) == 1
    assert client.get(f"/api/desks/{b['id']}/usage").json()["used"] == 0
    spend(desks_db, a["project_id"], "hugo", 150, 50)
    assert client.get(f"/api/desks/{a['id']}/usage").json()["state"] == "exhausted"
    assert client.get("/api/desks").json()["desks"][0]["budget_state"] in ("exhausted", "ok")
    states = {d["name"]: d["budget_state"] for d in client.get("/api/desks").json()["desks"]}
    assert states == {"Mesa A": "exhausted", "Mesa B": "ok"}


def test_the_home_usage_route_is_unchanged(client, desks_db):
    before = client.get("/api/usage/weekly").json()
    a = new_desk(client, "Mesa A")
    assert client.get("/api/usage/weekly").json() == before                    # creating a desk changes nothing
    spend(desks_db, a["project_id"], "desk-chief", 40, 10)
    with desks_db.write() as u:
        conv = u.conversations.create(engine="claude_code")                    # a general conversation, no desk
        u.messages.add(conv.id, "assistant", "y", model="sonnet", tokens_in=7, tokens_out=3)
    weekly = client.get("/api/usage/weekly").json()
    assert weekly["used"] == 60 and weekly["messages"] == 2                    # the whole fund, not filtered by desk
    assert client.get(f"/api/desks/{a['id']}/usage").json()["used"] == 50
    assert set(before) <= set(weekly)


# ------------------------------------------------------------------------------------------ conversation

def test_conversation_is_created_once_and_follows_the_chief_model(client, desks_db):
    desk = new_desk(client)
    first = client.post(f"/api/desks/{desk['id']}/conversation")
    assert first.status_code == 200
    conv = first.json()
    assert conv["created"] is True and conv["agent_slug"] == "desk-chief" and conv["model"] == "sonnet"
    assert conv["project_id"] == desk["project_id"] and conv["title"] == "Futuros NQ"
    again = client.post(f"/api/desks/{desk['id']}/conversation").json()
    assert again["created"] is False and again["conversation_id"] == conv["conversation_id"]
    client.put(f"/api/desks/{desk['id']}/agents/desk-chief", json={"model": "opus"})
    with desks_db.read() as u:
        assert u.conversations.get(conv["conversation_id"]).model == "opus"       # the chief's chat follows the desk model
    assert client.post(f"/api/desks/{desk['id']}/conversation").json()["model"] == "opus"
    other = new_desk(client, "Mesa B")
    second = client.post(f"/api/desks/{other['id']}/conversation").json()
    assert second["conversation_id"] != conv["conversation_id"] and second["model"] == "sonnet"


def test_conversation_of_an_archived_desk_is_refused(client):
    desk_id = new_desk(client)["id"]
    client.patch(f"/api/desks/{desk_id}", json={"status": "archived"})
    res = client.post(f"/api/desks/{desk_id}/conversation")
    assert (res.status_code, code(res)) == (409, "desk_archived")


def test_messages_follow_the_language(client, desks_db):
    from hokma.i18n import use_language

    with use_language("es"):
        res = client.get("/api/desks/99")
    assert res.status_code == 404 and res.json()["detail"]["message"] != "That desk does not exist."
    with use_language("en"):
        assert client.get("/api/desks/99").json()["detail"]["message"] == "That desk does not exist."
