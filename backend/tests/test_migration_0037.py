"""Migration 0037 (SPEC-031, A2): rules written by the strategy coder, their versions, events and the specs that use them.
Additive, idempotent, checked, frozen code, append only events and links, known to the transfer."""
import sqlite3

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.db import current_version, db_path, migrate
from hokma.db import transfer
from hokma.db.backend import SqliteBackend
from hokma.db.migrate import MIGRATIONS_DIR, discover, render

VERSION = int(next(p.name for p in MIGRATIONS_DIR.glob("*_user_rules.sql"))[:4])
H = "a" * 64
OTHER = "b" * 64


def con_of():
    con = sqlite3.connect(str(db_path()))
    con.execute("PRAGMA foreign_keys = ON")
    return con


def bank(con, slug="s1"):
    return con.execute("INSERT INTO strategy_bank (slug, title) VALUES (?, 't')", (slug,)).lastrowid


def rule(con, bank_id, slug="inside_bar", kind="alpha"):
    return con.execute("INSERT INTO user_rules (slug, kind, title, bank_id, component, created_by) VALUES (?, ?, 't', ?, 'entry.trigger', 'strategy-coder')",
                       (slug, kind, bank_id)).lastrowid


VERSION_SQL = ("INSERT INTO user_rule_versions (rule_id, version, registry_id, code_sha256, code_bytes, code_text, cases_json, manifest_json, "
               "test_json, status, reviewed_by, reviewed_at, reviewed_sha256, created_by) VALUES (?, ?, ?, ?, 10, 'x = 1', '{}', ?, ?, ?, ?, ?, ?, 'strategy-coder')")


def version(con, rule_id, number=1, sha=H, status="draft", reviewed=False, reviewed_sha=None, manifest=None, tests=None):
    registry_id = f"u_r{rule_id}_v{number}"
    if status in ("generated", "owner_reviewed"):
        manifest, tests = manifest or "{}", tests or "{}"
    args = (rule_id, number, registry_id, sha, manifest, tests, status,
            "owner" if reviewed else None, "2026-01-01 00:00:00" if reviewed else None, reviewed_sha if reviewed else None)
    return con.execute(VERSION_SQL, args).lastrowid


def test_the_migration_is_applied_once_the_four_tables_exist_and_start_empty(env):
    assert VERSION in migrate().applied
    assert migrate().applied == [] and current_version() == discover()[-1][0]
    con = con_of()
    for table in transfer.USER_RULE_TABLES:
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert set(transfer.USER_RULE_TABLES) == {"user_rules", "user_rule_versions", "user_rule_events", "strategy_spec_rules"}
    assert con.execute("SELECT COUNT(*) FROM schema_version WHERE version = ?", (VERSION,)).fetchone()[0] == 1


def test_the_migration_creates_nothing_but_new_tables_and_leaves_the_older_ones_alone():
    text = (MIGRATIONS_DIR / f"{VERSION:04d}_user_rules.sql").read_text(encoding="utf-8")
    statements = [line.strip().upper() for line in text.splitlines() if line.strip() and not line.strip().startswith("--")]
    assert not [s for s in statements if s.startswith(("ALTER ", "DROP ", "DELETE ", "UPDATE ", "INSERT "))]
    assert VERSION > 36 and [v for v, _n, _p in discover() if v < VERSION][:24] == list(range(1, 25))
    assert "\u2014" not in text


