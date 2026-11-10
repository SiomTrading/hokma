"""D11: Postgres engine, connection test, change-and-migrate in both directions, endpoints and secrecy of the password.

Tests marked with the `pg_*` fixtures need a real Postgres (HOKMA_TEST_POSTGRES_URL, see pgsupport.py) and are
skipped with an explicit reason without one. Everything else runs against SQLite or without a database.
"""
import json
import logging
import os
import stat
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from dbsupport import HUMAN, agent, env  # noqa: F401  (fixture)
from fastapi.testclient import TestClient
from pgsupport import pg_admin_url, pg_template, pg_url, pg_url_migrated, with_database  # noqa: F401  (fixtures)

from hokma.core.app import create_app
from hokma.db import dbconfig, get_database, migrate
from hokma.db.backend import SqliteBackend
from hokma.db.dbconfig import DatabaseConfig, load_config, save_config
from hokma.db.errors import RuleViolation, ValidationError
from hokma.db.paths import database_config_path, db_path
from hokma.db.postgres import PostgresBackend, convert_sql
from hokma.db.probe import probe_connection
from hokma.db.transfer import TransferError, sample_counts, transfer
from hokma.rpc.ws import SESSION_TOKEN

HEADERS = {"X-Hokma-Token": SESSION_TOKEN}
SECRET = "s3cr3t-P@ss:w/rd"
SECRET_ENCODED = "s3cr3t-P%40ss%3Aw%2Frd"


