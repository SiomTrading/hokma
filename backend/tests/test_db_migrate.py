"""Migrations: fresh schema, idempotence, backups, rollback, seeds."""
import json
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest
from dbsupport import env  # noqa: F401  (fixture)

from hokma.db import MigrationError, current_version, db_path, get_database, migrate
from hokma.db.migrate import MIGRATIONS_DIR, discover, render
from hokma.db.backend import SqliteBackend
from hokma.db.seeds import parse_frontmatter, seed_agents
from hokma.i18n import t

TABLES = {
    "schema_version", "profile", "license_checks", "agents", "projects", "tasks", "task_links", "task_runs",
    "task_events", "task_comments", "status_transitions", "approvals", "decisions", "journal_entries",
    "guard_events", "bugs", "conversations", "messages", "attachments", "extensions", "extension_requests",
    "scheduled_jobs", "onboarding_progress", "trash_items", "checkpoint_points", "engine_connections",
    "libraries", "documents", "doc_chunks", "doc_chunks_fts", "project_folders", "library_folders", "doc_chunk_vectors",
}


FTS_SHADOW = {"doc_chunks_fts_data", "doc_chunks_fts_idx", "doc_chunks_fts_docsize", "doc_chunks_fts_config"}


def raw(path=None):
    return sqlite3.connect(str(path or db_path()))


def test_fresh_database_has_the_22_tables_plus_onboarding_and_is_healthy(env):
    result = migrate()
    assert result.created and result.applied == [1, 2, 3, 4, 5, 6, 7, 8] and result.version == 8 and result.backup is None
    con = raw()
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
    assert names - FTS_SHADOW == TABLES and len(TABLES) == 33
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    triggers = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"trg_tasks_guard", "trg_tasks_transition", "trg_tasks_touch", "trg_tasks_event_ins", "trg_bugs_transition",
            "trg_approvals_no_upd", "trg_journal_no_del", "trg_events_no_upd", "trg_guard_no_del",
            "trg_comments_no_upd"} <= triggers
    assert con.execute("SELECT count(*) FROM status_transitions WHERE entity = 'bug'").fetchone()[0] == 9


def test_connections_use_foreign_keys_wal_and_busy_timeout(env):
    migrate()
    session = SqliteBackend(db_path()).connect()
    try:
        assert session.scalar("PRAGMA foreign_keys") == 1
        assert session.scalar("PRAGMA journal_mode") == "wal"
        assert session.scalar("PRAGMA busy_timeout") == 5000
    finally:
        session.close()


def test_migrating_twice_changes_nothing(env):
    migrate()
    con = raw()
    dump = list(con.iterdump())
    con.close()
    second = migrate()
    assert second.applied == [] and not second.created and second.backup is None and second.version == 8
    con = raw()
    assert list(con.iterdump()) == dump


def test_current_version(env):
    assert current_version() == 0
    migrate()
    assert current_version() == 8


def make_dir(tmp_path, files: dict[str, str]) -> Path:
    directory = tmp_path / "migs"
    directory.mkdir(exist_ok=True)
    for real in MIGRATIONS_DIR.glob("*.sql"):
        shutil.copy(real, directory / real.name)
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def test_a_new_migration_runs_after_a_backup_of_the_existing_database(env):
    migrate()
    directory = make_dir(env, {"0009_extra.sql": "CREATE TABLE extra ({PK_ID});".replace("{PK_ID}", "id {PK}")})
    result = migrate(migrations_dir=directory)
    assert result.applied == [9] and result.version == 9
    assert result.backup is not None and result.backup.parent == db_path().parent / "backups"
    backup = sqlite3.connect(str(result.backup))
    assert backup.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 8
    assert backup.execute("SELECT count(*) FROM sqlite_master WHERE name = 'extra'").fetchone()[0] == 0
    assert raw().execute("SELECT count(*) FROM sqlite_master WHERE name = 'extra'").fetchone()[0] == 1


def test_a_broken_migration_rolls_back_leaves_the_copy_and_reports(env):
    migrate()
    con = raw()
    before = list(con.iterdump())
    con.close()
    bad = "CREATE TABLE half (id {PK});\nINSERT INTO half (id) VALUES (1);\nCREATE TABLE half (id {PK});"
    directory = make_dir(env, {"0009_bad.sql": bad})
    with pytest.raises(MigrationError) as info:
        migrate(migrations_dir=directory)
    assert info.value.version == 9
    assert str(info.value) == t("db.migrate.failed", file="0009_bad.sql", error=str(info.value.__cause__.args[0]))
    con = raw()
    assert list(con.iterdump()) == before                           # the whole file was rolled back
    assert con.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 8
    backups = list((db_path().parent / "backups").glob("hokma-v8-*.db"))
    assert len(backups) == 1


def test_a_broken_first_migration_leaves_no_schema_version(env):
    directory = env / "only-bad"
    directory.mkdir()
    (directory / "0001_bad.sql").write_text("CREATE TABLE a (id {PK});\nSELECT * FROM missing_table;", encoding="utf-8")
    with pytest.raises(MigrationError):
        migrate(migrations_dir=directory)
    assert current_version() == 0


def test_a_database_newer_than_the_code_is_refused(env):
    directory = make_dir(env, {"0009_extra.sql": "CREATE TABLE extra (id {PK});"})
    migrate(migrations_dir=directory)
    with pytest.raises(MigrationError) as info:
        migrate()                                                   # the code only knows version 8
    assert str(info.value) == t("db.migrate.newer", found=9, known=8)


