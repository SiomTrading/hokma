"""SPEC-025 delivery 2 (task 3335): a new desk gets its demo account in the Hokma broker, in the same transaction, and
its capital is the account's `initial_capital`."""
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures)

from hokma.desks import service
from hokma.desks.errors import DeskError


def sim_accounts(db):
    with db.read() as u:
        sim = next(b for b in u.broker.list_brokers() if b.kind == "sim")
        return sim, u.broker.list_accounts(sim.id)


def test_new_demo_account_is_created_assigned_and_gives_the_capital(desks_db):
    desk = service.create_desk("Futuros NQ", account={"mode": "new", "initial_capital": 25000}, db=desks_db)
    sim, accounts = sim_accounts(desks_db)
    assert [a.name for a in accounts] == ["Futuros NQ demo"] and accounts[0].initial_capital == 25000
    assert desk["capital"] == 25000
    assert desk["accounts"][0]["id"] == accounts[0].id and desk["accounts"][0]["broker"] == sim.name
    assert desk["accounts"][0]["initial_capital"] == 25000
    with desks_db.read() as u:
        assert u.broker.account_of_strategy(desk["id"], "principal").id == accounts[0].id


def test_a_taken_account_name_gets_a_number(desks_db):
    service.create_desk("Uno", account={"mode": "new", "initial_capital": 1000}, db=desks_db)
    with desks_db.write() as u:
        sim = next(b for b in u.broker.list_brokers() if b.kind == "sim")
        u.broker.create_account(sim.id, "Dos demo", 500)
    second = service.create_desk("Dos", account={"mode": "new", "initial_capital": 2000}, db=desks_db)
    assert second["accounts"][0]["name"] == "Dos demo 2"


def test_existing_account_is_assigned_and_nothing_else_is_created(desks_db):
    with desks_db.write() as u:
        sim = next(b for b in u.broker.list_brokers() if b.kind == "sim")
        free = u.broker.create_account(sim.id, "Libre", 7000)
    before = len(sim_accounts(desks_db)[1])
    desk = service.create_desk("Mesa", account={"mode": "existing", "account_id": free.id}, db=desks_db)
    assert len(sim_accounts(desks_db)[1]) == before
    assert desk["capital"] == 7000 and desk["accounts"][0]["id"] == free.id


def test_existing_account_of_another_desk_is_shared_with_the_new_one(desks_db):
    first = service.create_desk("Una", account={"mode": "new", "initial_capital": 1000}, db=desks_db)
    second = service.create_desk("Otra", account={"mode": "existing", "account_id": first["accounts"][0]["id"]}, db=desks_db)    # SPEC-027: many to many
    assert second["accounts"][0]["id"] == first["accounts"][0]["id"] and [a["shared_with"] for a in second["accounts"]] == [[first["id"]]]
    with desks_db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM desks") == 2


def test_missing_account_is_refused(desks_db):
    with pytest.raises(DeskError) as err:
        service.create_desk("Mesa", account={"mode": "existing", "account_id": 9999}, db=desks_db)
    assert err.value.code == "account_not_found"


def test_failure_after_the_account_leaves_no_desk_and_no_account(desks_db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(service, "link_new_desk_account", boom)
    with pytest.raises(RuntimeError):
        service.create_desk("Mesa", account={"mode": "new", "initial_capital": 5000}, db=desks_db)
    with desks_db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM desks") == 0 and u.session.scalar("SELECT count(*) FROM projects") == 0
    assert sim_accounts(desks_db)[1] == []


@pytest.mark.parametrize("account", [{"mode": "new"}, {"mode": "new", "initial_capital": ""}, {"mode": "new", "initial_capital": 50},
                                     {"mode": "new", "initial_capital": True}, {"mode": "other"}, "x",
                                     {"mode": "existing"}, {"mode": "existing", "account_id": "3"}])
def test_invalid_account_requests_create_nothing(desks_db, account):
    with pytest.raises(DeskError) as err:
        service.create_desk("Mesa", account=account, db=desks_db)
    assert err.value.code in ("bad_capital", "bad_account")
    with desks_db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM desks") == 0
    assert sim_accounts(desks_db)[1] == []


def test_without_account_the_desk_stays_without_account(desks_db):
    desk = make_desk(desks_db)
    assert desk["accounts"] == [] and sim_accounts(desks_db)[1] == []


def test_new_desk_keeps_the_guards_of_task_3334(desks_db):
    desk = service.create_desk("Con activos", assets=["AAPL"], account={"mode": "new", "initial_capital": 3000}, db=desks_db)
    with desks_db.read() as u:
        assert u.broker.get_limits(desk["id"]).get("guards") == ["whitelist"]
