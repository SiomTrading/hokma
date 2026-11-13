"""The `.hkb` backup file: a standard zip of the database snapshot and the memory folder, encrypted with AES-256-GCM.

Layout of a `.hkb` file:
    header   MAGIC (4) | scrypt salt (16) | nonce prefix (8)
    chunks   repeated: ciphertext length (4, big endian) | AES-GCM ciphertext + tag, over 1 MiB of the zip
Each chunk is authenticated together with the header and a "last chunk" flag, so a truncated, reordered or
tampered file never decrypts. The key is a 24 character recovery key that Hokma generates and shows once; the
AES key is derived from it with `hashlib.scrypt`. Without that recovery key the file cannot be opened.
"""
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import struct
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hokma.backup.snapshot import safe_copy_db, verify_sqlite_integrity
from hokma.i18n import t

logger = logging.getLogger(__name__)

MAGIC = b"HKB1"
FORMAT_VERSION = 1
CHUNK_SIZE = 1 << 20                      # 1 MiB of plain data per encrypted block
SALT_SIZE = 16
NONCE_PREFIX_SIZE = 8
HEADER_SIZE = len(MAGIC) + SALT_SIZE + NONCE_PREFIX_SIZE
TAG_SIZE = 16
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 << 20
DB_ENTRY = "hokma.db"
MANIFEST_ENTRY = "manifest.json"
MEMORY_PREFIX = "memory/"
FILES_PREFIX = "files/"                   # libraries, disk folders and big project files
KEY_LENGTH = 24
KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # 32 symbols, no I, O, 0 or 1: easy to copy by hand


class BackupError(Exception):
    """A backup could not be made or opened. The text is already in the student's language."""


# --- Recovery key ---

def generate_recovery_key() -> str:
    """A new random recovery key: 24 symbols of a 32 symbol alphabet (120 bits)."""
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_LENGTH))


def group_recovery_key(key: str) -> str:
    """The key in groups of four for display (`ABCD-EFGH-...`)."""
    plain = normalize_recovery_key(key)
    return "-".join(plain[i:i + 4] for i in range(0, KEY_LENGTH, 4))


def normalize_recovery_key(key: str) -> str:
    """Strip spaces and dashes, upper-case, and check the length and the alphabet."""
    plain = "".join(ch for ch in (key or "") if ch not in " -\t\r\n").upper()
    if len(plain) != KEY_LENGTH or any(ch not in KEY_ALPHABET for ch in plain):
        raise BackupError(t("backup.key.invalid_format", length=KEY_LENGTH))
    return plain


def _derive(key: str, salt: bytes) -> bytes:
    return hashlib.scrypt(normalize_recovery_key(key).encode("ascii"), salt=salt,
                          n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32)


# --- Encryption of a file ---

def _nonce(prefix: bytes, index: int) -> bytes:
    return prefix + struct.pack(">I", index)


def _encrypt_file(src: Path, dst: Path, key: str) -> None:
    salt, prefix = os.urandom(SALT_SIZE), os.urandom(NONCE_PREFIX_SIZE)
    header = MAGIC + salt + prefix
    aes = AESGCM(_derive(key, salt))
    size = src.stat().st_size
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(header)
        index, done = 0, 0
        while True:
            block = fin.read(CHUNK_SIZE)
            done += len(block)
            last = done >= size
            sealed = aes.encrypt(_nonce(prefix, index), block, header + (b"\x01" if last else b"\x00"))
            fout.write(struct.pack(">I", len(sealed)) + sealed)
            index += 1
            if last:
                break


