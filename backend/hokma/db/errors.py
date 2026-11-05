"""Errors of the database layer. The API layer maps them to catalog messages; the text here is English."""


class DbError(Exception):
    """Base class of every error raised by hokma.db."""


class NotFound(DbError):
    """The row does not exist."""


class ValidationError(DbError):
    """The input is wrong and was caught before reaching SQL."""


class IntegrityViolation(DbError):
    """A CHECK, UNIQUE or foreign key constraint refused the write."""


class RuleViolation(IntegrityViolation):
    """A trigger refused the write: forbidden transition, unfinished parents, append-only table..."""


class Conflict(DbError):
    """A conditional update matched no row: somebody else got there first."""


class ConnectionFailed(DbError):
    """The database server could not be reached or refused the login. `code` is a catalog key suffix."""

    def __init__(self, message: str, code: str = "unreachable") -> None:
        super().__init__(message)
        self.code = code


class MigrationError(DbError):
    """A migration failed (or the database is newer than the code). The failing file was rolled back."""

    def __init__(self, message: str, version: int | None = None) -> None:
        super().__init__(message)
        self.version = version
