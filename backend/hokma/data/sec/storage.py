"""Local and cloud storage of filings (ED-019): a verified cache of submission texts and a transport that reads through it.

A stored filing carries its sha256: a copy that does not match is never returned, it is treated as missing. Only successful
answers are stored; a failure is never cached. The blob backend is a small protocol, so a cloud bucket plugs in without
changing the callers.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from hokma.data import _lib
from hokma.data.sec.transport import SecTransport, TransportError

_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")


class BlobBackend(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes | None: ...
    def exists(self, key: str) -> bool: ...
    def keys(self, prefix: str = "") -> list[str]: ...


class LocalBackend:
    """Blobs as files under a root folder. Keys cannot leave the folder."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"key escapes the storage root: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)                                  # a reader never sees half a file

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def keys(self, prefix: str = "") -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file() and p.relative_to(self.root).as_posix().startswith(prefix))


@dataclass(frozen=True)
class StoredFiling:
    cik: int
    accession: str
    sha256: str
    size: int
    stored_at: str
    source: str


class FilingStore:
    """Submission texts by CIK and accession, each with its checksum."""

    def __init__(self, backend: BlobBackend | Path | str) -> None:
        self.backend: BlobBackend = LocalBackend(backend) if isinstance(backend, (str, Path)) else backend

    @staticmethod
    def _keys(cik: int, accession: str) -> tuple[str, str]:
        if not _ACCESSION.match(accession):
            raise ValueError(f"not an accession number: {accession!r}")
        base = f"{int(cik)}/{accession}"
        return f"{base}.txt", f"{base}.meta.json"

    def save(self, cik: int, accession: str, text: str, *, source: str = "sec_edgar", now: datetime | None = None) -> StoredFiling:
        data_key, meta_key = self._keys(cik, accession)
        data = text.encode("utf-8")
        meta = StoredFiling(cik=int(cik), accession=accession, sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                            stored_at=(now or datetime.now(timezone.utc)).isoformat(), source=source)
        self.backend.put(data_key, data)
        self.backend.put(meta_key, json.dumps(meta.__dict__, sort_keys=True).encode("utf-8"))
        return meta

    def meta(self, cik: int, accession: str) -> StoredFiling | None:
        _, meta_key = self._keys(cik, accession)
        raw = self.backend.get(meta_key)
        if raw is None:
            return None
        try:
            return StoredFiling(**json.loads(raw))
        except (ValueError, TypeError):
            return None

    def load(self, cik: int, accession: str) -> str | None:
        """The stored text, or None when it is missing or does not match its checksum."""
        data_key, _ = self._keys(cik, accession)
        meta = self.meta(cik, accession)
        data = self.backend.get(data_key)
        if meta is None or data is None or hashlib.sha256(data).hexdigest() != meta.sha256 or len(data) != meta.size:
            return None
        return data.decode("utf-8")

    def verify(self) -> dict[str, list[str]]:
        """Checks every stored filing: ok, corrupt (checksum differs) and orphan (a text without its record, or the other way)."""
        report: dict[str, list[str]] = {"ok": [], "corrupt": [], "orphan": []}
        keys = self.backend.keys()
        texts = {k[:-4] for k in keys if k.endswith(".txt")}
        metas = {k[:-len(".meta.json")] for k in keys if k.endswith(".meta.json")}
        for base in sorted(texts | metas):
            cik, accession = base.split("/", 1)
            if base not in texts or base not in metas:
                report["orphan"].append(base)
            elif self.load(int(cik), accession) is None:
                report["corrupt"].append(base)
            else:
                report["ok"].append(base)
        return report

    def summary(self) -> dict[str, Any]:
        filings = [m for m in (self.meta(int(k.split("/")[0]), k.split("/", 1)[1][:-len(".meta.json")]) for k in self.backend.keys() if k.endswith(".meta.json")) if m]
        return {"filing_count": len(filings), "total_bytes": sum(m.size for m in filings), "ciks": sorted({m.cik for m in filings})}

    def delete(self, cik: int, accession: str) -> None:
        """Removes a stored filing. Only local backends can delete."""
        backend = self.backend
        if not isinstance(backend, LocalBackend):
            raise NotImplementedError("this backend does not delete")
        for key in self._keys(cik, accession):
            path = backend._path(key)
            if path.exists():
                path.unlink()


class CachedTransport:
    """Reads submission texts from the store first and fills it from the inner transport. Everything else passes through."""

    def __init__(self, inner: SecTransport, store: FilingStore) -> None:
        self._inner = inner
        self.store = store
        self.hits = 0
        self.misses = 0

    def submission_text(self, cik: int, accession: str) -> str:
        cached = self.store.load(cik, accession)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        text = self._inner.submission_text(cik, accession)        # a failure raises here and nothing is stored
        if not text:
            raise TransportError("not_found", "empty submission text")
        self.store.save(cik, accession, text)
        return text

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def library_storage_info(root: Path | str) -> dict[str, Any]:
    """What the filing library's own local storage holds at `root` (the library is pointed at that folder)."""
    return _lib.sec_library_storage_info(str(root))
