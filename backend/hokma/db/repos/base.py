"""Shared pieces of the repositories."""
from __future__ import annotations

from typing import Any

from hokma.db.backend import Session
from hokma.db.errors import NotFound, ValidationError


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Any = _Unset()          # "argument not given", as opposed to None (= set the column to NULL)


def flag(value: Any) -> int:
    return 1 if value else 0


def given(**kwargs: Any) -> dict[str, Any]:
    """The keyword arguments that were really passed (not UNSET)."""
    return {k: v for k, v in kwargs.items() if v is not UNSET}


def require_in(name: str, value: Any, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ValidationError(f"{name} must be one of {', '.join(allowed)}; got {value!r}")


class Repo:
    """A repository wraps one Session. It never commits: the Unit does."""

    def __init__(self, session: Session) -> None:
        self.s = session

    def _get(self, table: str, key: str, value: Any, model):
        row = self.s.one(f"SELECT * FROM {table} WHERE {key} = :v", {"v": value})
        if row is None:
            raise NotFound(f"{table} {value!r} not found")
        return model.from_row(row)

    def _find(self, table: str, key: str, value: Any, model):
        row = self.s.one(f"SELECT * FROM {table} WHERE {key} = :v", {"v": value})
        return model.from_row(row) if row else None