def test_the_older_tables_are_byte_identical_after_the_new_migration(env, tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    for f in MIGRATIONS_DIR.glob("00*.sql"):
        if int(f.name[:4]) < VERSION:
            (old / f.name).write_bytes(f.read_bytes())
    db_old = tmp_path / "old.db"
    migrate(db_old, migrations_dir=old, seed=False)
    newer = tmp_path / "newer"
    newer.mkdir()
    for f in MIGRATIONS_DIR.glob("00*.sql"):
        if int(f.name[:4]) <= VERSION:                  # later migrations may add columns to these tables (0038 does): this one only adds tables
            (newer / f.name).write_bytes(f.read_bytes())
    db_new = tmp_path / "new.db"
    migrate(db_new, migrations_dir=newer, seed=False)

    def tables(path):
        c = sqlite3.connect(str(path))
        return {n: s for n, s in c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")}

    before, after = tables(db_old), tables(db_new)
    assert {n: s for n, s in after.items() if n in before} == before
    assert set(after) - set(before) == set(transfer.USER_RULE_TABLES)


def test_the_transfer_knows_every_table_in_dependency_order():
    script = "\n".join(render(p.read_text(encoding="utf-8"), SqliteBackend(p)) for _v, _n, p in discover())
    plan = [t.name for t in transfer.table_plan(script)]
    assert set(transfer.USER_RULE_TABLES) <= set(plan)
    at = plan.index
    assert at("strategy_bank") < at("user_rules") < at("user_rule_versions")
    assert at("user_rule_versions") < at("user_rule_events") and at("user_rule_versions") < at("strategy_spec_rules")


def test_a_rule_has_a_valid_slug_length_a_known_kind_and_a_bank_strategy(env):
    migrate()
    con = con_of()
    b = bank(con)
    rule(con, b, "ok", "filter")
    rule(con, b, "ok2", "exit")
    for slug, kind, bank_id in (("a", "alpha", b), ("x" * 41, "alpha", b), ("dup_a", "sizing", b), ("ok", "alpha", b), ("nobank", "alpha", 999)):
        with pytest.raises(sqlite3.IntegrityError):
            rule(con, bank_id, slug, kind)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM strategy_bank WHERE id = ?", (b,))


def test_a_version_is_unique_by_number_registry_id_and_hash_and_its_limits_hold(env):
    migrate()
    con = con_of()
    r = rule(con, bank(con))
    version(con, r, 1, H)
    for number, sha in ((1, OTHER), (2, H)):
        with pytest.raises(sqlite3.IntegrityError):
            version(con, r, number, sha)
    version(con, r, 2, OTHER)
    with pytest.raises(sqlite3.IntegrityError):
        version(con, r, 3, "short")
    with pytest.raises(sqlite3.IntegrityError):
        version(con, r, 0, "c" * 64)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE user_rule_versions SET status = 'approved'")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE user_rule_versions SET code_bytes = 40000")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE user_rule_versions SET review_note = ?", ("n" * 2001,))


def test_a_version_is_owner_reviewed_only_with_a_reviewer_a_time_and_the_hash_of_its_own_code(env):
    migrate()
    con = con_of()
    con.execute("DROP TRIGGER trg_rule_owner_needs_agent_ins")        # the INSERT gate of 0038 has its own tests: these ones only look at the shape of 0037
    r = rule(con, bank(con))
    version(con, r, 1, H, status="generated")
    version(con, r, 2, OTHER, status="owner_reviewed", reviewed=True, reviewed_sha=OTHER)
    with pytest.raises(sqlite3.IntegrityError):
        version(con, r, 3, "c" * 64, status="owner_reviewed", reviewed=False)                            # no reviewer
    with pytest.raises(sqlite3.IntegrityError):
        version(con, r, 4, "d" * 64, status="owner_reviewed", reviewed=True, reviewed_sha=H)             # hash of other code
    with pytest.raises(sqlite3.IntegrityError):
        version(con, r, 5, "e" * 64, status="generated", reviewed=True, reviewed_sha="e" * 64)           # reviewed but not owner_reviewed
    with pytest.raises(sqlite3.IntegrityError):
        version(con, r, 6, "f" * 64, status="owner_reviewed", reviewed=True, reviewed_sha=None)


def test_a_generated_version_carries_its_manifest_and_its_test_results(env):
    migrate()
    con = con_of()
    con.execute("DROP TRIGGER trg_rule_owner_needs_agent_ins")        # the INSERT gate of 0038 has its own tests: these ones only look at the shape of 0037
    r = rule(con, bank(con))
    for number, manifest, tests in ((1, None, "{}"), (2, "{}", None)):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO user_rule_versions (rule_id, version, registry_id, code_sha256, code_bytes, code_text, cases_json, manifest_json, "
                        "test_json, status, created_by) VALUES (?, ?, ?, ?, 1, 'x', '{}', ?, ?, 'generated', 'c')",
                        (r, number, f"u_x_v{number}", "a" * 63 + str(number), manifest, tests))
    version(con, r, 3, "c" * 64, status="generated")


def test_the_code_of_a_version_never_changes_but_its_status_does(env):
    migrate()
    con = con_of()
    r = rule(con, bank(con))
    v = version(con, r)
    r2 = rule(con, bank(con, "s2"), "other_rule")
    for column, value in (("code_text", "y = 2"), ("code_sha256", "9" * 64), ("cases_json", "[]"), ("registry_id", "u_other_v1"), ("rule_id", r2), ("version", 7)):
        with pytest.raises(sqlite3.DatabaseError, match="never changes"):
            con.execute(f"UPDATE user_rule_versions SET {column} = ? WHERE id = ?", (value, v))
    con.execute("UPDATE user_rule_versions SET status = 'failed', check_json = '[1]' WHERE id = ?", (v,))
    assert con.execute("SELECT status FROM user_rule_versions WHERE id = ?", (v,)).fetchone()[0] == "failed"


def test_a_version_in_use_cannot_be_deleted_and_a_rule_with_versions_neither(env):
    migrate()
    con = con_of()
    con.execute("DROP TRIGGER trg_spec_rules_need_review")        # the second gate of 0038 has its own tests: these ones only look at the shape of the links of 0037
    r = rule(con, bank(con))
    v = version(con, r)
    con.execute("INSERT INTO strategy_spec_rules (strategy_id, spec_version, rule_version_id, code_sha256) VALUES ('s', 1, ?, ?)", (v, H))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM user_rule_versions WHERE id = ?", (v,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM user_rules WHERE id = ?", (r,))


