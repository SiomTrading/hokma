"""The panels of a desk screen and the Company panel (SPEC-025, delivery 3, task 3336): the layout column, adding, removing and moving panels
by hand and by the chief, and the data of the Company panel (P1 to P5 of the spec, the part the engine decides)."""
import json

import pytest
from desks_support import db, desks_db, env, make_desk  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_db_migrate import _desk, _desk_project, raw

from dbsupport import take_back_the_migrations_after_0033
from hokma.db import db_path
from hokma.db.downgrade import rollback_0026, rollback_0027, rollback_0028, rollback_0029, rollback_0030, rollback_0031, rollback_0032, rollback_0033
from hokma.db.migrate import discover
from hokma.db.migrate import MIGRATIONS_DIR, current_version, migrate
from hokma.desks import company, panels
from hokma.desks.api import router as desks_router
from hokma.kit import hokma_tools
from hokma.rpc.ws import SESSION_TOKEN

DEFAULT = ["terminal", "agents", "operations", "rules", "config"]


@pytest.fixture
def client(desks_db):
    app = FastAPI()
    app.include_router(desks_router)
    with TestClient(app) as c:
        c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
        yield c


def panels_of(client, desk_id):
    return client.get(f"/api/desks/{desk_id}").json()["panels"]


# ---- the column and the way back ---------------------------------------------------------------------------------------------------

def test_the_migration_adds_a_null_layout_and_the_way_back_takes_it_off(env):
    assert "ADD COLUMN layout" in (MIGRATIONS_DIR / "0026_desk_layout.sql").read_text(encoding="utf-8")
    migrate()
    con = raw()
    desk = _desk(con, _desk_project(con))
    con.commit()
    assert con.execute("SELECT layout FROM desks WHERE id = ?", (desk,)).fetchone() == (None,)
    con.execute("UPDATE desks SET layout = ? WHERE id = ?", (panels.dump(["company", "rules", "agents", "config"]), desk))
    con.commit()
    con.close()
    take_back_the_migrations_after_0033(db_path())
    assert rollback_0033() == 32 and rollback_0032() == 31 and rollback_0031() == 30 and rollback_0030() == 29 and rollback_0029() == 28 and rollback_0028() == 27 and rollback_0027() == 26 and rollback_0026() == 25 and current_version() == 25
    con = raw()
    assert "layout" not in [r[1] for r in con.execute("PRAGMA table_info(desks)")]
    assert con.execute("SELECT count(*) FROM desks").fetchone()[0] == 1 and con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    assert migrate().applied == [v for v, _, _ in discover() if v >= 26]                # and forward again


# ---- the logic ---------------------------------------------------------------------------------------------------------------------

def test_without_a_layout_a_desk_has_the_five_panels_of_before():                      # P4
    assert panels.resolve(None) == DEFAULT and panels.resolve("") == DEFAULT and panels.resolve("not json") == DEFAULT


def test_a_damaged_layout_never_loses_a_fixed_panel_and_drops_what_it_does_not_know():
    assert panels.resolve(panels.dump(["company", "nope", "company", "terminal"])) == ["company", "terminal", "rules", "agents", "config"]


def test_a_new_panel_enters_before_the_fixed_ones():
    assert panels.with_added(DEFAULT, "company") == ["terminal", "company", "agents", "operations", "rules", "config"]
    assert panels.with_added(["rules", "agents", "config", "terminal"], "company") == ["company", "rules", "agents", "config", "terminal"]
    assert panels.with_added(DEFAULT, "terminal") == DEFAULT                              # already there: nothing moves


# ---- by hand, over HTTP ------------------------------------------------------------------------------------------------------------

def test_add_company_and_remove_operations_survive_a_reload_and_stay_in_that_desk(client, desks_db):          # P1
    one, two = make_desk(desks_db, name="Uno"), make_desk(desks_db, name="Dos")
    assert panels_of(client, one["id"]) == DEFAULT == panels_of(client, two["id"])
    assert client.post(f"/api/desks/{one['id']}/panels", json={"id": "company"}).status_code == 201
    assert client.delete(f"/api/desks/{one['id']}/panels/operations").status_code == 200
    assert panels_of(client, one["id"]) == ["terminal", "company", "agents", "rules", "config"]
    assert panels_of(client, two["id"]) == DEFAULT
    assert client.get("/api/desks").json()["desks"][0]["layout"] == {"panels": ["terminal", "company", "agents", "rules", "config"]}


