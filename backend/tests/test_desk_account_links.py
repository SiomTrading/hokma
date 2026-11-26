"""0024_desk_account_links.sql and hokma.db.downgrade (SPEC-027, piece 027a, task 3335): many accounts per desk, many desks per
account, a strategy trades through one account. Old desks keep their account and their 'principal' strategy; the way back is
tested and refuses when an account is shared."""
import shutil
import sqlite3
from types import SimpleNamespace

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from test_db_migrate import _desk, _desk_project, raw

from hokma.db import get_database, migrate
from hokma.db.downgrade import RollbackRefused, downgrade, rollback_0024
from hokma.db.errors import MigrationError, ValidationError
from hokma.db.migrate import MIGRATIONS_DIR, current_version, discover, render
from hokma.db.postgres import PostgresBackend
from hokma.db.repos.broker import PRINCIPAL, AccountInUseByStrategy, AlreadyLinked, NotLinked
from hokma.db.transfer import BROKER_TABLES
from account_links_support import link_principal, unlink_all

FP = "f" * 64


def _up_to(env, version):
    """Migrate a fresh or older base to `version` only (later migrations are not applied), so these tests stay about 0024."""
    folder = env / f"upto{version}"
    if not folder.exists():
        folder.mkdir()
        for f in MIGRATIONS_DIR.glob("00*.sql"):
            if int(f.name[:4]) <= version:
                shutil.copy(f, folder / f.name)
    return migrate(migrations_dir=folder)


def _account(con, name="A", capital=1000):
    con.execute("INSERT INTO broker_accounts (broker_id, name, initial_capital) VALUES (1, ?, ?)", (name, capital))
    return con.execute("SELECT id FROM broker_accounts WHERE name = ?", (name,)).fetchone()[0]


def _base_at_23(env, *, shared=False):
    """A base at version 23 with two desks that have one account each, a desk with none, and an order and a proposal of the first desk."""
    old = env / "old23"
    old.mkdir()
    for f in MIGRATIONS_DIR.glob("00*.sql"):
        if int(f.name[:4]) <= 23:
            shutil.copy(f, old / f.name)
    assert migrate(migrations_dir=old).version == 23
    con = raw()
    con.execute("PRAGMA foreign_keys = ON")
    d1, d2, d3 = (_desk(con, _desk_project(con, s)) for s in ("a", "b", "c"))
    a1, a2 = _account(con, "A"), _account(con, "B")
    con.execute("INSERT INTO desk_accounts VALUES (?, ?)", (d1, a1))
    con.execute("INSERT INTO desk_accounts VALUES (?, ?)", (d2, a2))
    con.execute("INSERT INTO broker_orders (desk_id, account_id, broker_kind, action) VALUES (?, ?, 'sim', 'place')", (d1, a1))
    con.execute("INSERT INTO exec_proposals (desk_id, line, origin, level, proposal_json, fingerprint, status, created_by) "
                "VALUES (?, 'DT', 'desk', 1, '{}', ?, 'analysis_only', 'test')", (d1, FP))
    con.commit()
    return con, (d1, d2, d3), (a1, a2)


