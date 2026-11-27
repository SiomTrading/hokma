"""Migration 0038 (SPEC-031 rev 3, A2): the requirement of a strategy, the verdicts of the code reviewer and where each strategy and rule was created.
Additive, checked, frozen briefs, append only verdicts, the second gate in the base (a version is generated, owner_reviewed or linked to a spec only behind an
approval of the same code), known to the transfer."""
import sqlite3

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.db import current_version, db_path, migrate
from hokma.db import transfer
from hokma.db.backend import SqliteBackend
from hokma.db.migrate import MIGRATIONS_DIR, discover, render

VERSION = int(next(p.name for p in MIGRATIONS_DIR.glob("*_rule_briefs_reviews_origin.sql"))[:4])
H = "a" * 64
OTHER = "b" * 64
NEW_TABLES = ("user_rule_briefs", "user_rule_reviews")
ORIGIN_TABLES = ("strategy_bank", "strategy_bank_versions", "user_rules", "user_rule_versions")


def con_of():
    con = sqlite3.connect(str(db_path()))
    con.execute("PRAGMA foreign_keys = ON")
    return con


def setup(con, slug="s1", rule_slug="inside_bar"):
    bank_id = con.execute("INSERT INTO strategy_bank (slug, title) VALUES (?, 't')", (slug,)).lastrowid
    rule_id = con.execute("INSERT INTO user_rules (slug, kind, title, bank_id, component, created_by) VALUES (?, 'strategy', 't', ?, 'strategy', 'strategy-coder')",
                          (rule_slug, bank_id)).lastrowid
    return bank_id, rule_id


def bank_version(con, bank_id):
    src = con.execute("SELECT id FROM strategy_sources LIMIT 1").fetchone()
    src_id = src[0] if src else con.execute("INSERT INTO strategy_sources (kind, reference, rights, content_sha256, created_by) VALUES ('text', 'r', 'own', ?, 'iris')",
                                            ("h" * 64,)).lastrowid
    return con.execute("INSERT INTO strategy_bank_versions (bank_id, version, source_id, extraction_json, extraction_hash, created_by) VALUES (?, 1, ?, '{}', ?, 'iris')",
                       (bank_id, src_id, "e" * 64)).lastrowid


def brief(con, rule_id, request_version, number=1, sha=H, status="current", desk=(None, None)):
    return con.execute("INSERT INTO user_rule_briefs (rule_id, version, request_version, brief_json, brief_sha256, status, origin_desk_id, origin_desk_name, created_by) "
                       "VALUES (?, ?, ?, '{}', ?, ?, ?, ?, 'strategy-researcher')", (rule_id, number, request_version, sha, status, desk[0], desk[1])).lastrowid


def code_version(con, rule_id, number=1, sha=H, status="draft"):
    return con.execute("INSERT INTO user_rule_versions (rule_id, version, registry_id, code_sha256, code_bytes, code_text, cases_json, manifest_json, test_json, "
                       "status, created_by) VALUES (?, ?, ?, ?, 10, 'x = 1', '{}', '{}', '{}', ?, 'strategy-coder')",
                       (rule_id, number, f"u_r{rule_id}_v{number}", sha, status)).lastrowid


def review(con, version_id, brief_id, sha=H, verdict="approved"):
    return con.execute("INSERT INTO user_rule_reviews (rule_version_id, brief_id, code_sha256, test_json, verdict, reviewer) VALUES (?, ?, ?, '{}', ?, 'strategy-reviewer')",
                       (version_id, brief_id, sha, verdict)).lastrowid


def world(con):
    bank_id, rule_id = setup(con)
    rv = bank_version(con, bank_id)
    return bank_id, rule_id, rv


