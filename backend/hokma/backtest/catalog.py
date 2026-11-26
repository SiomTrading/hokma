"""Local cache of the bars that `hokma.data` already loaded (SPEC-020: N-31).

The catalog is a function interface over a store of Parquet files named by the hash of the request, so no caller picks a path. A second run
over the same request reads the bars from the catalog and does not call the data module; the data hash of both runs is the same.
Parquet is read and written through pandas; the library below it is not imported here (it stays outside this module, like every
calculation library).
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Protocol

import pandas as pd

_KEY = re.compile(r"^[0-9a-f]{64}$")


class BarStore(Protocol):
    def read(self, key: str) -> pd.DataFrame | None: ...

    def write(self, key: str, frame: pd.DataFrame) -> None: ...


class ParquetStore:
    """One Parquet file per request in a directory the service owns."""

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)

    def _path(self, key: str) -> Path:
        if not _KEY.match(key):
            raise ValueError("a catalog key is the hash of a request")
        return self._dir / f"{key}.parquet"

    def read(self, key: str) -> pd.DataFrame | None:
        path = self._path(key)
        return pd.read_parquet(path) if path.is_file() else None

    def write(self, key: str, frame: pd.DataFrame) -> None:
        path = self._path(key)
        self._dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        frame.to_parquet(temporary)
        os.replace(temporary, path)             # a reader never sees a half written file


class Catalog:
    def __init__(self, store: BarStore) -> None:
        self._store = store

    @staticmethod
    def key(symbol: str, interval: str, start: datetime, end: datetime, as_of: datetime) -> str:
        """The identity of a request: the same symbol, interval, window and cut always give the same key."""
        text = "|".join((symbol, interval, start.isoformat(), end.isoformat(), as_of.isoformat()))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, key: str) -> pd.DataFrame | None:
        return self._store.read(key)

    def put(self, key: str, frame: pd.DataFrame) -> None:
        self._store.write(key, frame)
