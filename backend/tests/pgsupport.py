"""Real Postgres for the tests (D11).

The address of a server where the tests may create and drop databases comes from HOKMA_TEST_POSTGRES_URL, for
example `postgresql://postgres@127.0.0.1:5432/postgres`. Without it every test that needs Postgres is SKIPPED
with a message that says so. Each test gets its own throwaway database, dropped afterwards.
"""
import os
import secrets
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

ENV_VAR = "HOKMA_TEST_POSTGRES_URL"
SKIP_REASON = f"no Postgres for the tests: set {ENV_VAR}=postgresql://user@host:port/postgres (a server where databases may be created)"


def with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, ""))


@pytest.fixture(scope="session")
def pg_admin_url():
    url = os.environ.get(ENV_VAR, "").strip()
    if not url:
        pytest.skip(SKIP_REASON)
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        pytest.skip(f"{ENV_VAR} is set but the server cannot be reached: {exc}")
    return url


@pytest.fixture(scope="session")
def pg_template(pg_admin_url):
    """A database with the schema already applied (no seeds), used as a template to make test databases fast."""
    from hokma.db.migrate import migrate
    from hokma.db.postgres import PostgresBackend

    name = "hokma_tpl_" + secrets.token_hex(6)
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    migrate(backend=PostgresBackend(with_database(pg_admin_url, name)), seed=False)
    yield name
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _make(pg_admin_url, template=None):
    name = "hokma_t_" + secrets.token_hex(6)
    suffix = f" TEMPLATE {template}" if template else ""
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}{suffix}")
    return name


def _drop(pg_admin_url, name):
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def pg_url(pg_admin_url):
    """URL of a new empty database on the test server; dropped at the end."""
    name = _make(pg_admin_url)
    try:
        yield with_database(pg_admin_url, name)
    finally:
        _drop(pg_admin_url, name)


@pytest.fixture
def pg_url_migrated(pg_admin_url, pg_template):
    """URL of a new database that already has the schema (copy of the template); dropped at the end."""
    name = _make(pg_admin_url, pg_template)
    try:
        yield with_database(pg_admin_url, name)
    finally:
        _drop(pg_admin_url, name)
