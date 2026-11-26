"""The way back from migrations 0026, 0025 and 0024 (SPEC-025 delivery 3, SPEC-027 pieces 027c and 027a). Go back with rollback_0026 first, then rollback_0025, then rollback_0024.

0026: the layout of a desk (the panels of its screen).
0025: a desk without capital. 0024: many accounts per desk and many desks per account.

The migration runner only goes forward, so this is the tested path to `desk_accounts` (one account per desk, one desk per
account). It refuses, and changes nothing, when the data does not fit that shape: a desk with more than one account or an
account on more than one desk. The refusal carries the list of desks and accounts that block it.

One transaction: the table is rebuilt from `desk_account_links`, the new tables and columns go, and the version row is removed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from hokma.db.backend import Backend, Session, SqliteBackend
from hokma.db.errors import DbError, MigrationError
from hokma.db.migrate import _version_of
from hokma.db.paths import db_path

VERSION = 24

_DESK_ACCOUNTS = """CREATE TABLE desk_accounts (
  desk_id    INTEGER NOT NULL PRIMARY KEY REFERENCES desks(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL UNIQUE REFERENCES broker_accounts(id) ON DELETE RESTRICT
)"""


@dataclass(frozen=True)
class Blocker:
    """Why the way back is refused: a desk with many accounts or an account on many desks."""
    kind: str                       # 'desk_with_many_accounts' | 'account_on_many_desks'
    desk_ids: tuple[int, ...]
    account_ids: tuple[int, ...]


class RollbackRefused(DbError):
    """The data does not fit one account per desk and one desk per account. Nothing was changed."""

    def __init__(self, blockers: list[Blocker]) -> None:
        self.blockers = blockers
        lines = [f"{b.kind}: desks {list(b.desk_ids)}, accounts {list(b.account_ids)}" for b in blockers]
        super().__init__("cannot go back to one account per desk: " + "; ".join(lines))


def blockers(session: Session) -> list[Blocker]:
    """Desks with more than one account and accounts on more than one desk, in id order."""
    found: list[Blocker] = []
    for row in session.query("SELECT desk_id FROM desk_account_links GROUP BY desk_id HAVING count(*) > 1 ORDER BY desk_id"):
        accounts = session.query("SELECT account_id FROM desk_account_links WHERE desk_id = :d ORDER BY account_id", {"d": row["desk_id"]})
        found.append(Blocker("desk_with_many_accounts", (row["desk_id"],), tuple(a["account_id"] for a in accounts)))
    for row in session.query("SELECT account_id FROM desk_account_links GROUP BY account_id HAVING count(*) > 1 ORDER BY account_id"):
        desks = session.query("SELECT desk_id FROM desk_account_links WHERE account_id = :a ORDER BY desk_id", {"a": row["account_id"]})
        found.append(Blocker("account_on_many_desks", tuple(d["desk_id"] for d in desks), (row["account_id"],)))
    return found


def downgrade(session: Session) -> None:
    """The steps, inside the caller's open write transaction. Raises RollbackRefused before changing anything."""
    refused = blockers(session)
    if refused:
        raise RollbackRefused(refused)
    session.execute(_DESK_ACCOUNTS)
    session.execute("INSERT INTO desk_accounts (desk_id, account_id) SELECT desk_id, account_id FROM desk_account_links")
    session.execute("DROP TABLE desk_strategy_accounts")
    session.execute("DROP TABLE desk_account_links")
    session.execute("ALTER TABLE exec_proposals DROP COLUMN account_id")
    session.execute("ALTER TABLE exec_proposals DROP COLUMN strategy_key")
    session.execute("ALTER TABLE broker_orders DROP COLUMN strategy_key")
    session.execute("DELETE FROM schema_version WHERE version = :v", {"v": VERSION})


def rollback_0024(path: Path | None = None, *, backend: Backend | None = None) -> int:
    """Take the database from version 24 back to 23. Returns the version it is at after. Raises RollbackRefused (nothing changed)
    or MigrationError (it is not at version 24 or the steps failed; the transaction was rolled back)."""
    path = Path(path) if path else db_path()
    backend = backend or SqliteBackend(path)
    session = backend.connect()
    try:
        session.begin(write=True)
        try:
            current = _version_of(session)
            if current != VERSION:
                raise MigrationError(f"the database is at version {current}; the way back from {VERSION} needs exactly {VERSION}", current)
            downgrade(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return _version_of(session)
    finally:
        session.close()


# ---- 0025 (SPEC-027, piece 027c): desks.capital optional --------------------------------------------------------------------------

VERSION_0025 = 25
_CAPITAL_CHECK = re.compile(r"(\bcapital\s+INTEGER)\s+(CHECK\b)", re.IGNORECASE)


class CapitalRequired(DbError):
    """Version 24 wants a capital on every desk. Nothing was changed. `desk_ids` are the desks that have none."""

    def __init__(self, desk_ids: list[int]) -> None:
        self.desk_ids = desk_ids
        super().__init__(f"cannot go back: desks {desk_ids} have no capital; set one in each before going back")


def downgrade_0025(session: Session) -> None:
    """Steps inside the caller's open write transaction. Raises CapitalRequired before changing anything."""
    empty = [r["id"] for r in session.query("SELECT id FROM desks WHERE capital IS NULL ORDER BY id")]
    if empty:
        raise CapitalRequired(empty)
    if session.engine == "sqlite":
        from hokma.db.datasteps import _rewrite_desks_definition

        _rewrite_desks_definition(session, lambda sql: _CAPITAL_CHECK.sub(r"\1 NOT NULL \2", sql, count=1))
    else:
        session.execute("ALTER TABLE desks ALTER COLUMN capital SET NOT NULL")
    session.execute("DELETE FROM schema_version WHERE version = :v", {"v": VERSION_0025})


def rollback_0025(path: Path | None = None, *, backend: Backend | None = None) -> int:
    """Take the database from version 25 back to 24. Returns the version it is at after. Raises CapitalRequired (nothing changed)
    or MigrationError (it is not at version 25 or the steps failed; the transaction was rolled back)."""
    path = Path(path) if path else db_path()
    backend = backend or SqliteBackend(path)
    session = backend.connect()
    try:
        session.begin(write=True)
        try:
            current = _version_of(session)
            if current != VERSION_0025:
                raise MigrationError(f"the database is at version {current}; the way back from {VERSION_0025} needs exactly {VERSION_0025}", current)
            downgrade_0025(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return _version_of(session)
    finally:
        session.close()


# ---- 0026 (SPEC-025, delivery 3): desks.layout -------------------------------------------------------------------------------------

VERSION_0026 = 26


def downgrade_0026(session: Session) -> None:
    """Steps inside the caller's open write transaction. The panels a desk had chosen are lost; the desks go back to the five panels of before."""
    session.execute("ALTER TABLE desks DROP COLUMN layout")
    session.execute("DELETE FROM schema_version WHERE version = :v", {"v": VERSION_0026})


def rollback_0026(path: Path | None = None, *, backend: Backend | None = None) -> int:
    """Take the database from version 26 back to 25. Returns the version it is at after. Raises MigrationError (it is not at version 26 or the
    step failed; the transaction was rolled back)."""
    path = Path(path) if path else db_path()
    backend = backend or SqliteBackend(path)
    session = backend.connect()
    try:
        session.begin(write=True)
        try:
            current = _version_of(session)
            if current != VERSION_0026:
                raise MigrationError(f"the database is at version {current}; the way back from {VERSION_0026} needs exactly {VERSION_0026}", current)
            downgrade_0026(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return _version_of(session)
    finally:
        session.close()


# ---------------------------------------------------------------------------------------------------- 0027 (SPEC-026, task 3390)

VERSION_0027 = 27


class AccountRulesExist(DbError):
    """Version 27 holds rules of an account. Nothing was changed. Remove them first so no rule is lost without the owner knowing."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(f"cannot go back: {count} rule(s) of an account exist; remove them first")


def downgrade_0027(session: Session) -> None:
    """Steps inside the caller's open write transaction. Raises AccountRulesExist before changing anything. The modes of the controls go
    (rule_modes is taken out of every desk, so every control blocks again, which is how version 26 behaved) and the four tables go."""
    import json

    count = int(session.scalar("SELECT count(*) FROM account_rules") or 0)
    if count:
        raise AccountRulesExist(count)
    for row in session.query("SELECT desk_id, config FROM desk_limits"):
        try:
            config = json.loads(row["config"] or "{}")
        except ValueError:
            continue
        if "rule_modes" in config:
            config.pop("rule_modes")
            session.execute("UPDATE desk_limits SET config = :c WHERE desk_id = :d", {"c": json.dumps(config, ensure_ascii=False), "d": row["desk_id"]})
    for table in ("rule_mode_migrations", "account_day_events", "account_day_state", "account_rules"):
        session.execute(f"DROP TABLE {table}")
    session.execute("DELETE FROM schema_version WHERE version = :v", {"v": VERSION_0027})


def rollback_0027(path: Path | None = None, *, backend: Backend | None = None) -> int:
    """Take the database from version 27 back to 26. Returns the version it is at after. Raises AccountRulesExist (nothing changed)
    or MigrationError (it is not at version 27 or the steps failed; the transaction was rolled back)."""
    path = Path(path) if path else db_path()
    backend = backend or SqliteBackend(path)
    session = backend.connect()
    try:
        session.begin(write=True)
        try:
            current = _version_of(session)
            if current != VERSION_0027:
                raise MigrationError(f"the database is at version {current}; the way back from {VERSION_0027} needs exactly {VERSION_0027}", current)
            downgrade_0027(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return _version_of(session)
    finally:
        session.close()


# ---------------------------------------------------------------------------------------------------- 0028 (SPEC-026, piece 3390c)

VERSION_0028 = 28


def downgrade_0028(session: Session) -> None:
    """Steps inside the caller's open write transaction: the two tables of the live freeze go. Nothing else of version 27 changes."""
    for table in ("desk_day_stops", "account_live_session"):
        session.execute(f"DROP TABLE {table}")
    session.execute("DELETE FROM schema_version WHERE version = :v", {"v": VERSION_0028})


def rollback_0028(path: Path | None = None, *, backend: Backend | None = None) -> int:
    """Take the database from version 28 back to 27. Returns the version it is at after. Raises MigrationError (it is not at version 28 or the
    steps failed; the transaction was rolled back)."""
    path = Path(path) if path else db_path()
    backend = backend or SqliteBackend(path)
    session = backend.connect()
    try:
        session.begin(write=True)
        try:
            current = _version_of(session)
            if current != VERSION_0028:
                raise MigrationError(f"the database is at version {current}; the way back from {VERSION_0028} needs exactly {VERSION_0028}", current)
            downgrade_0028(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return _version_of(session)
    finally:
        session.close()


# ---- 0029 (SPEC-026 piece 3390b): the notice orders and the grants of the rest of the day ---------------------------------------------

VERSION_0029 = 29


def downgrade_0029(session: Session) -> None:
    """Steps inside the caller's open write transaction. The grants and the deadlines of the pending orders are lost: a pending order goes back to
    waiting 30 minutes from its creation, and what a notice rule asked is still in its decision."""
    session.execute("DROP TABLE notice_grants")
    for column in ("expires_at", "notice_nonce", "notice_json"):
        session.execute(f"ALTER TABLE exec_proposals DROP COLUMN {column}")
    session.execute("DELETE FROM schema_version WHERE version = :v", {"v": VERSION_0029})


def rollback_0029(path: Path | None = None, *, backend: Backend | None = None) -> int:
    """Take the database from version 29 back to 28. Returns the version it is at after. Raises
    MigrationError (it is not at version 29 or the step failed; the transaction was rolled back)."""
    path = Path(path) if path else db_path()
    backend = backend or SqliteBackend(path)
    session = backend.connect()
    try:
        session.begin(write=True)
        try:
            current = _version_of(session)
            if current != VERSION_0029:
                raise MigrationError(f"the database is at version {current}; the way back from {VERSION_0029} needs exactly {VERSION_0029}", current)
            downgrade_0029(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return _version_of(session)
    finally:
        session.close()


# ---------------------------------------------------------------------------------------------------- 0030 (SPEC-026, piece 3390c2)

VERSION_0030 = 30


def downgrade_0030(session: Session) -> None:
    """Steps inside the caller's open write transaction: the declarations of the start equity go. Nothing else changes."""
    session.execute("DROP TABLE account_start_declarations")
    session.execute("DELETE FROM schema_version WHERE version = :v", {"v": VERSION_0030})


def rollback_0030(path: Path | None = None, *, backend: Backend | None = None) -> int:
    """Take the database from version 30 back to 29. Returns the version it is at after. Raises MigrationError (it is not at version 30 or the
    steps failed; the transaction was rolled back)."""
    path = Path(path) if path else db_path()
    backend = backend or SqliteBackend(path)
    session = backend.connect()
    try:
        session.begin(write=True)
        try:
            current = _version_of(session)
            if current != VERSION_0030:
                raise MigrationError(f"the database is at version {current}; the way back from {VERSION_0030} needs exactly {VERSION_0030}", current)
            downgrade_0030(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return _version_of(session)
    finally:
        session.close()


# ---------------------------------------------------------------------------------------------------- 0031 (SPEC-026 v2, piece 3390e3)

VERSION_0031 = 31


def downgrade_0031(session: Session) -> None:
    """Steps inside the caller's open write transaction: the two columns of the start of the session that Hokma measured with its own prices come back (empty).
    Nothing else changes."""
    session.execute("ALTER TABLE account_live_session ADD COLUMN pnl_start TEXT")
    session.execute("ALTER TABLE account_live_session ADD COLUMN desk_start TEXT NOT NULL DEFAULT '{}'")
    session.execute("DELETE FROM schema_version WHERE version = :v", {"v": VERSION_0031})


def rollback_0031(path: Path | None = None, *, backend: Backend | None = None) -> int:
    """Take the database from version 31 back to 30. Returns the version it is at after. Raises MigrationError (it is not at version 31 or the
    steps failed; the transaction was rolled back)."""
    path = Path(path) if path else db_path()
    backend = backend or SqliteBackend(path)
    session = backend.connect()
    try:
        session.begin(write=True)
        try:
            current = _version_of(session)
            if current != VERSION_0031:
                raise MigrationError(f"the database is at version {current}; the way back from {VERSION_0031} needs exactly {VERSION_0031}", current)
            downgrade_0031(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return _version_of(session)
    finally:
        session.close()


# ---------------------------------------------------------------------------------------------------- 0032 (SPEC-026 v2, piece 3390e2)

VERSION_0032 = 32


def downgrade_0032(session: Session) -> None:
    """Steps inside the caller's open write transaction: the column of the peak equity of the session goes. The peak is then read again from the readings of the broker."""
    session.execute("ALTER TABLE account_live_session DROP COLUMN peak_equity")
    session.execute("DELETE FROM schema_version WHERE version = :v", {"v": VERSION_0032})


def rollback_0032(path: Path | None = None, *, backend: Backend | None = None) -> int:
    """Take the database from version 32 back to 31. Returns the version it is at after. Raises MigrationError (it is not at version 32 or the
    steps failed; the transaction was rolled back)."""
    path = Path(path) if path else db_path()
    backend = backend or SqliteBackend(path)
    session = backend.connect()
    try:
        session.begin(write=True)
        try:
            current = _version_of(session)
            if current != VERSION_0032:
                raise MigrationError(f"the database is at version {current}; the way back from {VERSION_0032} needs exactly {VERSION_0032}", current)
            downgrade_0032(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return _version_of(session)
    finally:
        session.close()


# ---------------------------------------------------------------------------------------------------- 0033 (SPEC-026 v2, piece 3390e4)

VERSION_0033 = 33


def downgrade_0033(session: Session) -> None:
    """Steps inside the caller's open write transaction: the table of the notices of a cap with no order goes. What each one said stays in the messages that were sent;
    the day permissions it wrote are in notice_grants (0029) and stay."""
    session.execute("DROP TABLE cap_notices")
    session.execute("DELETE FROM schema_version WHERE version = :v", {"v": VERSION_0033})


def rollback_0033(path: Path | None = None, *, backend: Backend | None = None) -> int:
    """Take the database from version 33 back to 32. Returns the version it is at after. Raises MigrationError (it is not at version 33 or the
    steps failed; the transaction was rolled back)."""
    path = Path(path) if path else db_path()
    backend = backend or SqliteBackend(path)
    session = backend.connect()
    try:
        session.begin(write=True)
        try:
            current = _version_of(session)
            if current != VERSION_0033:
                raise MigrationError(f"the database is at version {current}; the way back from {VERSION_0033} needs exactly {VERSION_0033}", current)
            downgrade_0033(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return _version_of(session)
    finally:
        session.close()
