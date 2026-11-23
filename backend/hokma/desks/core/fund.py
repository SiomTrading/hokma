"""Registry of live desk instances (SPEC-017: AH-28).

A desk instance holds what a desk keeps between turns: its caches and its own state. Each desk has exactly one live
instance, created on first use. An archived desk has none: asking for it raises, and `live()` never lists it. Two desks never
share an instance, a cache or a state dict (decision 1088): everything is created per instance, never at module level.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

ARCHIVED = "archived"


class DeskNotLive(Exception):
    """The desk does not exist or is archived, so it has no live instance."""

    def __init__(self, desk_id: int, reason: str) -> None:
        super().__init__(f"desk {desk_id} is not live: {reason}")
        self.desk_id, self.reason = desk_id, reason


@dataclass
class DeskInstance:
    desk_id: int
    state: dict[str, Any] = field(default_factory=dict)
    caches: dict[str, dict[Any, Any]] = field(default_factory=dict)
    closed: bool = False

    def cache(self, name: str) -> dict[Any, Any]:
        """The named cache of this desk, created empty on first use. It survives from one turn to the next."""
        return self.caches.setdefault(name, {})

    def close(self) -> None:
        self.state.clear()
        self.caches.clear()
        self.closed = True


def db_status(desk_id: int) -> str | None:
    """Status of a desk in the database (`active`, `paused`, `archived`), or None when it does not exist."""
    from hokma.db import get_database

    with get_database().read() as u:
        row = u.session.one("SELECT status FROM desks WHERE id = :i", {"i": desk_id})
    return None if row is None else str(row["status"])


class Fund:
    """The live desk instances of one Hokma process."""

    def __init__(self, status_of: Callable[[int], str | None] = db_status,
                 factory: Callable[[int], DeskInstance] = DeskInstance) -> None:
        self._status_of = status_of
        self._factory = factory
        self._instances: dict[int, DeskInstance] = {}
        self._lock = threading.RLock()

    def _evict(self, desk_id: int) -> None:
        instance = self._instances.pop(desk_id, None)
        if instance is not None:
            instance.close()

    def instance(self, desk_id: int) -> DeskInstance:
        """The live instance of a desk. Raises DeskNotLive for a desk that does not exist or is archived (its instance is
        dropped with its caches)."""
        with self._lock:
            status = self._status_of(desk_id)
            if status is None:
                self._evict(desk_id)
                raise DeskNotLive(desk_id, "it does not exist")
            if status == ARCHIVED:
                self._evict(desk_id)
                raise DeskNotLive(desk_id, "it is archived")
            if desk_id not in self._instances:
                self._instances[desk_id] = self._factory(desk_id)
            return self._instances[desk_id]

    def live(self) -> list[DeskInstance]:
        """The instances whose desk is still active or paused, by desk id. Archived desks are dropped on the way."""
        with self._lock:
            for desk_id in sorted(self._instances):
                if self._status_of(desk_id) in (None, ARCHIVED):
                    self._evict(desk_id)
            return [self._instances[k] for k in sorted(self._instances)]

    def __iter__(self) -> Iterator[DeskInstance]:
        return iter(self.live())

    def release(self, desk_id: int) -> bool:
        """Drop the instance of a desk (its caches go with it). True when there was one."""
        with self._lock:
            existed = desk_id in self._instances
            self._evict(desk_id)
            return existed
