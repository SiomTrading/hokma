"""Where the database lives: inside the protected data folder, never in the working folder."""
from pathlib import Path

from hokma.core.config import hokma_data_dir

DB_FILE = "hokma.db"


def db_path() -> Path:
    return hokma_data_dir() / DB_FILE


def backups_dir(path: Path | None = None) -> Path:
    """Backups sit next to the database file (`<data>/backups/` for the default one)."""
    return (path or db_path()).parent / "backups"


def database_config_path() -> Path:
    """Which engine is active and, for Postgres, the connection URL (with its password). Owner-only file in the
    protected data folder: never inside the database, never in the working folder."""
    return hokma_data_dir() / "database.json"
