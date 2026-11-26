"""0025_desk_capital_optional.sql and hokma.db.downgrade.rollback_0025 (SPEC-027, piece 027c, task 3335): a desk may have no capital.
SQLite cannot rebuild `desks` while other tables reference it with ON DELETE RESTRICT, so the data step rewrites the stored definition;
these tests run it on a base that holds data in every table that references desks and check that nothing is lost."""
import shutil
from types import SimpleNamespace

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from test_db_migrate import _desk, _desk_project, raw

from hokma.db import migrate
from hokma.db.downgrade import CapitalRequired, rollback_0024, rollback_0025
from hokma.db.errors import MigrationError, ValidationError
from hokma.db.migrate import MIGRATIONS_DIR, current_version, discover, render
from hokma.db.postgres import PostgresBackend

FP = "f" * 64


def _up_to(env, version):
    folder = env / f"upto{version}"
    if not folder.exists():
        folder.mkdir()
        for f in MIGRATIONS_DIR.glob("00*.sql"):
            if int(f.name[:4]) <= version:
                shutil.copy(f, folder / f.name)
    return migrate(migrations_dir=folder)


def _capital_column(con):
    return next(r for r in con.execute("PRAGMA table_info(desks)") if r[1] == "capital")


def _base_at_24(env):
    """A base at version 24 with data in the tables that reference desks: an account link and a strategy (cascade), an order and a
    proposal (SET NULL and RESTRICT children), agents and a plantel seat."""
    assert _up_to(env, 24).version == 24
    con = raw()
    con.execute("PRAGMA foreign_keys = ON")
    d1, d2 = (_desk(con, _desk_project(con, s)) for s in ("a", "b"))
    con.execute("INSERT INTO broker_accounts (broker_id, name, initial_capital) VALUES (1, 'A', 1000)")
    a1 = con.execute("SELECT id FROM broker_accounts WHERE name = 'A'").fetchone()[0]
    con.execute("INSERT INTO desk_account_links (desk_id, account_id) VALUES (?, ?)", (d1, a1))
    con.execute("INSERT INTO desk_strategy_accounts VALUES (?, 'principal', ?)", (d1, a1))
    con.execute("INSERT INTO broker_orders (desk_id, account_id, broker_kind, action) VALUES (?, ?, 'sim', 'place')", (d1, a1))
    con.execute("INSERT INTO exec_proposals (desk_id, line, origin, level, proposal_json, fingerprint, status, created_by) "
                "VALUES (?, 'DT', 'desk', 1, '{}', ?, 'analysis_only', 'test')", (d1, FP))
    con.commit()
    return con, (d1, d2), a1


def _snapshot(con):
    tables = ("desks", "desk_agents", "desk_account_links", "desk_strategy_accounts", "broker_orders", "exec_proposals", "desk_assets")
    return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in tables}


def test_0025_is_known_and_has_a_postgres_step_only(env):
    assert 25 in [v for v, _, _ in discover()]
    text = (MIGRATIONS_DIR / "0025_desk_capital_optional.sql").read_text(encoding="utf-8")
    assert "ALTER COLUMN capital DROP NOT NULL" in render(text, SimpleNamespace(engine="postgres", marks=PostgresBackend.marks))
    assert "DROP NOT NULL" not in render(text, SimpleNamespace(engine="sqlite", marks={}))


def test_the_migration_keeps_every_row_of_desks_and_of_what_references_it_and_accepts_a_null_capital(env):
    con, (d1, d2), a1 = _base_at_24(env)
    assert _capital_column(con)[3] == 1                                         # NOT NULL before
    before = _snapshot(con)
    con.close()
    assert _up_to(env, 25).applied == [25]
    con = raw()
    con.execute("PRAGMA foreign_keys = ON")
    assert _capital_column(con)[3] == 0 and _snapshot(con) == before            # nothing lost, nothing moved
    assert con.execute("PRAGMA foreign_key_check").fetchall() == [] and con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    d3 = _desk(con, _desk_project(con, "c"))
    con.execute("UPDATE desks SET capital = NULL WHERE id = ?", (d3,))
    assert con.execute("SELECT capital FROM desks WHERE id = ?", (d3,)).fetchone()[0] is None
    with pytest.raises(Exception):                                              # the range check stays for a number
        con.execute("UPDATE desks SET capital = 5 WHERE id = ?", (d3,))
    con.rollback()
    assert _up_to(env, 25).applied == []                                              # idempotent


def test_deleting_a_desk_still_cascades_after_the_migration(env):
    con, (d1, d2), a1 = _base_at_24(env)
    con.close()
    _up_to(env, 25)
    con = raw()
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("DELETE FROM desk_strategy_accounts WHERE desk_id = ?", (d1,))
    con.execute("DELETE FROM desks WHERE id = ?", (d2,))
    assert con.execute("SELECT count(*) FROM desks").fetchone()[0] == 1
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):                                 # the RESTRICT of the proposals still protects the desk
        con.execute("DELETE FROM desks WHERE id = ?", (d1,))


def test_the_repo_takes_a_desk_without_capital_and_still_refuses_a_bad_number(env, db):
    with db.write() as u:
        project = u.projects.create("Mesa", str(env / "p"), slug="mesa", kind="strategy")
        desk = u.desks.create(project.id, "Mesa", template="custom")
        assert desk.capital is None
        assert u.desks.update(desk.id, capital=5000).capital == 5000
        assert u.desks.update(desk.id, capital=None).capital is None
        with pytest.raises(ValidationError):
            u.desks.update(desk.id, capital=10)
        with pytest.raises(ValidationError):
            u.desks.create(project.id, "Otra", capital=50)


def test_the_way_back_from_25_refuses_while_a_desk_has_no_capital_and_restores_not_null_after(env):
    con, (d1, d2), a1 = _base_at_24(env)
    con.close()
    _up_to(env, 25)
    con = raw()
    con.execute("UPDATE desks SET capital = NULL WHERE id = ?", (d2,))
    con.commit()
    con.close()
    with pytest.raises(CapitalRequired) as raised:
        rollback_0025()
    assert raised.value.desk_ids == [d2] and str(d2) in str(raised.value) and current_version() == 25
    con = raw()
    assert _capital_column(con)[3] == 0
    con.execute("UPDATE desks SET capital = 800 WHERE id = ?", (d2,))
    con.commit()
    before = _snapshot(con)
    con.close()
    assert rollback_0025() == 24 and current_version() == 24
    con = raw()
    assert _capital_column(con)[3] == 1 and _snapshot(con) == before
    assert con.execute("PRAGMA foreign_key_check").fetchall() == [] and con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert rollback_0024() == 23                                                # the whole way back still works
    with pytest.raises(MigrationError):
        rollback_0025()
    assert _up_to(env, 25).applied == [24, 25]                                        # and forward again