def _names(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def test_0024_exists_and_the_new_tables_are_in_the_transfer_list(env):
    assert 24 in [v for v, _, _ in discover()]
    assert {"desk_account_links", "desk_strategy_accounts"} <= set(BROKER_TABLES) and "desk_accounts" not in BROKER_TABLES


def test_old_desks_keep_their_account_and_get_the_principal_strategy(env):
    con, (d1, d2, d3), (a1, a2) = _base_at_23(env)
    orders_before = con.execute("SELECT * FROM broker_orders").fetchall()
    con.close()
    assert _up_to(env, 24).applied == [24]
    con = raw()
    assert "desk_accounts" not in _names(con)
    assert con.execute("SELECT desk_id, account_id, added_by FROM desk_account_links ORDER BY desk_id").fetchall() == [(d1, a1, "owner"), (d2, a2, "owner")]
    assert con.execute("SELECT desk_id, strategy_key, account_id FROM desk_strategy_accounts ORDER BY desk_id").fetchall() == [(d1, "principal", a1), (d2, "principal", a2)]
    assert con.execute("SELECT count(*) FROM desk_account_links WHERE desk_id = ?", (d3,)).fetchone()[0] == 0     # a desk without account stays without
    assert [r[:len(orders_before[0])] for r in con.execute("SELECT * FROM broker_orders")] == [tuple(o) for o in orders_before]     # history is untouched
    assert con.execute("SELECT account_id, strategy_key FROM exec_proposals").fetchall() == [(None, None)]
    assert con.execute("PRAGMA foreign_key_check").fetchall() == [] and con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert _up_to(env, 24).applied == []


def test_the_first_account_of_a_desk_ties_its_principal_strategy_and_unlinking_frees_both(env, db):
    con = raw()
    d1, d2 = _desk(con, _desk_project(con, "a")), _desk(con, _desk_project(con, "b"))
    a1 = _account(con)
    con.commit()
    with db.write() as u:
        assert u.broker.account_of_strategy(d1, PRINCIPAL) is None and u.broker.desks_of_account(a1) == []
        link_principal(u, d1, a1)
        assert u.broker.account_of_strategy(d1, PRINCIPAL).id == a1 and u.broker.desks_of_account(a1) == [d1]
        unlink_all(u, d1)
        assert u.broker.account_of_strategy(d1, PRINCIPAL) is None and u.broker.desks_of_account(a1) == []
        link_principal(u, d2, a1)                                              # free again


def test_an_account_can_be_shared_and_a_strategy_ties_to_one_account_of_its_desk(env, db):
    con = raw()
    d1, d2 = _desk(con, _desk_project(con, "a")), _desk(con, _desk_project(con, "b"))
    a1, a2 = _account(con, "A"), _account(con, "B")
    con.commit()
    with db.write() as u:
        b = u.broker
        b.link(d1, a1)
        b.link(d2, a1, added_by="chat")                                         # C5: the same account in a second desk
        b.link(d1, a2)
        assert b.desks_of_account(a1) == [d1, d2] and [a.id for a in b.accounts_of_desk(d1)] == [a1, a2]
        with pytest.raises(AlreadyLinked):
            b.link(d1, a1)
        with pytest.raises(ValidationError):
            b.link(d1, a2, added_by="robot")
        assert b.account_of_strategy(d1, "momentum") is None
        b.bind_strategy(d1, "momentum", a2)
        assert b.account_of_strategy(d1, "momentum").id == a2 and b.strategy_bindings(d1) == {"momentum": a2}
        b.bind_strategy(d1, "momentum", a1)                                     # an upsert: the strategy moves
        assert b.strategy_bindings(d1) == {"momentum": a1}
        with pytest.raises(NotLinked):                                          # an account that is not in the desk
            b.bind_strategy(d2, "momentum", a2)
        for bad in ("", "x" * 81):
            with pytest.raises(ValidationError):
                b.bind_strategy(d1, bad, a1)
        with pytest.raises(AccountInUseByStrategy) as raised:                   # C8 at the repo
            b.unlink(d1, a1)
        assert raised.value.code == "account_in_use_by_strategy"
        assert b.unlink(d1, a2) is True and b.unlink(d1, a2) is False
        b.unbind_strategy(d1, "momentum")
        assert b.unlink(d1, a1) is True and b.desks_of_account(a1) == [d2]


def test_the_database_refuses_to_drop_a_link_a_strategy_uses_but_deleting_the_desk_works(env, db):
    con = raw()
    con.execute("PRAGMA foreign_keys = ON")
    d1 = _desk(con, _desk_project(con, "a"))
    a1 = _account(con)
    con.execute("INSERT INTO desk_account_links (desk_id, account_id) VALUES (?, ?)", (d1, a1))
    con.execute("INSERT INTO desk_strategy_accounts VALUES (?, 'principal', ?)", (d1, a1))
    with pytest.raises(sqlite3.IntegrityError):                                 # no strategy on an account that is not linked
        con.execute("INSERT INTO desk_strategy_accounts VALUES (?, 'other', ?)", (d1, a1 + 100))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM desk_account_links WHERE desk_id = ?", (d1,))
    con.execute("DELETE FROM desks WHERE id = ?", (d1,))
    assert con.execute("SELECT count(*) FROM desk_account_links").fetchone()[0] == con.execute("SELECT count(*) FROM desk_strategy_accounts").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):                                 # the account cannot be deleted while a desk holds it
        d2 = _desk(con, _desk_project(con, "b"))
        con.execute("INSERT INTO desk_account_links (desk_id, account_id) VALUES (?, ?)", (d2, a1))
        con.execute("DELETE FROM broker_accounts WHERE id = ?", (a1,))


