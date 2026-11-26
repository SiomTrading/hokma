"""Migration of the verdict tables (SPEC-032, OPT-01 and OPT-02): idempotent, recorded in schema_version, append-only tables refuse UPDATE and DELETE
in both engines, the seed holds the values of section 6 for DT, SWC and SWO, and the copy between engines carries the four tables."""
import hashlib
import json
import sqlite3

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url  # noqa: F401  (fixtures)

from hokma.db import current_version, db_path, migrate
from hokma.db import transfer
from hokma.db.backend import SqliteBackend
from hokma.db.migrate import MIGRATIONS_DIR, discover, render
from hokma.db.postgres import PostgresBackend

VERSION = int(next(p.name for p in MIGRATIONS_DIR.glob("*_verdict.sql"))[:4])
FILE = MIGRATIONS_DIR / f"{VERSION:04d}_verdict.sql"
APPEND_ONLY = ("audit_threshold_sets", "audit_answers", "strategy_verdicts")
MEASURE_KEYS = {"pbo_warn", "pbo_reject", "min_dsr", "min_oos_to_is", "max_cost_share", "edge_warns", "neighbor_spread", "max_neighbor_drop",
                "red_flag_sharpe", "max_top_trade_share", "min_years", "min_trades", "min_regime_share"}
# The section 6 table: the start values shared by the three lines, and the two that change by line (M11 years, M12 trades).
SECTION_6 = {"pbo_warn": 0.20, "pbo_reject": 0.50, "min_dsr": 0.95, "min_oos_to_is": 0.50, "max_cost_share": 0.33, "edge_warns": True,
             "neighbor_spread": 0.10, "max_neighbor_drop": 0.30, "red_flag_sharpe": 3.0, "max_top_trade_share": 0.25, "min_regime_share": 0.50}
BY_LINE = {"DT": (1, 200), "SWC": (3, 100), "SWO": (3, 50)}


def raw():
    return sqlite3.connect(str(db_path()))


def test_the_number_is_the_one_of_the_spec_and_is_free():
    assert VERSION == 35 and [p.name for p in MIGRATIONS_DIR.glob("0035_*.sql")] == ["0035_verdict.sql"]


