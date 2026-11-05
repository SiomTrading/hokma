"""Database engine in use, read from the real configuration (`database.json`, written by D11's switch).

The setup does not run its own connection test or migration: the panel calls `POST /api/database/test` and
`POST /api/database/migrate` (hokma.db.api). What this module adds is keeping `profile.database_engine` equal to
the engine that is really in use, because the profile row travels with the data and says "sqlite" after a copy.
"""
from __future__ import annotations

from hokma.db import dbconfig


def engine_in_use() -> str:
    return dbconfig.load_config().engine


def location_shown() -> str | None:
    """`postgresql://user@host:port/database` (no password) for Postgres; None for the local file."""
    config = dbconfig.load_config()
    return dbconfig.display_url(config.url) if config.engine == "postgres" and config.url else None