def test_a_file_that_is_not_a_database_is_reported(env):
    db_path().parent.mkdir(parents=True)
    db_path().write_bytes(b"this is not a sqlite file" * 100)
    with pytest.raises(MigrationError):
        migrate()


def test_old_backups_are_pruned(env):
    migrate()
    for n in range(9, 23):
        directory = make_dir(env, {f"{k:04d}_x{k}.sql": f"CREATE TABLE x{k} (id {{PK}});" for k in range(9, n + 1)})
        migrate(migrations_dir=directory)
    assert len(list((db_path().parent / "backups").glob("hokma-v*.db"))) == 10


def test_engine_sections_and_marks():
    backend = SqliteBackend(Path("unused.db"))
    text = "-- @engine: all\nA {PK} {TS}\n-- @engine: postgres\nPG ONLY\n-- @engine: sqlite\nLITE ONLY\n-- @engine: all\nEND"
    out = render(text, backend)
    assert "A INTEGER PRIMARY KEY AUTOINCREMENT TEXT" in out and "LITE ONLY" in out and "PG ONLY" not in out and "END" in out


def test_bad_file_names_and_duplicate_versions_are_rejected(env):
    directory = env / "m"
    directory.mkdir()
    (directory / "init.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError):
        discover(directory)
    (directory / "init.sql").unlink()
    (directory / "0001_a.sql").write_text("SELECT 1;")
    (directory / "0001_b.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError):
        discover(directory)


def test_two_processes_migrating_at_once_apply_it_once(env):
    errors = []

    def run():
        try:
            migrate()
        except Exception as exc:                                    # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert not errors
    assert raw().execute("SELECT count(*) FROM schema_version").fetchone()[0] == 8


# ------------------------------------------------------------------------------------------------ seeds
def write_agent(directory: Path, slug: str, model="sonnet", name=None, description="Does a job.") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{slug}.md").write_text(
        f"---\nname: {name or slug}\ndisplay_name: {slug.title()}\nslug: {slug}\ndescription: >\n  {description}\n"
        f"  Second line.\nmodel: {model}\ntools: Read\n---\nBody\n", encoding="utf-8")


def test_frontmatter_folded_description_and_values():
    meta = parse_frontmatter("---\nname: joel\ndescription: >\n  one\n  two\nmodel: haiku\n---\nbody: x\n")
    assert meta == {"name": "joel", "description": "one two", "model": "haiku"}
    assert parse_frontmatter("no frontmatter") == {}


def test_agents_are_seeded_from_the_kit_files_and_stay_in_step(env):
    work = env / "work" / "agents"
    write_agent(work, "joel", model="opus")
    write_agent(work, "vera", model="haiku")
    kit = env / "kit"
    write_agent(kit, "joel")
    write_agent(kit, "vera")
    write_agent(kit, "iris")
    migrate()
    db = get_database()
    with db.write() as u:
        seed_agents(u.session, workdir_agents=work, kit_dir=kit)
        agents = {a.slug: a for a in u.agents.list()}
        assert set(agents) >= {"joel", "vera"}
        assert agents["joel"].model_tier in ("deep", "balanced")
        assert agents["joel"].description == "Does a job. Second line."
        u.agents.set_status("joel", "active")
        u.agents.set_model_tier("joel", "fast")
    with db.write() as u:                                           # a second pass changes nothing the owner chose
        before = [a.to_dict() for a in u.agents.list()]
        seed_agents(u.session, workdir_agents=work, kit_dir=kit)
        assert [a.to_dict() for a in u.agents.list()] == before
        joel = u.agents.get("joel")
        assert joel.status == "active" and joel.model_tier == "fast" and not joel.is_custom
    (work / "vera.md").unlink()                                     # the kit no longer brings it: it turns off
    write_agent(work, "my-own", description="Mine.")                # not in the kit: custom
    with db.write() as u:
        u.agents.set_status("vera", "active")
        seed_agents(u.session, workdir_agents=work, kit_dir=kit)
        assert u.agents.get("vera").status == "off"
        own = u.agents.get("my-own")
        assert own.is_custom and own.file_path == "agents/my-own.md"


def test_migrate_seeds_the_agents_of_the_real_kit_template(env):
    migrate()
    with get_database().read() as u:
        slugs = {a.slug for a in u.agents.list()}
        assert {"hokma", "joel", "vera", "axel", "diego", "iris", "hugo", "joel-dev-python"} <= slugs
        assert all(a.status == "off" for a in u.agents.list())
        assert u.agents.get("axel").model_tier == "fast"
        assert "{{" not in (u.agents.get("hokma").description or "")


def test_profile_is_seeded_from_owner_json_without_completing_setup(env):
    data = env / "data"
    data.mkdir(parents=True)
    (data / "owner.json").write_text(json.dumps({"owner_name": "Ana", "fund_name": "Fondo Uno"}), encoding="utf-8")
    migrate()
    with get_database().read() as u:
        profile = u.profile.get()
        assert profile.owner_name == "Ana" and profile.fund_name == "Fondo Uno"
        assert profile.setup_completed_at is None and profile.workdir == str(env / "work")
    migrate()                                                       # idempotent
    with get_database().write() as u:
        u.profile.set_owner("Beatriz", None)
    migrate()                                                       # never overwrites what the app stored
    with get_database().read() as u:
        assert u.profile.get().owner_name == "Beatriz"


def test_no_profile_without_a_name(env):
    data = env / "data"
    data.mkdir(parents=True)
    (data / "owner.json").write_text(json.dumps({"owner_name": "", "fund_name": ""}), encoding="utf-8")
    migrate()
    with get_database().read() as u:
        assert u.profile.get() is None