def _decrypt_file(src: Path, dst: Path, key: str) -> None:
    with open(src, "rb") as fin:
        header = fin.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE or not header.startswith(MAGIC):
            raise BackupError(t("backup.bundle.not_a_bundle"))
        salt, prefix = header[len(MAGIC):len(MAGIC) + SALT_SIZE], header[len(MAGIC) + SALT_SIZE:]
        aes = AESGCM(_derive(key, salt))
        with open(dst, "wb") as fout:
            index = 0
            while True:
                raw = fin.read(4)
                if len(raw) != 4:
                    raise BackupError(t("backup.bundle.corrupt"))
                (length,) = struct.unpack(">I", raw)
                sealed = fin.read(length)
                if length < TAG_SIZE or length > CHUNK_SIZE + TAG_SIZE or len(sealed) != length:
                    raise BackupError(t("backup.bundle.corrupt"))
                last = not fin.peek(1)
                try:
                    fout.write(aes.decrypt(_nonce(prefix, index), sealed, header + (b"\x01" if last else b"\x00")))
                except InvalidTag:
                    # The first block can only fail on a wrong key; a later one means the file is damaged.
                    raise BackupError(t("backup.key.wrong") if index == 0 else t("backup.bundle.corrupt")) from None
                index += 1
                if last:
                    break


# --- Zip ---

