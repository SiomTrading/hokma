"""Migration 0036 (SPEC-030, A1): the strategy bank and its sources. Additive, idempotent, checked, append only events, known to the transfer."""
import sqlite3

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.db import current_version, db_path, migrate
from hokma.db import transfer
from hokma.db.backend import SqliteBackend
from hokma.db.migrate import MIGRATIONS_DIR, discover, render

VERSION = int(next(p.name for p in MIGRATIONS_DIR.glob("*_strategy_bank.sql"))[:4])
H = "a" * 64


def con_of():
    con = sqlite3.connect(str(db_path()))
    con.execute("PRAGMA foreign_keys = ON")
    return con


def source(con, kind="text", sha=H):
    return con.execute("INSERT INTO strategy_sources (kind, reference, rights, content_sha256, created_by) VALUES (?, 'ref', 'own', ?, 'ian')",
                       (kind, sha)).lastrowid


def bank(con, slug="s1", status="idea", reason=None):
    return con.execute("INSERT INTO strategy_bank (slug, title, status, close_reason) VALUES (?, 't', ?, ?)", (slug, status, reason)).lastrowid


VERSION_SQL = ("INSERT INTO strategy_bank_versions (bank_id, version, source_id, extraction_json, extraction_hash, needs_rule_json, "
               "strategy_id, spec_version, created_by) VALUES (?, ?, ?, '{}', 'h', ?, ?, ?, 'ian')")


def test_the_migration_is_applied_once_the_five_tables_exist_and_start_empty(env):
    assert VERSION in migrate().applied
    assert migrate().applied == [] and current_version() == discover()[-1][0]
    con = con_of()
    for table in transfer.BANK_TABLES:
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM schema_version WHERE version = ?", (VERSION,)).fetchone()[0] == 1


def test_the_migration_creates_nothing_but_new_tables_and_leaves_0001_to_0024_alone():
    text = (MIGRATIONS_DIR / f"{VERSION:04d}_strategy_bank.sql").read_text(encoding="utf-8")
    statements = [line.strip().upper() for line in text.splitlines() if line.strip() and not line.strip().startswith("--")]
    assert not [s for s in statements if s.startswith(("ALTER ", "DROP ", "DELETE ", "UPDATE ", "INSERT "))]
    assert VERSION > 24 and [v for v, _n, _p in discover() if v < VERSION][:24] == list(range(1, 25))


