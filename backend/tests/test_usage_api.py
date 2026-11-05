"""Consumption: the current week of tokens (chat messages and scheduled runs) against the cap the owner sets."""
from datetime import timedelta

from agents_support import SYSTEM, client, db, make_client, work  # noqa: F401
from hokma.db import from_db_time, to_db_time


def seed(db):
    """Tokens at known times: two chat messages and one scheduled run this week, one message from last week."""
    with db.write() as u:
        week = u.usage.week()
        conv = u.conversations.create(engine="claude_code", title="t", model="sonnet")
        recent = u.messages.add(conv.id, "assistant", "a", model="sonnet", tokens_in=1000, tokens_out=500)
        second = u.messages.add(conv.id, "assistant", "b", model="haiku", tokens_in=200, tokens_out=100)
        old = u.messages.add(conv.id, "assistant", "old", model="sonnet", tokens_in=90000, tokens_out=9000)
        task = u.tasks.create("job", actor=SYSTEM, assigned_agent="hokma")
        u.tasks.claim(task.id, "hokma", actor=SYSTEM, run_model="opus")
        run = u.task_runs.current(task.id)
        u.task_runs.add_tokens(run.id, 3000, 200)
        start = from_db_time(week.starts_at)
        for message, moment in ((recent, start + timedelta(hours=1)), (second, start + timedelta(hours=2)),
                                (old, start - timedelta(days=1))):
            u.session.execute("UPDATE messages SET created_at = :t WHERE id = :id",
                              {"t": to_db_time(moment), "id": message.id})
        u.session.execute("UPDATE task_runs SET started_at = :t", {"t": to_db_time(start + timedelta(hours=3))})


def test_without_a_cap_the_bar_asks_for_one(client, db):
    seed(db)
    data = client.get("/api/usage/weekly").json()
    assert data["cap"] is None and data["pct"] is None
    assert data["used"] == 1500 + 300 + 3200 and data["tokens_in"] == 4200 and data["tokens_out"] == 800
    assert data["messages"] == 2 and data["runs"] == 1                     # last week's message is out
    assert {r["key"]: r["tokens_in"] + r["tokens_out"] for r in data["by_model"]} == {
        "sonnet": 1500, "haiku": 300, "opus": 3200}
    assert data["starts_at"] < data["ends_at"] and data["profile_exists"]


def test_the_cap_gives_the_exact_percentage(client, db):
    seed(db)
    res = client.put("/api/usage/cap", json={"cap": 10000})
    assert res.status_code == 200 and res.json()["cap"] == 10000 and res.json()["pct"] == 50.0
    assert client.get("/api/usage/weekly").json()["pct"] == 50.0
    with db.read() as u:
        assert u.profile.get().weekly_token_cap == 10000
    assert client.put("/api/usage/cap", json={"cap": 4000}).json()["pct"] == 125.0     # over the cap is shown, not hidden
    assert client.put("/api/usage/cap", json={"cap": None}).json()["pct"] is None


def test_invalid_caps(client):
    for value in (0, -5, 10**14):
        res = client.put("/api/usage/cap", json={"cap": value})
        assert res.status_code == 422 and res.json()["detail"]["code"] == "cap_invalid"
    assert client.put("/api/usage/cap", json={"cap": "many"}).status_code == 422
    assert client.put("/api/usage/cap", json={"cap": True}).status_code == 422


def test_empty_database_is_zero_not_invented(client):
    data = client.get("/api/usage/weekly").json()
    assert data["used"] == 0 and data["messages"] == 0 and data["by_day"] == [] and data["by_agent"] == []


def test_a_real_chat_turn_feeds_the_meter(work, db):
    """Tokens stored by the chat engine (stream-json `result`) are what the meter adds up."""
    from engine_support import connect, turn
    from engine_support import make_client as engine_client
    from hokma.engine.dbstore import DbAgents, DbChatStore, DbProjects
    from hokma.engine.engine import ChatEngine
    from hokma.rpc.ws import SESSION_TOKEN

    engine = ChatEngine(store=DbChatStore(db), projects=DbProjects(db), agents=DbAgents(db),
                        launch=lambda: work.launch_spec, session_token=SESSION_TOKEN, stop_grace=0.5)
    with engine_client(engine) as chat, connect(chat) as ws:
        turn(ws, "hello")
    with make_client() as client:
        data = client.get("/api/usage/weekly", headers={"X-Hokma-Token": SESSION_TOKEN}).json()
    assert data["used"] == 17711 and data["messages"] >= 1
