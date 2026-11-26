"""Migration of the backtest tables (SPEC-020, H4): idempotent, recorded in schema_version, append-only trial ledger, known to the transfer."""
import sqlite3

import pytest
from dbsupport import env  # noqa: F401  (fixture)

from hokma.db import current_version, db_path, migrate
from hokma.db import transfer
from hokma.db.backend import SqliteBackend
from hokma.db.migrate import MIGRATIONS_DIR, discover, render


VERSION = int(next(p.name for p in MIGRATIONS_DIR.glob("*_backtest.sql"))[:4])


def test_applying_the_migrations_twice_changes_nothing_and_backtest_is_in_schema_version(env):
    first = migrate()
    assert VERSION in first.applied
    con = sqlite3.connect(str(db_path()))
    before = sorted(r[0] for r in con.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    assert migrate().applied == [] and current_version() == discover()[-1][0]
    after = sorted(r[0] for r in sqlite3.connect(str(db_path())).execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    assert before == after
    assert con.execute("SELECT COUNT(*) FROM schema_version WHERE version = ?", (VERSION,)).fetchone()[0] == 1


def test_the_transfer_knows_every_table_of_the_migration():
    script = "\n".join(render(p.read_text(encoding="utf-8"), SqliteBackend(p)) for _v, _n, p in discover())
    plan = [t.name for t in transfer.table_plan(script)]
    assert set(transfer.BACKTEST_TABLES) <= set(plan)
    assert plan.index("backtest_trials") > plan.index("backtest_runs") and plan.index("backtest_trades") > plan.index("backtest_runs")


def test_the_migration_creates_nothing_but_new_tables():
    text = (MIGRATIONS_DIR / f"{VERSION:04d}_backtest.sql").read_text(encoding="utf-8")
    statements = [line.strip().upper() for line in text.splitlines() if line.strip() and not line.strip().startswith("--")]
    assert not [s for s in statements if s.startswith(("ALTER ", "DROP ", "DELETE ", "UPDATE ", "INSERT "))]


def test_the_trial_ledger_is_append_only(env):
    migrate()
    con = sqlite3.connect(str(db_path()))
    con.execute("INSERT INTO backtest_trials (strategy_id, hypothesis_id, params_json, status) VALUES ('s', 'h', '{}', 'failed')")
    con.commit()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE backtest_trials SET status = 'done'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM backtest_trials")


def test_a_root_cause_on_a_spec_is_real_text_of_20_to_4000_characters(env):
    migrate()
    con = sqlite3.connect(str(db_path()))
    row = "INSERT INTO strategy_specs (strategy_id, version, line, spec_yaml, spec_hash, root_cause) VALUES ('s', ?, 'DT', 'y', 'h', ?)"
    con.execute(row, (1, None))
    con.execute(row, (2, "x" * 20))
    con.execute(row, (3, "y" * 4000))
    for version, bad in ((4, ""), (5, " " * 30), (6, "too short"), (7, "z" * 4001)):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(row, (version, bad))
