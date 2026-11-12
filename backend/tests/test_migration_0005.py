"""Migration 0005 (task 3083): conversations.project_mode and profile.no_active_project. B-01.

A database as 0.0.6 left it (migrations 1 to 4) with conversations that have a project and conversations that have
none is migrated: every row keeps its project_id and gets project_mode 'follow', so nothing moves. Runs on SQLite and,
when HOKMA_TEST_POSTGRES_URL points to a server, on Postgres too.
"""
import shutil
import sqlite3

from pgsupport import pg_admin_url, pg_url  # noqa: F401  (fixtures)

from hokma.db import migrate
from hokma.db.migrate import MIGRATIONS_DIR
from hokma.db.postgres import PostgresBackend

OLD = ("0001_base.sql", "0002_chat_state.sql", "0003_permissions_trash.sql", "0004_guard_session.sql")


def old_migrations(tmp_path):
    directory = tmp_path / "v0.0.6-migrations"
    directory.mkdir()
    for name in OLD:
        shutil.copy(MIGRATIONS_DIR / name, directory / name)
    return directory


def seed(run):
    """Two projects and four conversations: with a project, without one, archived, pinned."""
    now = "2026-01-01T00:00:00Z"
    run("INSERT INTO projects (slug, name, path, kind, status, created_at, updated_at) "
        "VALUES ('alpha', 'Alpha', '/p/alpha', 'other', 'active', :n, :n)", {"n": now})
    run("INSERT INTO projects (slug, name, path, kind, status, created_at, updated_at) "
        "VALUES ('beta', 'Beta', '/p/beta', 'other', 'active', :n, :n)", {"n": now})
    run("INSERT INTO conversations (engine, title, project_id, started_at) VALUES ('claude_code', 'with alpha', 1, :n)", {"n": now})
    run("INSERT INTO conversations (engine, title, project_id, started_at) VALUES ('claude_code', 'with beta', 2, :n)", {"n": now})
    run("INSERT INTO conversations (engine, title, project_id, started_at) VALUES ('claude_code', 'none', NULL, :n)", {"n": now})
    run("INSERT INTO conversations (engine, title, project_id, pinned, archived, started_at) "
        "VALUES ('claude_code', 'old', NULL, 1, 1, :n)", {"n": now})


def test_b01_sqlite_existing_conversations_keep_their_project_and_follow(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "data" / "hokma.db"
    migrate(path, migrations_dir=old_migrations(tmp_path), seed=False)
    con = sqlite3.connect(str(path))
    seed(lambda sql, params: con.execute(sql.replace(":n", "?"), (params["n"],) * sql.count(":n")))
    con.commit()
    before = con.execute("SELECT id, project_id, title, pinned, archived FROM conversations ORDER BY id").fetchall()
    con.close()

    result = migrate(path, seed=False)
    assert result.applied == [5, 6, 7] and result.backup is not None

    con = sqlite3.connect(str(path))
    after = con.execute("SELECT id, project_id, title, pinned, archived FROM conversations ORDER BY id").fetchall()
    assert after == before == [(1, 1, "with alpha", 0, 0), (2, 2, "with beta", 0, 0), (3, None, "none", 0, 0), (4, None, "old", 1, 1)]
    assert [r[0] for r in con.execute("SELECT project_mode FROM conversations ORDER BY id")] == ["follow"] * 4
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    # the column is additive and constrained
    try:
        con.execute("UPDATE conversations SET project_mode = 'other' WHERE id = 1")
        raise AssertionError("project_mode accepted a value outside follow|fixed")
    except sqlite3.IntegrityError:
        pass
    # the profile may exist before the migration: the new flag defaults to the 0.0.6 rule
    assert con.execute("SELECT count(*) FROM pragma_table_info('profile') WHERE name = 'no_active_project'").fetchone()[0] == 1
    con.close()
    assert migrate(path, seed=False).applied == []              # idempotent


def test_b01_postgres_same_result(pg_url, tmp_path):
    backend = PostgresBackend(pg_url)
    migrate(backend=backend, migrations_dir=old_migrations(tmp_path), seed=False)
    session = backend.connect()
    try:
        seed(lambda sql, params: session.execute(sql, params))
        before = session.query("SELECT id, project_id, title, pinned, archived FROM conversations ORDER BY id")
    finally:
        session.close()

    result = migrate(backend=backend, seed=False)
    assert result.applied == [5, 6, 7]

    session = backend.connect()
    try:
        after = session.query("SELECT id, project_id, title, pinned, archived FROM conversations ORDER BY id")
        assert after == before and len(after) == 4
        assert [r["project_mode"] for r in session.query("SELECT project_mode FROM conversations ORDER BY id")] == ["follow"] * 4
        assert session.scalar("SELECT count(*) FROM information_schema.columns "
                              "WHERE table_name = 'profile' AND column_name = 'no_active_project'") == 1
    finally:
        session.close()
    assert migrate(backend=backend, seed=False).applied == []
