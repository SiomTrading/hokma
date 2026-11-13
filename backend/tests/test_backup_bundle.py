"""SPEC-011 P10: consistent database snapshot, encrypted `.hkb` bundle, restore, and the cloud_backup_state table."""
import sqlite3
import threading
import time

import pytest
from dbsupport import env  # noqa: F401  (fixture)

from hokma.backup.bundle import (BackupError, create_bundle, generate_recovery_key, group_recovery_key,
                                 normalize_recovery_key, open_bundle)
from hokma.backup.snapshot import safe_copy_db, safe_restore_db, verify_sqlite_integrity
from hokma.db import migrate
from hokma.i18n import t
from hokma.db.paths import db_path


def make_db(path, rows=50, wal=True):
    con = sqlite3.connect(str(path))
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE schema_version (version INTEGER)")
    con.execute("INSERT INTO schema_version VALUES (11)")
    con.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    con.executemany("INSERT INTO notes (body) VALUES (?)", [(f"note {i} " * 20,) for i in range(rows)])
    con.commit()
    return con


def dump(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return list(con.iterdump())
    finally:
        con.close()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "live" / "hokma.db"
    path.parent.mkdir()
    con = make_db(path)                               # stays open in WAL mode: data lives in the -wal file
    yield path
    con.close()


def test_snapshot_of_a_wal_database_holds_the_uncheckpointed_rows(source, tmp_path):
    copy = tmp_path / "copy.db"
    assert safe_copy_db(source, copy) is True
    assert verify_sqlite_integrity(copy)["valid"] is True
    assert dump(copy) == dump(source)


def test_a_locked_database_is_retried_until_it_is_free(tmp_path):
    path = tmp_path / "locked.db"
    make_db(path, wal=False).close()
    holder = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")
    threading.Timer(0.6, lambda: holder.execute("COMMIT")).start()
    started = time.monotonic()
    assert safe_copy_db(path, tmp_path / "copy.db", timeout_seconds=5.0) is True
    assert time.monotonic() - started >= 0.4
    holder.close()


def test_a_database_that_stays_locked_fails_closed_and_leaves_no_partial_copy(tmp_path):
    path = tmp_path / "locked.db"
    make_db(path, wal=False).close()
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    copy = tmp_path / "copy.db"
    assert safe_copy_db(path, copy, timeout_seconds=0.3) is False
    assert not copy.exists()
    holder.execute("ROLLBACK")
    holder.close()


def test_integrity_check_rejects_a_missing_tiny_and_foreign_file(tmp_path):
    assert verify_sqlite_integrity(tmp_path / "none.db")["valid"] is False
    tiny = tmp_path / "tiny.db"
    tiny.write_bytes(b"x" * 10)
    assert verify_sqlite_integrity(tiny)["valid"] is False
    foreign = tmp_path / "foreign.db"
    foreign.write_bytes(b"not a sqlite database " * 20)
    assert "header" in verify_sqlite_integrity(foreign)["message"]


def test_safe_restore_writes_the_snapshot_into_the_live_file(source, tmp_path):
    snapshot = tmp_path / "snap.db"
    assert safe_copy_db(source, snapshot)
    con = sqlite3.connect(str(source))
    con.execute("DELETE FROM notes")
    con.commit()
    con.close()
    assert safe_restore_db(snapshot, source) is True
    assert dump(source) == dump(snapshot)


def test_recovery_key_shape_and_normalisation():
    key = generate_recovery_key()
    assert len(key) == 24 and key == key.upper() and not set(key) & set("IO01")
    assert generate_recovery_key() != key
    assert normalize_recovery_key(group_recovery_key(key).lower() + " ") == key
    with pytest.raises(BackupError):
        normalize_recovery_key("short")
    with pytest.raises(BackupError):
        normalize_recovery_key("0" * 24)


def test_round_trip_decrypts_to_the_same_database_and_memory(source, tmp_path):
    memory = tmp_path / "memory"
    (memory / "sub").mkdir(parents=True)
    (memory / "a.md").write_text("alpha", encoding="utf-8")
    (memory / "sub" / "b.md").write_text("beta", encoding="utf-8")
    key = generate_recovery_key()
    out = tmp_path / "out" / "backup.hkb"
    info = create_bundle(out, db_path=source, key=key, memory_dir=memory)
    assert out.exists() and info.size == out.stat().st_size and info.schema_version == 11
    raw = out.read_bytes()
    assert raw[:4] == b"HKB1" and b"note 1" not in raw and b"SQLite format" not in raw
    opened = open_bundle(out, group_recovery_key(key), tmp_path / "restored")
    assert dump(opened.db_path) == dump(source)
    assert (opened.memory_dir / "a.md").read_text(encoding="utf-8") == "alpha"
    assert (opened.memory_dir / "sub" / "b.md").read_text(encoding="utf-8") == "beta"
    assert opened.manifest["db_sha256"] == info.db_sha256
    assert not list((tmp_path / "out").glob(".hkb-stage-*"))


def test_a_bundle_larger_than_one_block_round_trips(source, tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "big.bin").write_bytes(__import__("os").urandom(3 * (1 << 20) + 123))
    key = generate_recovery_key()
    out = tmp_path / "big.hkb"
    create_bundle(out, db_path=source, key=key, memory_dir=memory)
    opened = open_bundle(out, key, tmp_path / "restored")
    assert (opened.memory_dir / "big.bin").read_bytes() == (memory / "big.bin").read_bytes()


def test_a_wrong_key_fails_with_a_clear_message_and_writes_no_database(source, tmp_path):
    out = tmp_path / "backup.hkb"
    create_bundle(out, db_path=source, key=generate_recovery_key())
    with pytest.raises(BackupError) as caught:
        open_bundle(out, generate_recovery_key(), tmp_path / "restored")
    assert str(caught.value) == t("backup.key.wrong")
    assert not (tmp_path / "restored" / "hokma.db").exists()


def test_a_truncated_a_tampered_and_a_foreign_file_are_refused(source, tmp_path):
    key = generate_recovery_key()
    out = tmp_path / "backup.hkb"
    create_bundle(out, db_path=source, key=key)
    raw = out.read_bytes()
    cut = tmp_path / "cut.hkb"
    cut.write_bytes(raw[:-40])
    flipped = tmp_path / "flipped.hkb"
    flipped.write_bytes(raw[:-5] + bytes([raw[-5] ^ 1]) + raw[-4:])
    foreign = tmp_path / "foreign.hkb"
    foreign.write_bytes(b"this is not a backup at all")
    for bad in (cut, flipped, foreign):
        with pytest.raises(BackupError):
            open_bundle(bad, key, tmp_path / "restored")


def test_an_entry_that_escapes_the_folder_is_refused(source, tmp_path, monkeypatch):
    import hokma.backup.bundle as bundle

    real = bundle._write_zip_entries

    def evil(zf, files, out_path, **kw):
        zf.writestr("../escape.txt", "x")
        return real(zf, files, out_path, **kw)

    monkeypatch.setattr(bundle, "_write_zip_entries", evil)
    key = generate_recovery_key()
    with pytest.raises(BackupError):
        create_bundle(tmp_path / "evil.hkb", db_path=source, key=key)
    assert not (tmp_path / "evil.hkb").exists() and not (tmp_path / "escape.txt").exists()


def test_a_busy_database_makes_create_fail_cleanly(tmp_path):
    path = tmp_path / "locked.db"
    make_db(path, wal=False).close()
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    import hokma.backup.bundle as bundle

    original = bundle.safe_copy_db
    bundle.safe_copy_db = lambda s, d, **kw: original(s, d, timeout_seconds=0.2)
    try:
        with pytest.raises(BackupError):
            create_bundle(tmp_path / "x.hkb", db_path=path, key=generate_recovery_key())
    finally:
        bundle.safe_copy_db = original
        holder.execute("ROLLBACK")
        holder.close()
    assert not (tmp_path / "x.hkb").exists()


def test_migration_creates_cloud_backup_state(env):  # noqa: F811
    assert migrate().version >= 11
    con = sqlite3.connect(str(db_path()))
    cols = {r[1] for r in con.execute("PRAGMA table_info(cloud_backup_state)")}
    assert {"destination", "status", "status_detail", "last_backup_at", "last_size_bytes", "last_remote_name"} <= cols
    con.execute("INSERT INTO cloud_backup_state (destination) VALUES ('drive')")
    assert con.execute("SELECT status FROM cloud_backup_state").fetchone()[0] == "idle"
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO cloud_backup_state (destination) VALUES ('drive')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO cloud_backup_state (destination) VALUES ('dropbox')")
    con.close()
