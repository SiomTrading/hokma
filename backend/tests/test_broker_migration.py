"""0018_broker.sql (task 3294, C7-5a): additive, seeded, 1:1 desk accounts and cascade on desk delete."""
import shutil
import sqlite3

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from test_db_migrate import _desk, _desk_project, raw

from hokma.db import migrate
from hokma.db.migrate import MIGRATIONS_DIR
from hokma.db.transfer import BROKER_TABLES


def _account(con, broker=1, name="A", capital=1000):
    con.execute("INSERT INTO broker_accounts (broker_id, name, initial_capital) VALUES (?, ?, ?)", (broker, name, capital))
    return con.execute("SELECT id FROM broker_accounts WHERE name = ? AND broker_id = ?", (name, broker)).fetchone()[0]


def test_it_runs_on_a_1_0_4_base_without_losing_rows_and_seeds_the_broker(env):
    old = env / "old16"
    old.mkdir()
    for f in MIGRATIONS_DIR.glob("00*.sql"):
        if int(f.name[:4]) <= 16:
            shutil.copy(f, old / f.name)
    assert migrate(migrations_dir=old, seed=False).version == 16
    con = raw()
    _desk(con, _desk_project(con))
    con.commit()
    before = [con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in ("projects", "desks")]
    con.close()
    assert migrate().applied == [17, 18, 19, 20]
    con = raw()
    assert [con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in ("projects", "desks")] == before
    assert con.execute("SELECT kind, name FROM brokers").fetchall() == [("sim", "Hokma")]
    assert con.execute("SELECT count(*) FROM broker_accounts").fetchone()[0] == 0
    assert migrate().applied == [] and con.execute("SELECT count(*) FROM brokers").fetchone()[0] == 1


def test_the_tables_exist_and_the_limits_hold(env):
    migrate()
    con = raw()
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(BROKER_TABLES) <= names
    for capital in (99, 1_000_000_001):
        with pytest.raises(sqlite3.IntegrityError):
            _account(con, capital=capital)
    first = _account(con)
    with pytest.raises(sqlite3.IntegrityError):                        # UNIQUE (broker_id, name)
        _account(con)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE broker_accounts SET currency = 'EUR' WHERE id = ?", (first,))
    with pytest.raises(sqlite3.IntegrityError):                        # only open and reset in class 7
        con.execute("INSERT INTO account_ledger (account_id, kind, amount) VALUES (?, 'fill', 1)", (first,))


def test_a_desk_has_one_account_and_an_account_serves_one_desk_and_deleting_the_desk_frees_it(env):
    migrate()
    con = raw()
    con.execute("PRAGMA foreign_keys = ON")
    d1, d2 = _desk(con, _desk_project(con, "a")), _desk(con, _desk_project(con, "b"))
    a1, a2 = _account(con, name="A"), _account(con, name="B")
    con.execute("INSERT INTO desk_accounts VALUES (?, ?)", (d1, a1))
    with pytest.raises(sqlite3.IntegrityError):                        # one account per desk
        con.execute("INSERT INTO desk_accounts VALUES (?, ?)", (d1, a2))
    with pytest.raises(sqlite3.IntegrityError):                        # one desk per account
        con.execute("INSERT INTO desk_accounts VALUES (?, ?)", (d2, a1))
    con.execute("DELETE FROM desks WHERE id = ?", (d1,))
    assert con.execute("SELECT count(*) FROM desk_accounts").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM broker_accounts").fetchone()[0] == 2      # the account stays
    con.execute("INSERT INTO desk_accounts VALUES (?, ?)", (d2, a1))
