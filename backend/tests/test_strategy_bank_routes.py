"""The read only routes that the Strategies screen uses (1.2.1): the bank with its last versions, one strategy with its rules and their citation, and the code
versions of a strategy. They need the token and decide nothing."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_support import db, env, make_bank, pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)
from hokma.permissions.api import create_router
from hokma.rpc.ws import SESSION_TOKEN


def client() -> TestClient:
    app = FastAPI()
    app.include_router(create_router())
    return TestClient(app, headers={"X-Hokma-Token": SESSION_TOKEN})


def test_the_bank_is_listed_and_one_strategy_is_read_with_its_rules(db):
    bank_id = make_bank(db)
    with client() as c:
        assert TestClient(c.app).get("/api/strategy-bank").status_code == 401
        listed = c.get("/api/strategy-bank").json()
        assert listed["total"] == 1 and listed["rows"][0]["bank_id"] == bank_id
        assert {"title", "status", "version", "source", "origin"} <= set(listed["rows"][0])
        one = c.get(f"/api/strategy-bank/{bank_id}").json()
        assert one["bank_id"] == bank_id and one["version"]["extraction"] and one["versions"][0]["version"] == 1 and "cited" in one["source"]
        assert c.get("/api/strategy-bank/999999").status_code == 404
        assert c.get("/api/strategy-bank?status=nonsense").status_code == 400
        assert c.get(f"/api/strategy-code?bank_id={bank_id}").json() == {"rows": []}      # no code written yet