def _zip_sqlite_snapshot(zf: zipfile.ZipFile, abs_path: Path, rel_path: Path, out_path: Path) -> Optional[int]:
    """Add a WAL-safe snapshot of *abs_path* to *zf*; return its byte size, or None on failure.

    Staged beside the output file: /tmp may be a small tmpfs that cannot hold large databases.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=str(out_path.parent)) as tmp:
        tmp_db = Path(tmp.name)
    try:
        if not safe_copy_db(abs_path, tmp_db):
            return None
        zf.write(tmp_db, arcname=str(rel_path))
        return tmp_db.stat().st_size
    finally:
        tmp_db.unlink(missing_ok=True)


def _write_zip_entries(
    zf: zipfile.ZipFile, files_to_add: List[Tuple[Path, Path]], out_path: Path,
    *, on_db_failure, on_error, on_progress, track_bytes: bool) -> int:
    """Add every ``(abs_path, rel_path)`` to *zf*, WAL-safe for ``*.db``; return bytes archived.

    ``on_db_failure(rel_path)`` runs when a SQLite snapshot fails (may raise to abort);
    ``on_error(rel_path, exc)`` records a read failure; ``on_progress(i)`` fires every 500 files;
    ``track_bytes`` stats plain files for the size total.
    """
    total_bytes = 0
    for i, (abs_path, rel_path) in enumerate(files_to_add, 1):
        try:
            if abs_path.suffix == ".db":
                size = _zip_sqlite_snapshot(zf, abs_path, rel_path, out_path)
                if size is None:
                    on_db_failure(rel_path)
                    continue
                total_bytes += size
            else:
                zf.write(abs_path, arcname=str(rel_path))
                if track_bytes:
                    total_bytes += abs_path.stat().st_size
        except (PermissionError, OSError, ValueError) as exc:
            on_error(rel_path, exc)
            continue
        if i % 500 == 0:
            on_progress(i)
    return total_bytes


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema_version(db: Path) -> int:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return int(con.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0])
    except sqlite3.DatabaseError:
        return 0
    finally:
        con.close()


def _memory_files(memory_dir: Optional[Path]) -> List[Tuple[Path, Path]]:
    if memory_dir is None or not memory_dir.is_dir():
        return []
    found = [p for p in sorted(memory_dir.rglob("*")) if p.is_file() and not p.is_symlink()]
    return [(p, Path(MEMORY_PREFIX + p.relative_to(memory_dir).as_posix())) for p in found]


# --- Public API ---

@dataclass
class BundleInfo:
    path: Path
    size: int
    created_at: str
    db_sha256: str
    schema_version: int
    files: int


@dataclass
class BundleContents:
    db_path: Path
    memory_dir: Optional[Path]
    manifest: dict
    files_dir: Optional[Path] = None


def create_bundle(out_path: Path, *, db_path: Path, key: str, memory_dir: Optional[Path] = None,
                  extra: Sequence[Tuple[Path, str]] = ()) -> BundleInfo:
    """Write the encrypted `.hkb` file for *db_path*, *memory_dir* and the *extra* `(file, name)` pairs (stored under
    `files/<name>`) at *out_path* and verify it by reading it back.

    The file appears at *out_path* only when it is complete and verified; a failure leaves nothing behind.
    """
    normalize_recovery_key(key)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(out_path.parent), prefix=".hkb-stage-") as stage_name:
        stage = Path(stage_name)
        snapshot = stage / DB_ENTRY
        if not safe_copy_db(Path(db_path), snapshot):
            raise BackupError(t("backup.db.snapshot_failed"))
        check = verify_sqlite_integrity(snapshot)
        if not check["valid"]:
            raise BackupError(t("backup.db.integrity_failed", detail=check["message"]))
        db_sha, schema = _sha256(snapshot), _schema_version(snapshot)
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        skipped: List[str] = []

        def db_failed(rel: Path) -> None:
            raise BackupError(t("backup.db.snapshot_failed"))

        zip_path = stage / "bundle.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot, arcname=DB_ENTRY)
            _write_zip_entries(zf, _memory_files(memory_dir), zip_path, on_db_failure=db_failed,
                               on_error=lambda rel, exc: skipped.append(str(rel)), on_progress=lambda i: None,
                               track_bytes=False)
            for src, name in extra:                          # plain copies: a student's own `.db` is not ours to snapshot
                try:
                    zf.write(src, arcname=FILES_PREFIX + name)
                except OSError:
                    skipped.append(name)
            manifest = {"format": FORMAT_VERSION, "created_at": created, "db_sha256": db_sha,
                        "schema_version": schema, "skipped": skipped}
            zf.writestr(MANIFEST_ENTRY, json.dumps(manifest, indent=2))
            files = len(zf.namelist())
        staged = stage / "bundle.hkb"
        _encrypt_file(zip_path, staged, key)
        zip_path.unlink()
        try:
            with tempfile.TemporaryDirectory(dir=str(stage), prefix="verify-") as verify_dir:
                opened = open_bundle(staged, key, Path(verify_dir))
                if _sha256(opened.db_path) != db_sha:
                    raise BackupError(t("backup.bundle.verify_failed"))
        except BackupError:
            raise
        except Exception as exc:
            logger.warning("Backup verification failed: %s", exc)
            raise BackupError(t("backup.bundle.verify_failed")) from exc
        os.replace(staged, out_path)
    return BundleInfo(path=out_path, size=out_path.stat().st_size, created_at=created, db_sha256=db_sha,
                      schema_version=schema, files=files)


def open_bundle(bundle_path: Path, key: str, dest_dir: Path) -> BundleContents:
    """Decrypt *bundle_path* into *dest_dir* and verify the database inside it.

    Raises BackupError (with a plain message) for a wrong key, a damaged file or a database that fails its check.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / ".bundle.zip"
    try:
        _decrypt_file(Path(bundle_path), zip_path, key)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                manifest = json.loads(zf.read(MANIFEST_ENTRY))
                if int(manifest.get("format", 0)) > FORMAT_VERSION:
                    raise BackupError(t("backup.bundle.newer_format"))
                _extract(zf, dest_dir)
        except (zipfile.BadZipFile, KeyError, ValueError):
            raise BackupError(t("backup.bundle.corrupt")) from None
    finally:
        zip_path.unlink(missing_ok=True)
    db = dest_dir / DB_ENTRY
    check = verify_sqlite_integrity(db)
    if not check["valid"]:
        raise BackupError(t("backup.db.integrity_failed", detail=check["message"]))
    if manifest.get("db_sha256") and _sha256(db) != manifest["db_sha256"]:
        raise BackupError(t("backup.bundle.corrupt"))
    memory = dest_dir / MEMORY_PREFIX.rstrip("/")
    files = dest_dir / FILES_PREFIX.rstrip("/")
    return BundleContents(db_path=db, memory_dir=memory if memory.is_dir() else None, manifest=manifest,
                          files_dir=files if files.is_dir() else None)


def _extract(zf: zipfile.ZipFile, dest_dir: Path) -> None:
    root = dest_dir.resolve()
    for info in zf.infolist():
        name = info.filename
        if name != DB_ENTRY and name != MANIFEST_ENTRY and not name.startswith((MEMORY_PREFIX, FILES_PREFIX)):
            raise BackupError(t("backup.bundle.unsafe_entry"))
        target = (dest_dir / name).resolve()
        if root != target and root not in target.parents:
            raise BackupError(t("backup.bundle.unsafe_entry"))
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            for block in iter(lambda: src.read(1 << 20), b""):
                out.write(block)