def test_the_way_back_restores_desk_accounts_and_forward_again_gives_the_same_links(env):
    con, (d1, d2, d3), (a1, a2) = _base_at_23(env)
    con.close()
    _up_to(env, 24)
    con = raw()
    con.execute("DELETE FROM desk_strategy_accounts WHERE desk_id = ?", (d2,))        # the way back does not need the strategies
    links = con.execute("SELECT desk_id, account_id FROM desk_account_links ORDER BY desk_id").fetchall()
    con.commit()
    con.close()
    assert rollback_0024() == 23 and current_version() == 23
    con = raw()
    names = _names(con)
    assert "desk_accounts" in names and not {"desk_account_links", "desk_strategy_accounts"} & names
    assert con.execute("SELECT desk_id, account_id FROM desk_accounts ORDER BY desk_id").fetchall() == links
    assert "account_id" not in _columns(con, "exec_proposals") and "strategy_key" not in _columns(con, "exec_proposals")
    assert "strategy_key" not in _columns(con, "broker_orders")
    assert con.execute("SELECT count(*) FROM broker_orders").fetchone()[0] == con.execute("SELECT count(*) FROM exec_proposals").fetchone()[0] == 1
    assert con.execute("PRAGMA foreign_key_check").fetchall() == [] and con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT count(*) FROM schema_version WHERE version = 24").fetchone()[0] == 0
    con.close()
    assert _up_to(env, 24).applied == [24]
    con = raw()
    assert con.execute("SELECT desk_id, account_id FROM desk_account_links ORDER BY desk_id").fetchall() == links


@pytest.mark.parametrize("shape", ["account_on_two_desks", "desk_with_two_accounts"])
def test_the_way_back_refuses_with_the_list_of_what_blocks_it_and_changes_nothing(env, shape):
    con, (d1, d2, d3), (a1, a2) = _base_at_23(env)
    con.close()
    _up_to(env, 24)
    con = raw()
    if shape == "account_on_two_desks":
        con.execute("INSERT INTO desk_account_links (desk_id, account_id) VALUES (?, ?)", (d2, a1))
        expected = ("account_on_many_desks", (d1, d2), (a1,))
    else:
        con.execute("INSERT INTO desk_account_links (desk_id, account_id) VALUES (?, ?)", (d1, a2))
        expected = ("desk_with_many_accounts", (d1,), (a1, a2))
    con.commit()
    before = con.execute("SELECT desk_id, account_id FROM desk_account_links ORDER BY 1, 2").fetchall()
    con.close()
    with pytest.raises(RollbackRefused) as raised:
        rollback_0024()
    kinds = [(b.kind, b.desk_ids, b.account_ids) for b in raised.value.blockers]
    assert expected in kinds and all(str(x) in str(raised.value) for x in (d1, a1))
    assert current_version() == 24
    con = raw()
    assert "desk_accounts" not in _names(con) and {"desk_account_links", "desk_strategy_accounts"} <= _names(con)
    assert con.execute("SELECT desk_id, account_id FROM desk_account_links ORDER BY 1, 2").fetchall() == before
    assert {"account_id", "strategy_key"} <= set(_columns(con, "exec_proposals"))


def test_the_way_back_needs_version_24_and_a_failure_in_it_rolls_everything_back(env, monkeypatch):
    _up_to(env, 24)
    from hokma.db import downgrade as module

    def broken(session):
        session.execute("DROP TABLE desk_strategy_accounts")
        raise RuntimeError("boom")

    real = module.downgrade
    monkeypatch.setattr(module, "downgrade", broken)
    with pytest.raises(RuntimeError):
        rollback_0024()
    assert current_version() == 24 and "desk_strategy_accounts" in _names(raw())          # the DROP was rolled back too
    monkeypatch.setattr(module, "downgrade", real)
    assert rollback_0024() == 23
    with pytest.raises(MigrationError):                                                   # already at 23
        rollback_0024()


def test_the_postgres_script_renders_without_leftover_marks_and_keeps_the_composite_key(env):
    text = (MIGRATIONS_DIR / "0024_desk_account_links.sql").read_text(encoding="utf-8")
    script = render(text, SimpleNamespace(engine="postgres", marks=PostgresBackend.marks))
    assert "{TS}" not in script and "{PK}" not in script and "TIMESTAMPTZ" in script
    assert "PRIMARY KEY (desk_id, strategy_key)" in script and "FOREIGN KEY (desk_id, account_id) REFERENCES desk_account_links (desk_id, account_id)" in script
    assert "DROP TABLE desk_accounts" in script and "ADD COLUMN account_id INTEGER;" in script
