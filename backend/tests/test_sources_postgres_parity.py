"""The readers of sources (SPEC-030, A1-4 and A1-5), run again against a real Postgres: a case that passes on SQLite and fails here, or the other way round, is a bug.

The tests are the ones of test_ingest; only the `db` fixture is replaced by a Postgres-backed one."""
import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)
from sources.test_ingest import *  # noqa: F401,F403
from sources.test_ingest import local, server  # noqa: F401  (fixtures that import * leaves out)
from sources.test_transcript import *  # noqa: F401,F403

from hokma.db import get_database, migrate
from hokma.db.dbconfig import DatabaseConfig, save_config


@pytest.fixture
def db(env, pg_url_migrated):  # noqa: F811
    save_config(DatabaseConfig("postgres", pg_url_migrated))
    migrate()
    database = get_database()
    assert database.engine == "postgres"
    with database.write() as u:
        for slug in ("hokma", "joel", "vera", "iris"):
            u.agents.upsert_kit(slug, slug.capitalize(), None, "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    return database