def test_the_fixed_panels_cannot_be_removed_and_a_bad_name_is_refused(client, desks_db):                         # P3
    desk = make_desk(desks_db)
    for fixed in panels.FIXED:
        res = client.delete(f"/api/desks/{desk['id']}/panels/{fixed}")
        assert res.status_code == 409 and res.json()["detail"]["code"] == "panel_fixed"
    assert client.delete(f"/api/desks/{desk['id']}/panels/nope").status_code == 400
    assert client.post(f"/api/desks/{desk['id']}/panels", json={"id": "nope"}).status_code == 400
    assert client.delete("/api/desks/9999/panels/company").status_code == 404
    assert panels_of(client, desk["id"]) == DEFAULT


def test_the_whole_order_is_saved_and_the_fixed_ones_move(client, desks_db):                                       # P3
    desk = make_desk(desks_db)
    order = ["rules", "terminal", "operations", "agents", "config"]
    res = client.put(f"/api/desks/{desk['id']}/panels", json={"panels": order})
    assert res.status_code == 200 and res.json()["panels"] == order and panels_of(client, desk["id"]) == order
    order = ["rules", "terminal", "operations", "config", "agents"]
    assert client.put(f"/api/desks/{desk['id']}/panels", json={"panels": order}).json()["panels"] == order
    for bad in (["terminal"], ["terminal", "terminal", *panels.FIXED], "x", ["rules", "agents", "config", "nope"]):
        assert client.put(f"/api/desks/{desk['id']}/panels", json={"panels": bad}).status_code in (400, 409)
    assert panels_of(client, desk["id"]) == order
    assert client.put(f"/api/desks/{desk['id']}/panels", json={"panels": order}).status_code == 200


def test_the_routes_need_the_token(client, desks_db):
    desk = make_desk(desks_db)
    anonymous = TestClient(client.app)
    assert anonymous.post(f"/api/desks/{desk['id']}/panels", json={"id": "company"}).status_code == 401
    assert anonymous.get(f"/api/desks/{desk['id']}/company/AAPL").status_code == 401


# ---- by chat -----------------------------------------------------------------------------------------------------------------------

def run(name, args):
    text, is_error = hokma_tools.call(name, args, hokma_tools.Caller({}))
    return (json.loads(text) if not is_error else text), is_error


def test_the_chief_adds_and_removes_a_panel_at_once_without_asking(client, desks_db):                              # P2
    desk = make_desk(desks_db)
    for name in ("desk_panel_add", "desk_panel_remove"):
        assert hokma_tools.TOOLS[name][4] is True
    from hokma.permissions import modes

    assert not any("panel" in x for x in modes.ALWAYS_ASK)                            # no permission question in auto mode
    out, err = run("desk_panel_add", {"desk_id": desk["id"], "panel": "company"})
    assert not err and out["panels"] == ["terminal", "company", "agents", "operations", "rules", "config"]
    assert panels_of(client, desk["id"]) == out["panels"]                             # the screen reads the same thing
    out, err = run("desk_panel_remove", {"desk_id": desk["id"], "panel": "operations"})
    assert not err and "operations" not in out["panels"]


def test_the_chief_cannot_take_off_a_fixed_panel_and_stays_in_its_own_desk(desks_db):
    desk = make_desk(desks_db)
    out, err = run("desk_panel_remove", {"desk_id": desk["id"], "panel": "rules"})
    assert err and "terminal, operations, company, calendar, analysis, strategies, option_detail" in out                           # the schema itself offers only the ones that can leave
    from hokma.desks import service

    with pytest.raises(Exception) as refused:                                          # and the service refuses it too
        service.remove_panel(desk["id"], "rules", db=desks_db)
    assert refused.value.code == "panel_fixed"
    with pytest.raises(ValueError):
        hokma_tools.desk_panel_add(None, hokma_tools.Caller({}), {"panel": "company"})        # no desk and no active project


# ---- the Company panel -------------------------------------------------------------------------------------------------------------

