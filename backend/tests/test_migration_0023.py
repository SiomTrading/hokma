"""Migration 0023 (SPEC-019, H3): additive, idempotent, and the transfer knows its tables. premium_thresholds starts empty."""
import sqlite3

from dbsupport import env  # noqa: F401  (fixture)

from hokma.db import db_path, migrate
from hokma.db import transfer
from hokma.db.backend import SqliteBackend
from hokma.db.migrate import MIGRATIONS_DIR, discover, render


def test_0023_is_applied_once_and_the_new_tables_are_empty(env):
    assert 23 in migrate().applied
    assert migrate().applied == []
    con = sqlite3.connect(str(db_path()))
    for table in transfer.PLANTEL_TABLES:
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM schema_version WHERE version = 23").fetchone()[0] == 1


def test_the_transfer_knows_every_table_of_0023():
    script = "\n".join(render(p.read_text(encoding="utf-8"), SqliteBackend(p)) for _v, _n, p in discover())
    plan = [t.name for t in transfer.table_plan(script)]
    assert set(transfer.PLANTEL_TABLES) <= set(plan)


def test_0023_creates_nothing_but_new_tables():
    text = (MIGRATIONS_DIR / "0023_desk_plantel.sql").read_text(encoding="utf-8")
    statements = [line.strip().upper() for line in text.splitlines() if line.strip() and not line.strip().startswith("--")]
    assert not [s for s in statements if s.startswith(("ALTER ", "DROP ", "DELETE ", "UPDATE ", "INSERT "))]


def test_an_abstention_has_no_value_and_a_value_is_not_an_abstention(env):
    migrate()
    con = sqlite3.connect(str(db_path()))
    con.execute("PRAGMA foreign_keys = OFF")
    ok = ("INSERT INTO desk_signals (desk_id, seat_code, symbol, as_of, value, confidence, abstained, signal_json, snapshot_hash) "
          "VALUES (1, 'A16', 'AAPL', '2026-01-01', ?, 50, ?, '{}', 'h')")
    con.execute(ok, (0.5, 0))
    con.execute(ok, (None, 1))
    for value, abstained in ((0.5, 1), (None, 0)):
        try:
            con.execute(ok, (value, abstained))
        except sqlite3.IntegrityError:
            continue
        raise AssertionError("the abstention rule was not enforced")
