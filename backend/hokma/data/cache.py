"""SQLite result cache with a time to live and a record of what was observed (SPEC-017: FT-099).

One table of entries and one of coverage, shared by the eight data modules (`CATEGORIES`). Only successful results are
stored. A hit declares its source and the time it was received, and never returns an entry whose `as_of` is later than
the cut asked for. Coverage records which windows a source has observed, so a window outside it is a gap that is reported
and never filled.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from hokma.data.sentinels import DataResult, OK, parse_time, utcnow

CATEGORIES = ("bars", "fundamentals", "macro", "news", "social", "filings", "universe", "cot")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    category TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, source TEXT NOT NULL,
    as_of TEXT, received_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    PRIMARY KEY (category, key)
);
CREATE TABLE IF NOT EXISTS coverage (
    category TEXT NOT NULL, symbol TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
    PRIMARY KEY (category, symbol, start, end)
);
"""


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"cannot cache a value of type {type(value).__name__}")


def make_key(category: str, symbol: str = "", /, *, as_of: datetime | None = None, **params: Any) -> str:
    """Stable key of a request: the same category, symbol, cut and parameters give the same key."""
    body = json.dumps({"c": category, "s": symbol, "a": as_of.isoformat() if as_of else None, "p": params},
                      sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


class ResultCache:
    def __init__(self, path: str | Path = ":memory:", *, clock: Callable[[], datetime] = utcnow) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _check(category: str) -> None:
        if category not in CATEGORIES:
            raise ValueError(f"unknown cache category {category!r}; expected one of {CATEGORIES}")

    # ---------------------------------------------------------------------------------------------- entries
    def put(self, category: str, key: str, value: Any, *, source: str, ttl_seconds: float,
            as_of: datetime | None = None) -> None:
        """Store a successful value. Raises ValueError for a bad category or a value that is None."""
        self._check(category)
        if value is None:
            raise ValueError("a missing value is never cached")
        now = self._clock()
        payload = json.dumps(value, default=_json_default)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?,?)",
                (category, key, payload, source, as_of.isoformat() if as_of else None, now.isoformat(),
                 (now + timedelta(seconds=ttl_seconds)).isoformat()),
            )
            self._db.commit()

    def put_result(self, category: str, key: str, result: DataResult, *, ttl_seconds: float) -> bool:
        """Store a result only when it is ok. Returns whether it was stored: failures are never cached."""
        if result.status != OK:
            return False
        self.put(category, key, result.value, source=result.source, ttl_seconds=ttl_seconds, as_of=result.as_of)
        return True

    def get(self, category: str, key: str, *, as_of: datetime | None = None) -> DataResult | None:
        """The cached result, or None when missing, expired or later than `as_of`."""
        self._check(category)
        with self._lock:
            row = self._db.execute(
                "SELECT payload, source, as_of, received_at, expires_at FROM entries WHERE category=? AND key=?",
                (category, key),
            ).fetchone()
            if row is None:
                return None
            payload, source, stored_as_of, received_at, expires_at = row
            if parse_time(expires_at) <= self._clock():
                self._db.execute("DELETE FROM entries WHERE category=? AND key=?", (category, key))
                self._db.commit()
                return None
        stored = parse_time(stored_as_of) if stored_as_of else None
        if as_of is not None and stored is not None and stored > parse_time(as_of):
            return None
        return DataResult(json.loads(payload), source, parse_time(received_at), stored, OK, "", (), {"cached": True})

    def purge_expired(self) -> int:
        with self._lock:
            cursor = self._db.execute("DELETE FROM entries WHERE expires_at <= ?", (self._clock().isoformat(),))
            self._db.commit()
            return cursor.rowcount

    # ---------------------------------------------------------------------------------------------- coverage
    def record_coverage(self, category: str, symbol: str, start: datetime, end: datetime) -> None:
        self._check(category)
        if parse_time(end) < parse_time(start):
            raise ValueError("coverage ends before it starts")
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO coverage VALUES (?,?,?,?)",
                             (category, symbol, parse_time(start).isoformat(), parse_time(end).isoformat()))
            self._db.commit()

    def _merged(self, category: str, symbol: str) -> list[tuple[datetime, datetime]]:
        with self._lock:
            rows = self._db.execute("SELECT start, end FROM coverage WHERE category=? AND symbol=? ORDER BY start",
                                    (category, symbol)).fetchall()
        merged: list[tuple[datetime, datetime]] = []
        for start, end in ((parse_time(a), parse_time(b)) for a, b in rows):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def coverage(self, category: str, symbol: str) -> list[tuple[datetime, datetime]]:
        """The observed windows of a symbol, merged and sorted."""
        self._check(category)
        return self._merged(category, symbol)

    def gaps(self, category: str, symbol: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """The parts of [start, end] that no source has observed. They are reported, never filled."""
        self._check(category)
        cursor, stop = parse_time(start), parse_time(end)
        missing: list[tuple[datetime, datetime]] = []
        for window_start, window_end in self._merged(category, symbol):
            if window_end < cursor:
                continue
            if window_start > stop:
                break
            if window_start > cursor:
                missing.append((cursor, window_start))
            cursor = max(cursor, window_end)
            if cursor >= stop:
                break
        if cursor < stop:
            missing.append((cursor, stop))
        return missing

    def covers(self, category: str, symbol: str, start: datetime, end: datetime) -> bool:
        return not self.gaps(category, symbol, start, end)