def fake(monkeypatch, *, down=()):
    def answer(tool, body):
        def call(*args, **kwargs):
            if tool in down:
                raise LookupError("Finnhub no respondio.")
            return {"source": "Finnhub", "fetched_at": "2026-11-25T14:00:00+00:00", **body}
        return call

    c = company.company_tools
    monkeypatch.setattr(c, "company_profile", answer("company_profile", {"name": "Apple Inc", "exchange": "NASDAQ", "industry": "Technology",
                                                                         "currency": "USD", "market_cap_millions": 3500000.0}))
    monkeypatch.setattr(c, "basic_financials", answer("basic_financials", {"metrics": {
        "marketCapitalization": 3600000.0, "beta": 1.24, "peTTM": 36.9, "epsTTM": 6.4, "operatingMarginTTM": 31.5,
        "totalDebt/totalEquityQuarterly": 1.5, "revenueGrowthTTMYoy": 6.1}}))
    monkeypatch.setattr(c, "earnings_calendar", answer("earnings_calendar", {"earnings": [
        {"symbol": "AAPL", "date": "2000-01-01", "hour": "amc"}, {"symbol": "AAPL", "date": "2999-01-30", "hour": "amc", "eps_estimate": 2.1}]}))
    monkeypatch.setattr(c, "earnings_surprises", answer("earnings_surprises", {"surprises": [
        {"period": "2026-09-30", "year": 2026, "quarter": 3, "eps_estimate": 1.5, "eps_actual": 1.6, "surprise": 0.1, "surprise_pct": 6.7}]}))
    monkeypatch.setattr(c, "company_news", answer("company_news", {"news": [
        {"time": "2026-11-24T10:00:00+00:00", "headline": "Apple lanza algo", "source": "Reuters", "summary": "", "url": "https://x"}]}))
    from hokma.market import options

    monkeypatch.setattr(options, "expirations", lambda symbol, desk_id=None: {"source": "Schwab", "expirations": [
        {"date": "2026-12-18", "days": 23}, {"date": "2027-01-15", "days": 51}]})


def test_the_company_panel_has_its_five_blocks_from_our_own_functions(client, desks_db, monkeypatch):                    # P5
    fake(monkeypatch)
    desk = make_desk(desks_db)
    body = client.get(f"/api/desks/{desk['id']}/company/aapl").json()
    assert body["symbol"] == "AAPL" and body["profile"]["name"] == "Apple Inc"
    assert body["fundamentals"]["ratios"] == {"market_cap_millions": 3600000.0, "beta": 1.24, "pe": 36.9, "eps": 6.4,
                                              "operating_margin": 31.5, "debt_to_equity": 1.5, "revenue_growth": 6.1}
    assert body["events"]["earnings"]["next"]["date"] == "2999-01-30"                   # the report of the past is not "next"
    assert [e["date"] for e in body["events"]["expirations"]["items"]] == ["2026-12-18", "2027-01-15"]
    assert body["results"]["rows"][0]["surprise_pct"] == 6.7 and body["news"]["items"][0]["source"] == "Reuters"
    assert body["news"]["ok"] and body["results"]["ok"]


def test_a_provider_that_is_down_says_no_data_and_never_a_zero(client, desks_db, monkeypatch):                          # P5
    fake(monkeypatch, down=("basic_financials", "earnings_surprises"))
    desk = make_desk(desks_db)
    body = client.get(f"/api/desks/{desk['id']}/company/AAPL").json()
    assert body["fundamentals"]["ok"] is False and "ratios" not in body["fundamentals"] and body["fundamentals"]["source"] == "Finnhub"
    assert body["results"]["ok"] is False and "rows" not in body["results"] and body["results"]["reason"]
    assert body["news"]["ok"] is True                                                    # one block down does not take the rest


def test_the_chain_without_a_source_leaves_the_expirations_empty_and_a_bad_symbol_is_refused(client, desks_db, monkeypatch):
    fake(monkeypatch)
    from hokma.market import options

    def broken(symbol, desk_id=None):
        raise LookupError("no chain")
    monkeypatch.setattr(options, "expirations", broken)
    desk = make_desk(desks_db)
    body = client.get(f"/api/desks/{desk['id']}/company/AAPL").json()
    assert body["events"]["expirations"]["ok"] is False and body["events"]["earnings"]["ok"] is True
    assert client.get(f"/api/desks/{desk['id']}/company/NOT%20A%20SYMBOL").status_code == 400
    assert client.get("/api/desks/9999/company/AAPL").status_code == 404


def test_the_company_module_calls_no_library_or_provider_directly():
    import inspect

    imports = [line for line in inspect.getsource(company).splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in imports if any(w in line for w in ("urllib", "requests", "httpx", "yfinance", "finnhub"))]