@pytest.fixture(scope="session", autouse=True)
def _drop_roles_the_tests_created():
    """Every move to Postgres creates the cluster-wide roles hokma_app and hokma_agent when it may. The test server
    is left as it was found: roles that did not exist before the run are dropped at the end."""
    url = os.environ.get("HOKMA_TEST_POSTGRES_URL", "").strip()
    existing: set[str] = set()
    if url:
        try:
            with psycopg.connect(url, autocommit=True, connect_timeout=5) as admin:
                existing = {r[0] for r in admin.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'hokma%'")}
        except psycopg.Error:
            url = ""
    yield
    if url:
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as admin:
            for role in ("hokma_app", "hokma_agent"):
                if role not in existing:
                    admin.execute(f"DROP ROLE IF EXISTS {role}")


# ------------------------------------------------------------------------------------------ no server needed

def test_url_is_validated_and_normalised():
    assert dbconfig.parse_url(" postgres://u:p@h:5432/db?sslmode=require ") == "postgresql://u:p@h:5432/db?sslmode=require"
    for bad, code in [("", "url_empty"), (None, "url_empty"), ("mysql://u@h/db", "url_scheme"),
                      ("postgresql:///db", "url_host"), ("postgresql://u@h", "url_database"),
                      ("postgresql://u@h/db?passfile=/etc/passwd", "url_option"),
                      ("postgresql://u@h/db?sslcert=/x", "url_option"),
                      ("postgresql://u@h/db?options=-c%20log_statement=all", "url_option"),
                      ("postgresql://u@h/db?sslmode=banana", "url_option"),
                      ("postgresql://u@h:99999/db", "url_invalid"), ("postgresql://u@h/d b", "url_invalid"),
                      ("postgresql://u@h/d\nb", "url_invalid"), ("x" * 2000, "url_invalid")]:
        with pytest.raises(ValidationError) as caught:
            dbconfig.parse_url(bad)
        assert caught.value.args[0] == code, bad


def test_the_password_never_survives_redact_or_display():
    url = f"postgresql://hokma:{SECRET_ENCODED}@db.example.com:5432/fund"
    assert dbconfig.password_of(url) == SECRET
    shown = dbconfig.display_url(url)
    assert shown == "postgresql://hokma@db.example.com:5432/fund"
    text = f"could not connect with {url} using {SECRET} and {SECRET_ENCODED}"
    cleaned = dbconfig.redact(text, url)
    assert SECRET not in cleaned and SECRET_ENCODED not in cleaned and "***" in cleaned
    assert SECRET not in repr(PostgresBackend(url)) and SECRET_ENCODED not in repr(PostgresBackend(url))


def test_named_parameters_become_psycopg_parameters_outside_literals():
    assert convert_sql("SELECT :a, ':b', x::int, \"c:d\" FROM t WHERE y LIKE '%x%' AND z = :z") == \
        "SELECT %(a)s, ':b', x::int, \"c:d\" FROM t WHERE y LIKE '%%x%%' AND z = %(z)s"


def test_statements_are_split_around_function_bodies():
    script = ("CREATE TABLE a (x text);\n-- comment; with semicolon\nCREATE FUNCTION f() RETURNS trigger AS $$\n"
              "BEGIN RETURN NEW; END $$ LANGUAGE plpgsql;\nINSERT INTO a VALUES ('a;b');")
    parts = list(PostgresBackend("postgresql://u@h/db").split_statements(script))
    assert len(parts) == 3 and "RETURN NEW; END" in parts[1] and parts[2].endswith("'a;b')")


def test_config_file_is_owner_only_and_round_trips(env):
    assert load_config() == DatabaseConfig("sqlite", None)
    save_config(DatabaseConfig("postgres", f"postgresql://u:{SECRET_ENCODED}@h:5432/db"))
    path = database_config_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_config() == DatabaseConfig("postgres", f"postgresql://u:{SECRET_ENCODED}@h:5432/db")
    path.write_text("{ not json", encoding="utf-8")
    assert load_config().engine == "sqlite"                   # a damaged file never locks the app out
    path.write_text(json.dumps({"engine": "postgres", "url": "mysql://x@y/z"}), encoding="utf-8")
    assert load_config() == DatabaseConfig("sqlite", None)


def test_probe_reports_url_mistakes_without_connecting(env):
    result = probe_connection("not a url")
    assert not result.ok and result.code == "url_invalid" and result.message


def test_probe_unreachable_server_has_a_clear_message(env):
    # Port 1 on localhost refuses immediately.
    result = probe_connection(f"postgresql://u:{SECRET_ENCODED}@127.0.0.1:1/db")
    assert not result.ok and result.code in ("refused", "unreachable")
    assert SECRET not in json.dumps(result.to_dict()) and SECRET_ENCODED not in json.dumps(result.to_dict())
    unknown = probe_connection(f"postgresql://u:{SECRET_ENCODED}@no-such-host.invalid:5432/db")
    assert not unknown.ok and unknown.code in ("host", "unreachable", "timeout")
    assert SECRET not in json.dumps(unknown.to_dict())


# ---------------------------------------------------------------------------------------- real Postgres: probe

def test_probe_ok_reports_server_database_and_rights(pg_url):
    result = probe_connection(pg_url)
    assert result.ok and result.code == "ok" and result.can_create_tables is True
    assert result.database == pg_url.rsplit("/", 1)[1] and result.server_version
    assert result.has_data is False and result.notes


def test_probe_wrong_database_name(pg_url, pg_admin_url):
    result = probe_connection(with_database(pg_admin_url, "hokma_does_not_exist_zz"))
    assert not result.ok and result.code == "no_database"


@pytest.fixture
def password_role(pg_admin_url):
    """A login role with a password that needs it; skips when the server trusts every local connection."""
    if "127.0.0.1" not in pg_admin_url and "localhost" not in pg_admin_url:
        pytest.skip("password test needs a server on 127.0.0.1 or localhost")
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute("DROP ROLE IF EXISTS hokma_pw")
        admin.execute(sql.SQL("CREATE ROLE hokma_pw LOGIN PASSWORD {}").format(sql.Literal(SECRET)))
    host = pg_admin_url.split("@")[1].split("/")[0]
    try:
        yield lambda password: f"postgresql://hokma_pw:{password}@{host}/postgres"
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute("DROP ROLE IF EXISTS hokma_pw")


def test_probe_wrong_password_is_explained_and_not_echoed(password_role, caplog):
    wrong = password_role("wrong-Passw0rd")
    try:
        psycopg.connect(password_role(SECRET_ENCODED), connect_timeout=5).close()
    except psycopg.Error:
        pytest.skip("the server does not enforce passwords on this address (trust authentication)")
    with caplog.at_level(logging.DEBUG):
        bad = probe_connection(wrong)
        good = probe_connection(password_role(SECRET_ENCODED))
    assert not bad.ok and bad.code == "auth" and "wrong-Passw0rd" not in json.dumps(bad.to_dict())
    assert good.code != "auth"                                 # logged in (this role may not create tables here)
    assert SECRET not in caplog.text and SECRET_ENCODED not in caplog.text and "wrong-Passw0rd" not in caplog.text


# --------------------------------------------------------------------------------------- data to move around

def seed(database):
    """A little of everything, including a self-reference (decisions) and every append-only table."""
    with database.write() as u:
        for slug in ("hokma", "joel", "vera"):
            u.agents.upsert_kit(slug, slug.capitalize(), f"{slug} agent", "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
        u.profile.save(owner_name="Ana", fund_name="Fondo", language="en", weekly_token_cap=100000)
        u.profile.complete_setup()
        project = u.projects.create("Alpha", "/work/alpha", kind="research")
        parent = u.tasks.create("parent", actor=HUMAN, project_id=project.id, assigned_agent="joel",
                                requires_review=True, reviewer_agent="vera")
        child = u.tasks.create("child", actor=HUMAN, project_id=project.id, assigned_agent="joel", parents=[parent.id])
        running = u.tasks.claim(parent.id, "joel")
        u.task_runs.start(running.id, "joel", model="sonnet")
        u.tasks.request_review(parent.id, "done, check it", actor=agent("joel"))
        u.tasks.review_approve(parent.id, actor=agent("vera"))
        u.task_comments.add(child.id, "note \u00e9\u00f1 \u2713", actor=HUMAN)
        money = u.tasks.create("trade", actor=HUMAN, assigned_agent="joel", touches_real_money=True)
        u.tasks.claim(money.id, "joel")
        u.tasks.complete(money.id, "ready", actor=agent("joel"))
        u.tasks.approve(money.id, decided_by="owner", channel="cli")
        old = u.decisions.propose("topic one", "do A", proposed_by="joel", project_id=project.id)
        new = u.decisions.propose("topic one v2", "do B", proposed_by="joel")
        u.session.execute("UPDATE decisions SET status = 'superseded', superseded_by = :n WHERE id = :o",
                          {"n": new.id, "o": old.id})
        u.journal.add("note", "first entry", actor=HUMAN, body="x" * 50, refs={"task": child.id})
        u.guard_events.record("broker", "blocked", "real order", agent_slug="joel", tool="bash")
        u.bugs.create("a bug", actor=HUMAN, severity="high", project_id=project.id)
        conv = u.conversations.create(engine="claude_code", title="Chat", project_id=project.id, agent_slug="joel")
        msg = u.messages.add(conv.id, "user", "hello \u00e9", tokens_in=10, tokens_out=0)
        u.messages.add(conv.id, "assistant", "hi", agent_slug="joel", model="sonnet", tokens_in=0, tokens_out=25)
        u.attachments.add(kind="image", stored_path="/data/a.png", mime_type="image/png", size_bytes=3,
                          sha256="a" * 64, message_id=msg.id)
        u.extensions.add("ext-a", "market", "plugin", enabled=True)
        u.extension_requests.create("install", "plugin", "ext-b", "market", "needed", actor=agent("joel"))
        u.scheduled_jobs.create("daily", "joel", "do it", "cron", "0 9 * * *", "UTC")
        u.onboarding.mark("first_chat")
        u.license_checks.record("valid", latest_version="1.0.0")
    return database


def snapshot(backend):
    """Every table, row by row, as comparable Python values."""
    session = backend.connect()
    try:
        out = {}
        for table in sorted(sample_counts(backend)):
            if table == "schema_version":                       # the moment each migration ran is not data
                continue
            rows = sorted(session.query(f"SELECT * FROM {table}"), key=lambda r: [str(v) for v in r.values()])
            out[table] = [{k: v for k, v in r.items() if k != "database_engine"} for r in rows]
        return out
    finally:
        session.close()


@pytest.fixture
def seeded(env):
    migrate()
    return seed(get_database())


# ------------------------------------------------------------------------------------ change and migrate

def test_sqlite_to_postgres_and_back_keeps_every_row(seeded, pg_url, env):
    source = SqliteBackend(db_path())
    before = snapshot(source)
    sizes = sample_counts(source)
    assert sizes["tasks"] == 3 and sizes["task_events"] > 3 and sizes["decisions"] == 2 and sizes["messages"] == 2

    target = PostgresBackend(pg_url)
    result = transfer(source, target)
    assert result.counts == {k: v for k, v in sizes.items()} and result.schema_version == 6
    assert sample_counts(target) == sizes
    assert snapshot(target) == before                         # same values, same ids, same timestamps
    # the destination says it is Postgres; the origin was not touched
    session = target.connect()
    try:
        assert session.scalar("SELECT database_engine FROM profile WHERE id = 1") == "postgres"
    finally:
        session.close()
    assert snapshot(source) == before

    # and back: the old local file still holds data, so it must be confirmed
    with pytest.raises(TransferError) as refused:
        transfer(target, source)
    assert refused.value.code == "target_not_empty"
    back = transfer(target, source, replace=True)
    assert back.previous_copy and back.previous_copy.is_file()
    assert sample_counts(source) == sizes
    assert snapshot(source) == before
    assert not Path(str(db_path()) + ".migrating").exists()


def test_triggers_and_counters_work_after_the_move(seeded, pg_url, env):
    transfer(SqliteBackend(db_path()), PostgresBackend(pg_url))
    save_config(DatabaseConfig("postgres", pg_url))
    database = get_database()
    assert database.engine == "postgres"
    with database.write() as u:
        events = len(u.task_events.recent(1000))
        assert events == sample_counts(PostgresBackend(pg_url))["task_events"]        # no duplicates from the copy
        new = u.tasks.create("after", actor=HUMAN, assigned_agent="joel")             # identity continues after max(id)
        assert new.id > 3
        assert len(u.task_events.recent(1000)) == events + 1                         # the event trigger is alive
        u.tasks.claim(new.id, "joel")
        with pytest.raises(RuleViolation):                                           # append-only still holds
            u.session.execute("DELETE FROM journal_entries")
        t = u.tasks.create("money", actor=HUMAN, assigned_agent="joel", touches_real_money=True)
        u.tasks.claim(t.id, "joel")
        with pytest.raises(RuleViolation):                                           # a real-money task cannot skip approval
            u.session.execute("UPDATE tasks SET status = 'done', changed_by_kind = 'agent', changed_by = 'joel', "
                              "completed_at = '2026-01-01 00:00:00' WHERE id = :i", {"i": t.id})


def test_a_failing_copy_to_postgres_leaves_the_destination_untouched(seeded, pg_url, env):
    source = SqliteBackend(db_path())
    with get_database().write() as u:                         # SQLite accepts a NUL in text; Postgres refuses it
        u.session.execute("UPDATE messages SET content = :c WHERE id = 1", {"c": "bad\x00text"})
    target = PostgresBackend(pg_url)
    with pytest.raises(TransferError) as failed:
        transfer(source, target)
    assert failed.value.code == "failed"
    session = target.connect()
    try:
        assert not session.table_exists("tasks") and not session.table_exists("schema_version")
    finally:
        session.close()


def test_a_failing_copy_to_sqlite_keeps_the_old_file(seeded, pg_url, env):
    source = SqliteBackend(db_path())
    pg = PostgresBackend(pg_url)
    transfer(source, pg)
    before = snapshot(source)
    session = pg.connect()
    try:
        session.begin(write=True)
        session.execute("ALTER TABLE messages DISABLE TRIGGER USER")
        session.execute("ALTER TABLE profile DROP CONSTRAINT IF EXISTS profile_language_check")
        session.execute("UPDATE profile SET language = 'fr' WHERE id = 1")      # the SQLite CHECK will refuse it
        session.commit()
    finally:
        session.close()
    with pytest.raises(TransferError) as failed:
        transfer(pg, source, replace=True)
    assert failed.value.code == "failed"
    assert snapshot(source) == before
    assert not Path(str(db_path()) + ".migrating").exists()


def test_postgres_destination_with_data_needs_replace(seeded, pg_url, env):
    source = SqliteBackend(db_path())
    target = PostgresBackend(pg_url)
    transfer(source, target)
    save_config(DatabaseConfig("sqlite", pg_url))
    with pytest.raises(TransferError) as refused:
        transfer(source, target)
    assert refused.value.code == "target_not_empty"
    assert transfer(source, target, replace=True).rows == sum(sample_counts(source).values())
    with pytest.raises(TransferError) as same:
        transfer(source, SqliteBackend(env / "other.db"))
    assert same.value.code == "same_engine"


def test_roles_are_created_when_allowed(seeded, pg_url, pg_admin_url, env):
    result = transfer(SqliteBackend(db_path()), PostgresBackend(pg_url))
    assert result.roles in ("created", "exists")
    with psycopg.connect(pg_url, autocommit=True) as admin:
        can = admin.execute("SELECT has_table_privilege('hokma_agent', 'approvals', 'INSERT') AS a, "
                            "has_table_privilege('hokma_agent', 'tasks', 'UPDATE') AS b").fetchone()
    assert can == (False, True)


def test_a_user_who_cannot_create_roles_is_used_as_is(pg_admin_url, pg_url):
    from hokma.db.transfer import provision_roles

    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute("DROP ROLE IF EXISTS hokma_plain")
        admin.execute("CREATE ROLE hokma_plain LOGIN")
        admin.execute(f"GRANT ALL ON DATABASE {pg_url.rsplit('/', 1)[1]} TO hokma_plain")
    try:
        host = pg_admin_url.split("@")[1].split("/")[0]
        plain = PostgresBackend(f"postgresql://hokma_plain@{host}/{pg_url.rsplit('/', 1)[1]}")
        assert provision_roles(plain) == "skipped"
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(f"REVOKE ALL ON DATABASE {pg_url.rsplit('/', 1)[1]} FROM hokma_plain")
            admin.execute("DROP ROLE IF EXISTS hokma_plain")


# ----------------------------------------------------------------------------------------------- endpoints

def test_endpoints_need_the_session_token(env):
    client = TestClient(create_app())
    assert client.get("/api/database").status_code == 401
    assert client.post("/api/database/test", json={"url": "postgresql://u@h/d"}).status_code == 401
    assert client.post("/api/database/migrate", json={"target": "sqlite"}).status_code == 401


def test_status_of_the_default_database(env):
    migrate()
    body = TestClient(create_app(), headers=HEADERS).get("/api/database").json()
    assert body["engine"] == "sqlite" and body["reachable"] and body["schema_version"] == 6
    assert body["location"].endswith("hokma.db") and body["saved_url"] is None


def test_test_endpoint_rejects_bad_addresses_in_words(env):
    client = TestClient(create_app(), headers=HEADERS)
    for url, code in [("mysql://u@h/d", "url_scheme"), ("", "url_empty")]:
        body = client.post("/api/database/test", json={"url": url}).json()
        assert body["ok"] is False and body["code"] == code and body["message"]
    assert client.post("/api/database/test", json={"url": "x" * 5000}).status_code == 422


def test_switch_endpoint_validations(env):
    migrate()
    client = TestClient(create_app(), headers=HEADERS)
    no_url = client.post("/api/database/migrate", json={"target": "postgres"})
    assert no_url.status_code == 400 and no_url.json()["detail"]["code"] == "no_url"
    bad = client.post("/api/database/migrate", json={"target": "postgres", "url": "mysql://u@h/d"})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "url_scheme"
    same = client.post("/api/database/migrate", json={"target": "sqlite"})
    assert same.status_code == 409 and same.json()["detail"]["code"] == "same_engine"
    assert client.post("/api/database/migrate", json={"target": "mongo"}).status_code == 422


def test_unreachable_destination_over_http_is_clear_and_secret_free(env, caplog):
    migrate()
    client = TestClient(create_app(), headers=HEADERS)
    with caplog.at_level(logging.DEBUG):
        response = client.post("/api/database/migrate",
                               json={"target": "postgres", "url": f"postgresql://u:{SECRET_ENCODED}@127.0.0.1:1/db"})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "target_unreachable"
    assert SECRET not in response.text and SECRET_ENCODED not in response.text
    assert SECRET not in caplog.text and SECRET_ENCODED not in caplog.text
    assert load_config().engine == "sqlite" and not database_config_path().exists()   # nothing was switched


@pytest.fixture
def pg_password_url(pg_admin_url):
    """A database owned by a role whose password is SECRET; skips when the server does not enforce passwords."""
    if "127.0.0.1" not in pg_admin_url and "localhost" not in pg_admin_url:
        pytest.skip("password test needs a server on 127.0.0.1 or localhost")
    host = pg_admin_url.split("@")[1].split("/")[0]
    name = "hokma_pw_db"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.execute("DROP ROLE IF EXISTS hokma_pw")
        admin.execute(sql.SQL("CREATE ROLE hokma_pw LOGIN PASSWORD {}").format(sql.Literal(SECRET)))
        admin.execute(f"CREATE DATABASE {name} OWNER hokma_pw")
    url = f"postgresql://hokma_pw:{SECRET_ENCODED}@{host}/{name}"
    try:
        try:
            psycopg.connect(f"postgresql://hokma_pw:wrong@{host}/{name}", connect_timeout=5).close()
            pytest.skip("the server does not enforce passwords on this address (trust authentication)")
        except psycopg.OperationalError:
            pass
        yield url
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            admin.execute("DROP ROLE IF EXISTS hokma_pw")


def test_full_switch_over_http_and_back(seeded, pg_password_url, env, caplog):
    password_url = pg_password_url
    app = create_app()
    sizes = sample_counts(SqliteBackend(db_path()))
    with TestClient(app, headers=HEADERS) as client, caplog.at_level(logging.DEBUG):
        assert app.state.db.engine == "sqlite" and app.state.db_error is None
        tested = client.post("/api/database/test", json={"url": password_url}).json()
        assert tested["ok"] is True and tested["can_create_tables"] is True

        moved = client.post("/api/database/migrate", json={"target": "postgres", "url": password_url})
        assert moved.status_code == 200, moved.text
        body = moved.json()
        assert body["ok"] and body["engine"] == "postgres" and body["counts"] == sizes and body["message"]
        assert body["location"] == dbconfig.display_url(load_config().url) and SECRET not in json.dumps(body)
        assert app.state.db.engine == "postgres" and get_database().engine == "postgres"
        assert stat.S_IMODE(os.stat(database_config_path()).st_mode) == 0o600
        assert SECRET_ENCODED in database_config_path().read_text(encoding="utf-8")      # only here

        status = client.get("/api/database").json()
        assert status["engine"] == "postgres" and status["reachable"] and status["schema_version"] == 6
        assert SECRET not in json.dumps(status) and SECRET_ENCODED not in json.dumps(status)
        assert SECRET not in json.dumps(tested) and SECRET_ENCODED not in json.dumps(tested)

        again = client.post("/api/database/migrate", json={"target": "postgres", "url": password_url})
        assert again.status_code == 409 and again.json()["detail"]["code"] == "same_engine"

        refused = client.post("/api/database/migrate", json={"target": "sqlite"})
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "target_not_empty"
        back = client.post("/api/database/migrate", json={"target": "sqlite", "replace": True})
        assert back.status_code == 200 and back.json()["engine"] == "sqlite"
        assert app.state.db.engine == "sqlite"
    assert load_config().engine == "sqlite"
    assert sample_counts(SqliteBackend(db_path())) == sizes
    assert SECRET not in caplog.text and SECRET_ENCODED not in caplog.text
    for found in db_path().parent.rglob("*"):                  # the password is in database.json and nowhere else
        if found.is_file() and found != database_config_path():
            assert SECRET.encode() not in found.read_bytes() and SECRET_ENCODED.encode() not in found.read_bytes()


def test_app_starts_with_postgres_down_and_says_why(env):
    save_config(DatabaseConfig("postgres", f"postgresql://u:{SECRET_ENCODED}@127.0.0.1:1/db"))
    app = create_app()
    with TestClient(app, headers=HEADERS) as client:
        assert client.get("/health").status_code == 200
        assert app.state.db_error and SECRET not in app.state.db_error and SECRET_ENCODED not in app.state.db_error
        status = client.get("/api/database").json()
        assert status["engine"] == "postgres" and status["reachable"] is False
        assert SECRET not in json.dumps(status) and SECRET_ENCODED not in json.dumps(status)
        assert status["saved_url"] == "postgresql://u@127.0.0.1:1/db"


def test_lifespan_opens_and_migrates_the_database(env):
    app = create_app()
    assert not db_path().exists()
    with TestClient(app):
        assert app.state.db.engine == "sqlite" and db_path().is_file()
        with app.state.db.read() as u:
            assert u.profile.get_or_default().exists is False