def test_the_migration_is_applied_once_the_two_tables_exist_and_start_empty(env):
    assert VERSION in migrate().applied
    assert migrate().applied == [] and current_version() == discover()[-1][0]
    con = con_of()
    for table in NEW_TABLES:
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert set(transfer.RULE_BRIEF_TABLES) == set(NEW_TABLES)
    assert "user_rule_brief_events" not in {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert con.execute("SELECT COUNT(*) FROM schema_version WHERE version = ?", (VERSION,)).fetchone()[0] == 1
    assert VERSION == 38


def test_the_new_columns_exist_and_the_rows_of_0036_and_0037_keep_a_null_origin(env, tmp_path):
    migrate()
    con = con_of()
    for table in ORIGIN_TABLES:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        assert {"origin_desk_id", "origin_desk_name"} <= cols, table
    assert "brief_id" in {r[1] for r in con.execute("PRAGMA table_info(user_rule_versions)")}
    bank_id, rule_id, rv = world(con)
    v = code_version(con, rule_id)
    for table, key in (("strategy_bank", bank_id), ("strategy_bank_versions", rv), ("user_rules", rule_id), ("user_rule_versions", v)):
        assert con.execute(f"SELECT origin_desk_id, origin_desk_name FROM {table} WHERE id = ?", (key,)).fetchone() == (None, None)
    assert con.execute("SELECT brief_id FROM user_rule_versions WHERE id = ?", (v,)).fetchone()[0] is None


def test_the_migration_only_adds_tables_and_nullable_columns_and_keeps_0001_to_0037():
    text = (MIGRATIONS_DIR / f"{VERSION:04d}_rule_briefs_reviews_origin.sql").read_text(encoding="utf-8")
    statements = [line.strip().upper() for line in text.splitlines() if line.strip() and not line.strip().startswith("--")]
    assert not [s for s in statements if s.startswith(("DROP ", "DELETE ", "UPDATE ", "INSERT "))]
    alters = [s for s in statements if s.startswith("ALTER ")]
    assert alters and all("ADD COLUMN" in s and "NOT NULL" not in s for s in alters)
    assert [v for v, _n, _p in discover() if v < VERSION][:24] == list(range(1, 25)) and 36 in [v for v, _n, _p in discover()] and 37 in [v for v, _n, _p in discover()]
    assert "\u2014" not in text and "rev 3" in text


def test_the_tables_of_0037_and_before_only_gain_the_new_columns(env, tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    for f in MIGRATIONS_DIR.glob("00*.sql"):
        if int(f.name[:4]) < VERSION:
            (old / f.name).write_bytes(f.read_bytes())
    db_old = tmp_path / "old.db"
    migrate(db_old, migrations_dir=old, seed=False)
    db_new = tmp_path / "new.db"
    migrate(db_new, seed=False)

    def columns(path):
        c = sqlite3.connect(str(path))
        names = [n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {n: [(r[1], r[2], r[3], r[4]) for r in c.execute(f"PRAGMA table_info({n})")] for n in names}

    before, after = columns(db_old), columns(db_new)
    changed = {n for n in before if after[n] != before[n]}
    assert changed == set(ORIGIN_TABLES)
    for n in changed:
        assert after[n][:len(before[n])] == before[n]          # nothing moved or changed type: columns were only appended
    assert set(after) - set(before) - set(transfer.CREATION_TABLES) == set(NEW_TABLES)          # 0040 adds its own table on top


def test_the_transfer_knows_the_new_tables_in_dependency_order():
    script = "\n".join(render(p.read_text(encoding="utf-8"), SqliteBackend(p)) for _v, _n, p in discover())
    plan = [t.name for t in transfer.table_plan(script)]
    assert set(NEW_TABLES) <= set(plan)
    at = plan.index
    assert at("user_rules") < at("user_rule_briefs") < at("user_rule_versions")
    assert at("user_rule_versions") < at("user_rule_reviews") and at("user_rule_briefs") < at("user_rule_reviews")


def test_the_rule_kind_strategy_exists_and_a_kind_that_is_not_known_is_refused(env):
    migrate()
    con = con_of()
    bank_id, _rule_id = setup(con)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO user_rules (slug, kind, title, bank_id, component, created_by) VALUES ('other', 'sizing', 't', ?, 'x', 'c')", (bank_id,))


def test_a_brief_is_unique_by_number_and_hash_and_its_status_and_hash_length_are_checked(env):
    migrate()
    con = con_of()
    _b, rule_id, rv = world(con)
    brief(con, rule_id, rv, 1, H, "superseded")
    for number, sha in ((1, OTHER), (2, H)):
        with pytest.raises(sqlite3.IntegrityError):
            brief(con, rule_id, rv, number, sha)
    for number, sha, status in ((2, "short", "current"), (0, "c" * 64, "current"), (3, "d" * 64, "published"), (4, "e" * 64, "draft")):
        with pytest.raises(sqlite3.IntegrityError):
            brief(con, rule_id, rv, number, sha, status)
    with pytest.raises(sqlite3.IntegrityError):
        brief(con, 999, rv, 2, "e" * 64)
    with pytest.raises(sqlite3.IntegrityError):
        brief(con, rule_id, 999, 2, "e" * 64)


def test_only_one_brief_per_rule_is_current_at_a_time(env):
    migrate()
    con = con_of()
    _b, rule_id, rv = world(con)
    first = brief(con, rule_id, rv, 1, H, "current")
    with pytest.raises(sqlite3.IntegrityError):
        brief(con, rule_id, rv, 2, OTHER, "current")
    con.execute("UPDATE user_rule_briefs SET status = 'superseded' WHERE id = ?", (first,))
    brief(con, rule_id, rv, 2, OTHER, "current")


def test_the_origin_of_a_brief_is_both_fields_or_neither_and_the_name_is_short(env):
    migrate()
    con = con_of()
    _b, rule_id, rv = world(con)
    brief(con, rule_id, rv, 1, H, "superseded", desk=(None, None))
    brief(con, rule_id, rv, 2, OTHER, "current", desk=(7, "Mesa NQ"))
    for number, desk in ((3, (7, None)), (4, (None, "Mesa NQ")), (5, (7, "")), (6, (7, "n" * 81))):
        with pytest.raises(sqlite3.IntegrityError):
            brief(con, rule_id, rv, number, f"{number}" * 64, "superseded", desk=desk)


def test_a_requirement_never_changes_but_its_status_does(env):
    migrate()
    con = con_of()
    _b, rule_id, rv = world(con)
    b = brief(con, rule_id, rv, 1, H)
    for column, value in (("brief_json", "{\"a\":1}"), ("brief_sha256", "9" * 64), ("rule_id", rule_id + 1), ("version", 4), ("request_version", rv + 1)):
        with pytest.raises(sqlite3.DatabaseError, match="never changes"):
            con.execute(f"UPDATE user_rule_briefs SET {column} = ? WHERE id = ?", (value, b))
    con.execute("UPDATE user_rule_briefs SET status = 'superseded' WHERE id = ?", (b,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM user_rules WHERE id = ?", (rule_id,))


def test_a_verdict_is_append_only_has_a_known_verdict_and_a_real_hash(env):
    migrate()
    con = con_of()
    _b, rule_id, rv = world(con)
    b = brief(con, rule_id, rv, 1, H)
    v = code_version(con, rule_id)
    review(con, v, b, H, "approved")
    review(con, v, b, H, "rejected")
    for sha, verdict in (("short", "approved"), (H, "maybe")):
        with pytest.raises(sqlite3.IntegrityError):
            review(con, v, b, sha, verdict)
    with pytest.raises(sqlite3.IntegrityError):
        review(con, 999, b)
    with pytest.raises(sqlite3.IntegrityError):
        review(con, v, 999)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE user_rule_reviews SET verdict = 'approved'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM user_rule_reviews")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM user_rule_versions WHERE id = ?", (v,))


def move(con, v, status, sha=H):
    owner = ", reviewed_by = 'owner', reviewed_at = '2026-01-02', reviewed_sha256 = ?" if status == "owner_reviewed" else ""
    con.execute(f"UPDATE user_rule_versions SET status = ?{owner} WHERE id = ?", (status, *((sha,) if owner else ()), v))


def test_a_version_is_not_generated_or_owner_reviewed_without_an_approval_of_the_same_code_even_by_direct_sql(env):
    migrate()
    con = con_of()
    _b, rule_id, rv = world(con)
    b = brief(con, rule_id, rv, 1, H)
    v = code_version(con, rule_id, 1, H)
    for target in ("generated", "owner_reviewed"):
        with pytest.raises(sqlite3.DatabaseError, match="code reviewer has not approved"):               # no verdict at all
            move(con, v, target)
    review(con, v, b, H, "rejected")
    with pytest.raises(sqlite3.DatabaseError, match="code reviewer has not approved"):               # the last verdict is a rejection
        move(con, v, "generated")
    review(con, v, b, OTHER, "approved")
    with pytest.raises(sqlite3.DatabaseError, match="code reviewer has not approved"):               # approved, but for another hash
        move(con, v, "generated")
    review(con, v, b, H, "approved")
    move(con, v, "generated")
    move(con, v, "owner_reviewed")
    assert con.execute("SELECT status FROM user_rule_versions WHERE id = ?", (v,)).fetchone()[0] == "owner_reviewed"


def test_a_version_cannot_be_inserted_already_generated_or_owner_reviewed(env):
    migrate()
    con = con_of()
    _b, rule_id, rv = world(con)
    brief(con, rule_id, rv, 1, H)
    for n, target in enumerate(("generated", "owner_reviewed"), start=1):
        with pytest.raises(sqlite3.DatabaseError, match="code reviewer has not approved"):
            code_version(con, rule_id, n, H, status=target)
    assert con.execute("SELECT count(*) FROM user_rule_versions").fetchone()[0] == 0
    assert code_version(con, rule_id, 1, H, status="draft")


def test_a_later_rejection_takes_the_approval_away_for_the_owner(env):
    migrate()
    con = con_of()
    _b, rule_id, rv = world(con)
    b = brief(con, rule_id, rv, 1, H)
    v = code_version(con, rule_id, 1, H)
    review(con, v, b, H, "approved")
    move(con, v, "generated")
    review(con, v, b, H, "rejected")
    with pytest.raises(sqlite3.DatabaseError, match="code reviewer has not approved"):
        move(con, v, "owner_reviewed")


def link(con, v, sha=H, strategy="u_r1_v1"):
    return con.execute("INSERT INTO strategy_spec_rules (strategy_id, spec_version, rule_version_id, code_sha256) VALUES (?, 1, ?, ?)", (strategy, v, sha))


def test_a_strategy_is_linked_to_a_spec_only_through_a_version_the_reviewer_approved(env):
    migrate()
    con = con_of()
    _b, rule_id, rv = world(con)
    b = brief(con, rule_id, rv, 1, H)
    v = code_version(con, rule_id, 1, H)
    with pytest.raises(sqlite3.DatabaseError, match="code reviewer has not approved"):               # a draft with no verdict
        link(con, v)
    review(con, v, b, H, "approved")
    with pytest.raises(sqlite3.DatabaseError, match="code reviewer has not approved"):               # approved, but the version is still a draft
        link(con, v)
    move(con, v, "generated")
    review(con, v, b, H, "rejected")
    with pytest.raises(sqlite3.DatabaseError, match="code reviewer has not approved"):               # the last verdict is a rejection
        link(con, v)
    review(con, v, b, H, "approved")
    link(con, v)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE strategy_spec_rules SET spec_version = 2")


def test_a_version_points_to_the_requirement_it_was_written_against(env):
    migrate()
    con = con_of()
    _b, rule_id, rv = world(con)
    b = brief(con, rule_id, rv, 1, H)
    v = code_version(con, rule_id)
    con.execute("UPDATE user_rule_versions SET brief_id = ? WHERE id = ?", (b, v))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE user_rule_versions SET brief_id = 999 WHERE id = ?", (v,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM user_rule_briefs WHERE id = ?", (b,))


def test_the_origin_columns_take_a_desk_id_with_no_foreign_key_and_a_short_name(env):
    migrate()
    con = con_of()
    con.execute("INSERT INTO strategy_bank (slug, title, origin_desk_id, origin_desk_name) VALUES ('from-desk', 't', 4242, 'Mesa NQ')")      # there is no desk 4242
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO strategy_bank (slug, title, origin_desk_id, origin_desk_name) VALUES ('long-name', 't', 1, ?)", ("n" * 81,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO strategy_bank (slug, title, origin_desk_id, origin_desk_name) VALUES ('empty-name', 't', 1, '')")
    assert con.execute("SELECT origin_desk_id, origin_desk_name FROM strategy_bank WHERE slug = 'from-desk'").fetchone() == (4242, "Mesa NQ")
    assert "idx_bank_origin" in {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def test_postgres_has_the_same_checks_the_frozen_brief_the_append_only_verdicts_and_the_two_triggers_of_the_second_gate(pg_url_migrated):
    import psycopg

    with psycopg.connect(pg_url_migrated, autocommit=True) as c:
        c.execute("INSERT INTO strategy_bank (slug, title, origin_desk_id, origin_desk_name) VALUES ('s', 't', 4242, 'Mesa NQ')")
        c.execute("INSERT INTO strategy_sources (kind, reference, rights, content_sha256, created_by) VALUES ('text', 'r', 'own', %s, 'iris')", ("h" * 64,))
        c.execute("INSERT INTO strategy_bank_versions (bank_id, version, source_id, extraction_json, extraction_hash, created_by) VALUES (1, 1, 1, '{}', %s, 'iris')", ("e" * 64,))
        c.execute("INSERT INTO user_rules (slug, kind, title, bank_id, component, created_by) VALUES ('inside_bar', 'strategy', 't', 1, 'strategy', 'c')")
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO strategy_bank (slug, title, origin_desk_id, origin_desk_name) VALUES ('x', 't', 1, %s)", ("n" * 81,))
        row = ("INSERT INTO user_rule_briefs (rule_id, version, request_version, brief_json, brief_sha256, status, origin_desk_id, origin_desk_name, created_by) "
               "VALUES (1, %s, 1, '{}', %s, %s, %s, %s, 'iris')")
        c.execute(row, (1, H, "current", None, None))
        with pytest.raises(psycopg.errors.UniqueViolation):                      # one current brief per rule
            c.execute(row, (2, OTHER, "current", None, None))
        with pytest.raises(psycopg.errors.CheckViolation):                       # a status that is not known
            c.execute(row, (3, "c" * 64, "approved", None, None))
        with pytest.raises(psycopg.errors.CheckViolation):                       # origin: both or neither
            c.execute(row, (5, "e" * 64, "superseded", 7, None))
        c.execute(row, (6, "f" * 64, "superseded", 7, "Mesa NQ"))
        for column, value in (("brief_json", "{\"a\":1}"), ("brief_sha256", "9" * 64), ("rule_id", 2), ("version", 9), ("request_version", 2)):
            with pytest.raises(psycopg.Error, match="never changes"):
                c.execute(f"UPDATE user_rule_briefs SET {column} = %s WHERE id = 1", (value,))
        c.execute("UPDATE user_rule_briefs SET status = 'superseded' WHERE id = 1")
        vrow = ("INSERT INTO user_rule_versions (rule_id, version, registry_id, code_sha256, code_bytes, code_text, cases_json, manifest_json, test_json, status, created_by) "
                "VALUES (1, 1, 'u_inside_bar_v1', %s, 10, 'x = 1', '{}', '{}', '{}', 'draft', 'c')")
        c.execute(vrow, (H,))
        review_sql = "INSERT INTO user_rule_reviews (rule_version_id, brief_id, code_sha256, test_json, verdict, reviewer) VALUES (1, (SELECT max(id) FROM user_rule_briefs), %s, '{}', %s, 'strategy-reviewer')"
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(review_sql, ("short", "approved"))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(review_sql, (H, "maybe"))
        for born in ("generated", "owner_reviewed"):                              # also on INSERT
            with pytest.raises(psycopg.Error, match="code reviewer has not approved"):
                c.execute(vrow.replace("'draft'", f"'{born}'").replace("'u_inside_bar_v1'", "'u_inside_bar_v2'").replace("(1, 1,", "(1, 2,"), (H,))
        generated = "UPDATE user_rule_versions SET status = 'generated' WHERE id = 1"
        owner = "UPDATE user_rule_versions SET status = 'owner_reviewed', reviewed_by = 'owner', reviewed_at = '2026-01-02', reviewed_sha256 = %s WHERE id = 1"
        link_sql = "INSERT INTO strategy_spec_rules (strategy_id, spec_version, rule_version_id, code_sha256) VALUES ('u_inside_bar_v1', 1, 1, %s)"
        with pytest.raises(psycopg.Error, match="code reviewer has not approved"):
            c.execute(generated)
        with pytest.raises(psycopg.Error, match="code reviewer has not approved"):
            c.execute(link_sql, (H,))
        c.execute(review_sql, (OTHER, "approved"))
        with pytest.raises(psycopg.Error, match="code reviewer has not approved"):
            c.execute(generated)
        c.execute(review_sql, (H, "approved"))
        c.execute(generated)
        c.execute(review_sql, (H, "rejected"))
        with pytest.raises(psycopg.Error, match="code reviewer has not approved"):
            c.execute(owner, (H,))
        with pytest.raises(psycopg.Error, match="code reviewer has not approved"):
            c.execute(link_sql, (H,))
        c.execute(review_sql, (H, "approved"))
        c.execute(link_sql, (H,))
        c.execute(owner, (H,))
        with pytest.raises(psycopg.Error, match="append-only"):
            c.execute("UPDATE user_rule_reviews SET verdict = 'approved'")
        with pytest.raises(psycopg.Error, match="append-only"):
            c.execute("DELETE FROM user_rule_reviews")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            c.execute("DELETE FROM user_rule_briefs WHERE id = (SELECT max(id) FROM user_rule_briefs)")