def test_applying_the_migrations_twice_changes_nothing_and_verdict_is_in_schema_version(env):
    first = migrate()
    assert VERSION in first.applied
    con = raw()
    before = sorted(r[0] for r in con.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    assert migrate().applied == [] and current_version() == discover()[-1][0]
    after = sorted(r[0] for r in raw().execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    assert before == after
    assert con.execute("SELECT COUNT(*) FROM schema_version WHERE version = ?", (VERSION,)).fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM audit_threshold_sets").fetchone()[0] == 3         # the seed is not repeated


def test_the_migration_creates_nothing_but_new_tables_and_the_seed_rows():
    statements = [line.strip().upper() for line in FILE.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("--")]
    assert not [s for s in statements if s.startswith(("ALTER ", "DROP ", "DELETE ", "UPDATE "))]
    created = [s.split()[2] for s in statements if s.startswith("CREATE TABLE ")]
    assert created == [t.upper() for t in transfer.VERDICT_TABLES]
    assert [s for s in statements if s.startswith("INSERT ")] == ["INSERT INTO AUDIT_THRESHOLD_SETS (LINE, VERSION, VALUES_JSON, VALUES_HASH, CHANGED_BY)"] * 3


def test_the_file_has_no_long_dash():
    assert "\u2014" not in FILE.read_text(encoding="utf-8")


def test_the_transfer_knows_the_four_tables_in_dependency_order():
    script = "\n".join(render(p.read_text(encoding="utf-8"), SqliteBackend(p)) for _v, _n, p in discover())
    plan = [t.name for t in transfer.table_plan(script)]
    assert transfer.VERDICT_TABLES == ("audit_threshold_sets", "audit_answers", "strategy_verdicts", "library_requests")
    assert set(transfer.VERDICT_TABLES) <= set(plan)
    assert plan.index("strategy_verdicts") > plan.index("audit_threshold_sets") and plan.index("strategy_verdicts") > plan.index("backtest_runs")
    assert [t for t in plan if t in transfer.VERDICT_TABLES] == sorted(set(transfer.VERDICT_TABLES) & set(plan), key=plan.index)


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_update_and_delete_fail_in_the_three_append_only_tables(env, table):
    migrate()
    con = raw()
    con.execute("INSERT INTO backtest_runs (id, kind, spec_json, spec_hash, engine_version, seed, status) VALUES (1, 'run', '{}', 'h', 'e', 0, 'done')")
    rows = {
        "audit_threshold_sets": "INSERT INTO audit_threshold_sets (line, version, values_json, values_hash, changed_by) VALUES ('LP', 1, '{}', 'h', 'owner')",
        "audit_answers": "INSERT INTO audit_answers (scope, subject, question_key, answer, answered_by) VALUES ('data', 'FUT:NQ', 'point_in_time', 'yes', 'owner')",
        "strategy_verdicts": ("INSERT INTO strategy_verdicts (strategy_id, strategy_version, line, cause, run_id, thresholds_id, evidence_json, audit_verdict, "
                              "verdict, report_json, sentences_json, inputs_hash, actor) VALUES ('s', 1, 'DT', 'evaluate', 1, 1, '{}', 'go', 'go', '{}', '[]', 'h', 'owner')"),
    }
    con.execute(rows[table])
    con.commit()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute(f"UPDATE {table} SET id = id")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute(f"DELETE FROM {table}")


def test_library_requests_is_the_one_table_that_can_change_and_its_resolution_follows_its_status(env):
    migrate()
    con = raw()
    insert = "INSERT INTO library_requests (name, description, status, resolution, requested_by) VALUES (?, ?, ?, ?, 'ian')"
    description = "an indicator the library lacks, described with enough words"
    con.execute(insert, ("vwap_band", description, "open", None))
    con.execute(insert, ("vwap_band2", description, "declined", "not a causal indicator"))
    for bad in (("a", description, "open", None), ("x" * 49, description, "open", None), ("n", "too short", "open", None),
                ("n", description, "open", "an open request has no resolution"), ("n", description, "built", None), ("n", description, "maybe", None)):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(insert, bad) if bad[0] != "a" else con.execute(insert, ("", *bad[1:]))
    con.execute("UPDATE library_requests SET status = 'wired', resolution = 'wired into the library' WHERE name = 'vwap_band'")
    con.execute("DELETE FROM library_requests WHERE name = 'vwap_band2'")


def test_the_limits_of_the_columns_hold(env):
    migrate()
    con = raw()
    sets = "INSERT INTO audit_threshold_sets (line, version, values_json, values_hash, changed_by, reason) VALUES (?, ?, '{}', 'h', 'owner', ?)"
    con.execute(sets, ("LP", 1, "x" * 500))
    for bad in (("XX", 1, None), ("LP", 0, None), ("LP", 1, None), ("RF", 2, "x" * 501)):          # (LP, 1) is taken: UNIQUE (line, version)
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(sets, bad)
    answer = "INSERT INTO audit_answers (scope, subject, question_key, answer, note, answered_by) VALUES (?, ?, ?, ?, ?, 'owner')"
    con.execute(answer, ("strategy", "s@1", "capacity_checked", "na", "x" * 1000))
    for bad in (("market", "s@1", "k", "yes", None), ("data", "", "k", "yes", None), ("data", "s" * 201, "k", "yes", None), ("data", "s", "", "yes", None),
                ("data", "s", "k" * 41, "yes", None), ("data", "s", "k", "maybe", None), ("data", "s", "k", "yes", "x" * 1001)):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(answer, bad)
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):                       # restored_from must point at a set that exists
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("INSERT INTO audit_threshold_sets (line, version, values_json, values_hash, changed_by, restored_from) VALUES ('RF', 1, '{}', 'h', 'owner', 999)")


def test_the_seed_is_version_1_of_dt_swc_and_swo_with_the_values_of_section_6(env):
    migrate()
    rows = raw().execute("SELECT line, version, values_json, values_hash, changed_by, reason, restored_from FROM audit_threshold_sets ORDER BY line").fetchall()
    assert [r[0] for r in rows] == ["DT", "SWC", "SWO"]
    for line, version, text, digest, changed_by, reason, restored_from in rows:
        years, trades = BY_LINE[line]
        values = json.loads(text)
        assert set(values) == MEASURE_KEYS and version == 1 and changed_by == "system:seed" and reason is None and restored_from is None
        assert values == {**SECTION_6, "min_years": years, "min_trades": trades}
        assert values["edge_warns"] is True and isinstance(values["min_trades"], int)
        assert digest == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert text == json.dumps(values, sort_keys=True, separators=(",", ":"))           # the canonical form thresholds.save writes
    assert {r[0] for r in raw().execute("SELECT line FROM audit_threshold_sets")} == set(BY_LINE)       # LP and RF have no set


def test_the_postgres_script_has_the_six_triggers_on_the_shared_function():
    script = render(FILE.read_text(encoding="utf-8"), PostgresBackend("postgresql://u@h/db"))
    statements = [s for s in PostgresBackend("postgresql://u@h/db").split_statements(script) if s.startswith("CREATE TRIGGER")]
    assert len(statements) == 6 and all("FOR EACH ROW EXECUTE FUNCTION hokma_trials_append_only()" in s for s in statements)
    assert {s.split()[6] for s in statements} == set(APPEND_ONLY) and "RAISE(ABORT" not in script
    sqlite = render(FILE.read_text(encoding="utf-8"), SqliteBackend(FILE))
    assert sqlite.count("CREATE TRIGGER") == 6 and "hokma_trials_append_only" not in sqlite


def test_the_counts_of_the_copy_see_the_four_tables_and_the_agent_role_cannot_write_them(env):
    migrate()
    con = raw()
    con.execute("INSERT INTO audit_answers (scope, subject, question_key, answer, answered_by) VALUES ('data', 'FUT:NQ', 'point_in_time', 'yes', 'owner')")
    con.commit()
    counts = transfer.sample_counts(SqliteBackend(db_path()))
    assert [counts[t] for t in transfer.VERDICT_TABLES] == [3, 1, 0, 0]
    source = (MIGRATIONS_DIR.parent / "transfer.py").read_text(encoding="utf-8")
    assert "PLANTEL_TABLES + BACKTEST_TABLES + VERDICT_TABLES" in source          # roles: the agents read these tables, they never write them


def test_sqlite_to_postgres_and_back_keeps_the_verdict_tables_and_the_triggers(env, pg_url):
    migrate()
    source = SqliteBackend(db_path())
    con = raw()
    con.execute("INSERT INTO audit_answers (scope, subject, question_key, answer, answered_by) VALUES ('data', 'FUT:NQ', 'point_in_time', 'yes', 'owner')")
    con.execute("INSERT INTO library_requests (name, description, requested_by) VALUES ('vwap_band', 'an indicator the library lacks, with words', 'ian')")
    con.commit()
    target = PostgresBackend(pg_url)
    result = transfer.transfer(source, target)
    assert [result.counts[t] for t in transfer.VERDICT_TABLES] == [3, 1, 0, 1]
    session = target.connect()
    try:
        assert session.query("SELECT line, version, values_json, changed_by FROM audit_threshold_sets ORDER BY line") == \
            [{"line": line, "version": 1, "values_json": text, "changed_by": "system:seed"}
             for line, text in raw().execute("SELECT line, values_json FROM audit_threshold_sets ORDER BY line").fetchall()]
        for table in ("audit_threshold_sets", "audit_answers"):          # the tables that hold a row after the copy: the trigger is per row
            session.begin(write=True)
            for statement in (f"UPDATE {table} SET id = id", f"DELETE FROM {table}"):
                with pytest.raises(Exception, match="append-only"):
                    session.execute(statement)
                session.rollback()
                session.begin(write=True)
            session.rollback()
        session.begin(write=True)                                                           # the sequence continues after the copy
        assert session.insert("audit_answers", {"scope": "data", "subject": "FUT:ES", "question_key": "point_in_time", "answer": "no", "answered_by": "owner"}) == 2
        session.rollback()
    finally:
        session.close()
