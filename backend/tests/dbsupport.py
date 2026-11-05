"""Shared fixtures for the database tests (import them into a test module)."""
import pytest

from hokma.db import Actor, get_database, migrate

HUMAN = Actor.human("owner")
SYSTEM = Actor.system()


def agent(slug: str) -> Actor:
    return Actor.agent(slug)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOKMA_LANG", "en")
    return tmp_path


@pytest.fixture
def db(env):
    """A migrated database in a temporary data folder, with three agents ready to work."""
    migrate()
    database = get_database()
    with database.write() as u:
        for slug in ("hokma", "joel", "vera", "iris"):
            u.agents.upsert_kit(slug, slug.capitalize(), None, "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    return database
