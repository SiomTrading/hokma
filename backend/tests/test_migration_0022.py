"""Migration 0022 (SPEC-018, OA-36): idempotent, recorded in schema_version, and the transfer knows its tables."""
import sqlite3

from dbsupport import env  # noqa: F401  (fixture)

from hokma.db import current_version, db_path, migrate
from hokma.db import transfer
from hokma.db.migrate import MIGRATIONS_DIR, discover, render
from hokma.db.backend import SqliteBackend


def test_applying_the_migrations_twice_changes_nothing_and_0022_is_in_schema_version(env):
    first = migrate()
    assert 22 in first.applied
    con = sqlite3.connect(str(db_path()))
    before = sorted(r[0] for r in con.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    assert migrate().applied == [] and current_version() == discover()[-1][0]
    after = sorted(r[0] for r in sqlite3.connect(str(db_path())).execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    assert before == after
    assert con.execute("SELECT COUNT(*) FROM schema_version WHERE version = 22").fetchone()[0] == 1


def test_the_transfer_knows_every_table_of_the_migration():
    script = "\n".join(render(p.read_text(encoding="utf-8"), SqliteBackend(p)) for _v, _n, p in discover())
    plan = [t.name for t in transfer.table_plan(script)]
    assert set(transfer.EXEC_TABLES) <= set(plan)
    assert plan.index("recon_breaks") > plan.index("reconciliations") and plan.index("sim_fills") > plan.index("sim_orders")


def test_the_migration_creates_nothing_but_new_tables():
    text = (MIGRATIONS_DIR / "0022_execution.sql").read_text(encoding="utf-8")
    statements = [line.strip().upper() for line in text.splitlines() if line.strip() and not line.strip().startswith("--")]
    assert not [s for s in statements if s.startswith(("ALTER ", "DROP ", "DELETE ", "UPDATE ", "INSERT "))]