def test_the_events_are_append_only_with_a_known_channel_and_the_links_never_change(env):
    migrate()
    con = con_of()
    con.execute("DROP TRIGGER trg_spec_rules_need_review")        # the second gate of 0038 has its own tests: these ones only look at the shape of the links of 0037
    v = version(con, rule(con, bank(con)))
    con.execute("INSERT INTO user_rule_events (rule_version_id, from_status, to_status, actor, channel) VALUES (?, NULL, 'draft', 'strategy-coder', 'tool')", (v,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO user_rule_events (rule_version_id, to_status, actor, channel) VALUES (?, 'draft', 'x', 'telepathy')", (v,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE user_rule_events SET to_status = 'generated'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM user_rule_events")
    con.execute("INSERT INTO strategy_spec_rules (strategy_id, spec_version, rule_version_id, code_sha256) VALUES ('s', 1, ?, ?)", (v, H))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO strategy_spec_rules (strategy_id, spec_version, rule_version_id, code_sha256) VALUES ('s', 1, ?, ?)", (v, H))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO strategy_spec_rules (strategy_id, spec_version, rule_version_id, code_sha256) VALUES ('s', 2, ?, 'short')", (v,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE strategy_spec_rules SET spec_version = 9")


def test_the_channel_is_tool_app_terminal_or_system_and_dashboard_is_not_one(env):
    migrate()
    con = con_of()
    v = version(con, rule(con, bank(con)))
    for channel in ("tool", "app", "terminal", "system"):
        con.execute("INSERT INTO user_rule_events (rule_version_id, to_status, actor, channel) VALUES (?, 'draft', 'x', ?)", (v, channel))
    for channel in ("dashboard", "telegram", ""):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO user_rule_events (rule_version_id, to_status, actor, channel) VALUES (?, 'draft', 'x', ?)", (v, channel))


def test_a_spec_link_must_carry_the_hash_of_the_version_it_points_to(env):
    migrate()
    con = con_of()
    con.execute("DROP TRIGGER trg_spec_rules_need_review")        # the second gate of 0038 has its own tests: these ones only look at the shape of the links of 0037
    r = rule(con, bank(con))
    v1, v2 = version(con, r, 1, H), version(con, r, 2, OTHER)
    con.execute("PRAGMA foreign_keys = ON")
    for version_id, sha in ((v1, OTHER), (v2, H), (v1, "c" * 64)):           # a real hash of ANOTHER version, or of none: refused
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO strategy_spec_rules (strategy_id, spec_version, rule_version_id, code_sha256) VALUES ('s', 1, ?, ?)", (version_id, sha))
    con.execute("INSERT INTO strategy_spec_rules (strategy_id, spec_version, rule_version_id, code_sha256) VALUES ('s', 1, ?, ?)", (v1, H))
    con.execute("INSERT INTO strategy_spec_rules (strategy_id, spec_version, rule_version_id, code_sha256) VALUES ('s', 1, ?, ?)", (v2, OTHER))


def test_postgres_has_the_same_checks_the_frozen_code_and_the_append_only_tables(pg_url_migrated):
    import psycopg

    with psycopg.connect(pg_url_migrated, autocommit=True) as c:
        c.execute("DROP TRIGGER trg_spec_rules_need_review ON strategy_spec_rules")        # the second gate of 0038 has its own tests
        c.execute("DROP TRIGGER trg_rule_owner_needs_agent_ins ON user_rule_versions")        # the INSERT gate of 0038 has its own tests: these ones only look at the shape of 0037
        c.execute("INSERT INTO strategy_bank (slug, title) VALUES ('s', 't')")
        c.execute("INSERT INTO user_rules (slug, kind, title, bank_id, component, created_by) VALUES ('inside_bar', 'alpha', 't', 1, 'entry.trigger', 'c')")
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO user_rules (slug, kind, title, bank_id, component, created_by) VALUES ('sizer', 'sizing', 't', 1, 'sizing', 'c')")
        row = ("INSERT INTO user_rule_versions (rule_id, version, registry_id, code_sha256, code_bytes, code_text, cases_json, manifest_json, test_json, "
               "status, reviewed_by, reviewed_at, reviewed_sha256, created_by) VALUES (1, %s, %s, %s, 10, 'x = 1', '{}', %s, %s, %s, %s, %s, %s, 'c')")
        c.execute(row, (1, "u_inside_bar_v1", H, "{}", "{}", "generated", None, None, None))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(row, (2, "u_inside_bar_v2", OTHER, "{}", "{}", "owner_reviewed", None, None, None))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(row, (3, "u_inside_bar_v3", "c" * 64, "{}", "{}", "owner_reviewed", "owner", "2026-01-01", H))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(row, (4, "u_inside_bar_v4", "d" * 64, None, None, "generated", None, None, None))
        c.execute(row, (5, "u_inside_bar_v5", "e" * 64, "{}", "{}", "owner_reviewed", "owner", "2026-01-01", "e" * 64))
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute(row, (1, "u_inside_bar_v9", "9" * 64, "{}", "{}", "generated", None, None, None))
        c.execute("INSERT INTO strategy_bank (slug, title) VALUES ('s2', 't')")
        c.execute("INSERT INTO user_rules (slug, kind, title, bank_id, component, created_by) VALUES ('other_rule', 'alpha', 't', 2, 'entry.trigger', 'c')")
        for column, value in (("code_text", "y = 2"), ("code_sha256", "9" * 64), ("cases_json", "[]"), ("registry_id", "u_other_v1"), ("rule_id", 2), ("version", 7)):
            with pytest.raises(psycopg.Error, match="never changes"):
                c.execute(f"UPDATE user_rule_versions SET {column} = %s WHERE id = 1", (value,))
        c.execute("UPDATE user_rule_versions SET status = 'failed' WHERE id = 1")
        c.execute("INSERT INTO user_rule_events (rule_version_id, to_status, actor, channel) VALUES (1, 'draft', 'strategy-coder', 'tool')")
        c.execute("INSERT INTO user_rule_events (rule_version_id, to_status, actor, channel) VALUES (1, 'draft', 'owner', 'app')")
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO user_rule_events (rule_version_id, to_status, actor, channel) VALUES (1, 'draft', 'x', 'dashboard')")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):            # the hash a spec stores must be the hash of THAT version
            c.execute("INSERT INTO strategy_spec_rules (strategy_id, spec_version, rule_version_id, code_sha256) VALUES ('s', 1, 1, %s)", (OTHER,))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO user_rule_events (rule_version_id, to_status, actor, channel) VALUES (1, 'draft', 'x', 'telepathy')")
        with pytest.raises(psycopg.Error, match="append-only"):
            c.execute("UPDATE user_rule_events SET to_status = 'generated'")
        with pytest.raises(psycopg.Error, match="append-only"):
            c.execute("DELETE FROM user_rule_events")
        c.execute("INSERT INTO strategy_spec_rules (strategy_id, spec_version, rule_version_id, code_sha256) VALUES ('s', 1, 1, %s)", (H,))
        with pytest.raises(psycopg.Error, match="append-only"):
            c.execute("UPDATE strategy_spec_rules SET spec_version = 9")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            c.execute("DELETE FROM user_rule_versions WHERE id = 1")
