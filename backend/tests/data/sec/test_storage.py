"""ED-019: verified local storage of filings and the caching transport."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hokma.data.sec import filings
from hokma.data.sec.client import SecClient
from hokma.data.sec.storage import CachedTransport, FilingStore, LocalBackend, library_storage_info
from hokma.data.sec.transport import TransportError
from tests.data.sec.support import FIXTURES, IDENTITY, DownTransport, RecordedTransport

ACC = "0001000001-24-000021"


def _text() -> str:
    return (FIXTURES / "sgml" / f"{ACC}.txt").read_text(encoding="utf-8")


def test_save_and_load_with_checksum_ED_019(tmp_path: Path):
    store = FilingStore(tmp_path)
    meta = store.save(1000001, ACC, _text())
    assert meta.sha256 == hashlib.sha256(_text().encode()).hexdigest() and meta.size == len(_text().encode()) and meta.source == "sec_edgar"
    assert store.load(1000001, ACC) == _text()
    assert store.meta(1000001, ACC) == meta
    assert (tmp_path / "1000001" / f"{ACC}.txt").is_file()


def test_missing_filing_is_none_ED_019(tmp_path: Path):
    assert FilingStore(tmp_path).load(1000001, ACC) is None and FilingStore(tmp_path).meta(1000001, ACC) is None


def test_corrupt_copy_is_never_returned_ED_019(tmp_path: Path):
    store = FilingStore(tmp_path)
    store.save(1000001, ACC, _text())
    (tmp_path / "1000001" / f"{ACC}.txt").write_text(_text().replace("108.2", "999.9"), encoding="utf-8")
    assert store.load(1000001, ACC) is None
    report = store.verify()
    assert report["corrupt"] == [f"1000001/{ACC}"] and report["ok"] == []


def test_verify_reports_ok_and_orphans_ED_019(tmp_path: Path):
    store = FilingStore(tmp_path)
    store.save(1000001, ACC, _text())
    store.save(1000001, "0001000001-24-000030", "second")
    (tmp_path / "1000001" / "0001000001-24-000030.meta.json").unlink()
    report = store.verify()
    assert report["ok"] == [f"1000001/{ACC}"] and report["orphan"] == ["1000001/0001000001-24-000030"]


def test_summary_and_delete_ED_019(tmp_path: Path):
    store = FilingStore(tmp_path)
    store.save(1000001, ACC, "abc")
    store.save(1000002, "0001000002-24-000003", "defgh")
    assert store.summary() == {"filing_count": 2, "total_bytes": 8, "ciks": [1000001, 1000002]}
    store.delete(1000001, ACC)
    assert store.load(1000001, ACC) is None and store.summary()["filing_count"] == 1


def test_keys_cannot_leave_the_root_and_accessions_are_checked_ED_019(tmp_path: Path):
    with pytest.raises(ValueError):
        FilingStore(tmp_path).save(1000001, "../../etc/passwd", "x")
    with pytest.raises(ValueError):
        LocalBackend(tmp_path).put("../outside.txt", b"x")


def test_cached_transport_reads_the_store_second_time_ED_019(tmp_path: Path):
    inner = RecordedTransport()
    cached = CachedTransport(inner, FilingStore(tmp_path))
    first = cached.submission_text(1000001, ACC)
    second = cached.submission_text(1000001, ACC)
    assert first == second == _text()
    assert (cached.misses, cached.hits) == (1, 1) and [c for c in inner.calls if c[0] == "submission_text"] == [("submission_text", ACC)]


def test_cached_transport_does_not_cache_failures_ED_019(tmp_path: Path):
    store = FilingStore(tmp_path)
    cached = CachedTransport(DownTransport(), store)
    with pytest.raises(TransportError):
        cached.submission_text(1000001, ACC)
    assert store.load(1000001, ACC) is None and store.summary()["filing_count"] == 0


def test_cached_transport_works_under_a_client_with_point_in_time_ED_019(tmp_path: Path):
    from datetime import date

    inner = RecordedTransport()
    client = SecClient(CachedTransport(inner, FilingStore(tmp_path)), identity_reader=lambda: IDENTITY)
    assert filings.current_report(client, "NWND", as_of=date(2024, 6, 1)).ok
    assert filings.current_report(client, "NWND", as_of=date(2024, 6, 1)).ok
    assert [c for c in inner.calls if c[0] == "submission_text"] == [("submission_text", ACC)]
    assert filings.current_report(client, "NWND", as_of=date(2024, 5, 1)).status == "NO_DATA"       # the store never shows a later filing


def test_library_storage_summary_ED_019(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EDGAR_LOCAL_DATA_DIR", str(tmp_path / "before"))
    info = library_storage_info(tmp_path / "library")
    assert info["filing_count"] == 0 and info["file_count"] == 0 and info["storage_path"].endswith("library")