def test_the_older_tables_are_byte_identical_after_the_new_migration(env, tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    for f in MIGRATIONS_DIR.glob("00*.sql"):
        if int(f.name[:4]) < VERSION:
            (old / f.name).write_bytes(f.read_bytes())
    db_old = tmp_path / "old.db"
    migrate(db_old, migrations_dir=old, seed=False)
    db_new = tmp_path / "new.db"
    migrate(db_new, seed=False)

    def tables(path):
        c = sqlite3.connect(str(path))
        return {n: s for n, s in c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")}

    before, after = tables(db_old), tables(db_new)
    assert {n: s for n, s in after.items() if n in before} == before
    assert set(after) - set(before) - set(transfer.USER_RULE_TABLES) - set(transfer.RULE_BRIEF_TABLES) == set(transfer.BANK_TABLES)      # 0037 and 0038 add their own tables on top


def test_the_transfer_knows_every_table_in_dependency_order():
    script = "\n".join(render(p.read_text(encoding="utf-8"), SqliteBackend(p)) for _v, _n, p in discover())
    plan = [t.name for t in transfer.table_plan(script)]
    assert set(transfer.BANK_TABLES) <= set(plan)
    at = plan.index
    assert at("strategy_sources") < at("strategy_source_passages") and at("strategy_sources") < at("strategy_bank_versions")
    assert at("strategy_bank") < at("strategy_bank_versions") and at("strategy_bank") < at("strategy_bank_events")


def test_a_status_outside_the_seven_is_refused(env):
    migrate()
    con = con_of()
    for status in ("idea", "draft", "testing", "ready", "live", "off"):
        bank(con, slug=status, status=status)
    bank(con, slug="closed", status="not_working", reason="r" * 20)
    with pytest.raises(sqlite3.IntegrityError):
        bank(con, slug="x", status="running")


def test_not_working_needs_a_reason_of_20_to_4000_characters_and_only_it_has_one(env):
    migrate()
    con = con_of()
    for slug, status, reason in (("a", "not_working", None), ("b", "not_working", "short"), ("c", "not_working", " " * 30),
                                 ("d", "not_working", "z" * 4001), ("e", "draft", "r" * 20)):
        with pytest.raises(sqlite3.IntegrityError):
            bank(con, slug=slug, status=status, reason=reason)
    bank(con, slug="ok", status="not_working", reason="y" * 4000)


def test_a_version_with_a_pending_rule_cannot_carry_a_spec(env):
    migrate()
    con = con_of()
    sid, b = source(con), bank(con)
    con.execute(VERSION_SQL, (b, 1, sid, "[]", "s", 1))                                  # a spec and no pending rule: fine
    con.execute(VERSION_SQL, (b, 2, sid, '[{"rule": "x"}]', None, None))                 # a pending rule and no spec: fine
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(VERSION_SQL, (b, 3, sid, '[{"rule": "x"}]', "s", 1))


def test_a_strategy_id_goes_with_its_spec_version_and_the_version_number_is_unique(env):
    migrate()
    con = con_of()
    sid, b = source(con), bank(con)
    for number, strategy_id, spec_version in ((1, "s", None), (2, None, 3)):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(VERSION_SQL, (b, number, sid, "[]", strategy_id, spec_version))
    con.execute(VERSION_SQL, (b, 1, sid, "[]", None, None))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(VERSION_SQL, (b, 1, sid, "[]", None, None))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE strategy_bank_versions SET evidence_stage = 'proven'")


def test_a_source_is_unique_by_kind_and_hash_and_its_limits_hold(env):
    migrate()
    con = con_of()
    source(con)
    source(con, kind="link")
    with pytest.raises(sqlite3.IntegrityError):
        source(con)
    with pytest.raises(sqlite3.IntegrityError):
        source(con, kind="podcast", sha="b" * 64)
    with pytest.raises(sqlite3.IntegrityError):
        source(con, sha="short")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO strategy_sources (kind, reference, rights, content_sha256, created_by) VALUES ('text', ' ', 'own', ?, 'x')", ("c" * 64,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO strategy_sources (kind, reference, rights, content_sha256, created_by) VALUES ('text', 'r', 'mine', ?, 'x')", ("d" * 64,))


def test_the_passages_of_a_source_go_with_it_and_their_order_is_unique(env):
    migrate()
    con = con_of()
    sid = source(con)
    row = "INSERT INTO strategy_source_passages (source_id, ord, locator, body, body_sha256) VALUES (?, ?, ?, 'b', ?)"
    con.execute(row, (sid, 0, "page 1", H))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(row, (sid, 0, "page 2", H))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(row, (sid, 1, "", H))
    con.execute("DELETE FROM strategy_sources WHERE id = ?", (sid,))
    assert con.execute("SELECT count(*) FROM strategy_source_passages").fetchone()[0] == 0


def test_a_source_in_use_by_a_version_cannot_be_deleted(env):
    migrate()
    con = con_of()
    sid, b = source(con), bank(con)
    con.execute(VERSION_SQL, (b, 1, sid, "[]", None, None))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM strategy_sources WHERE id = ?", (sid,))


def test_the_bank_events_are_append_only_in_sqlite(env):
    migrate()
    con = con_of()
    b = bank(con)
    con.execute("INSERT INTO strategy_bank_events (bank_id, from_status, to_status, actor) VALUES (?, NULL, 'idea', 'ian')", (b,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE strategy_bank_events SET to_status = 'draft'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM strategy_bank_events")


def test_postgres_has_the_same_checks_and_the_events_are_append_only(pg_url_migrated):
    import psycopg

    with psycopg.connect(pg_url_migrated, autocommit=True) as c:
        c.execute("INSERT INTO strategy_sources (kind, reference, rights, content_sha256, created_by) VALUES ('text', 'r', 'own', %s, 'ian')", (H,))
        c.execute("INSERT INTO strategy_bank (slug, title) VALUES ('s', 't')")
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO strategy_bank (slug, title, status) VALUES ('n', 't', 'not_working')")
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO strategy_bank (slug, title, status) VALUES ('u', 't', 'running')")
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO strategy_bank_versions (bank_id, version, source_id, extraction_json, extraction_hash, needs_rule_json, strategy_id, "
                      "spec_version, created_by) VALUES (1, 1, 1, '{}', 'h', '[1]', 's', 1, 'ian')")
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO strategy_bank_versions (bank_id, version, source_id, extraction_json, extraction_hash, strategy_id, created_by) "
                      "VALUES (1, 1, 1, '{}', 'h', 's', 'ian')")
        c.execute("INSERT INTO strategy_bank_events (bank_id, to_status, actor) VALUES (1, 'idea', 'ian')")
        with pytest.raises(psycopg.Error, match="append-only"):
            c.execute("UPDATE strategy_bank_events SET to_status = 'draft'")
        with pytest.raises(psycopg.Error, match="append-only"):
            c.execute("DELETE FROM strategy_bank_events")
