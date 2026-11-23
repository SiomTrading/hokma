"""Sentinels and the result cache (SPEC-017: TA-53, FT-099)."""
from datetime import datetime, timedelta, timezone

import pytest

from hokma.data import sentinels
from hokma.data.cache import CATEGORIES, ResultCache, make_key
from hokma.data.providers import NoDataError, RateLimitError, route

from .support import no_network, when  # noqa: F401

UTC = timezone.utc


class Clock:
    def __init__(self, start: str = "2024-04-01T12:00:00+00:00") -> None:
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def test_provider_without_rows_is_no_data_and_a_down_one_is_unavailable_TA_53() -> None:
    impls = {"quiet": lambda: [], "down": lambda: (_ for _ in ()).throw(RateLimitError("503 from host", reason="provider_unreachable"))}
    empty = route("t", impls, ["quiet"])
    down = route("t", impls, ["down"])
    assert (empty.status, empty.source, empty.reason, empty.value) == (sentinels.NO_DATA, "quiet", "empty_result", None)
    assert (down.status, down.source, down.reason, down.value) == (sentinels.DATA_UNAVAILABLE, "down", "provider_unreachable", None)


def test_the_agent_gets_the_sentinel_not_a_zero_TA_53() -> None:
    result = sentinels.no_data("prov", "empty_result")
    assert result.value is None and not result.ok
    with pytest.raises(ValueError):
        sentinels.DataResult(0, "prov", sentinels.utcnow(), status=sentinels.NO_DATA, reason="x")   # a zero beside a sentinel is refused
    with pytest.raises(ValueError):
        sentinels.DataResult(None, "prov", sentinels.utcnow(), status=sentinels.DATA_UNAVAILABLE)  # a failure must say why


def test_result_declares_source_and_reception_time_TA_53() -> None:
    result = sentinels.ok([1], source="prov", as_of=when("2024-03-01"))
    data = result.to_dict()
    assert data["source"] == "prov" and data["received_at"] and data["as_of"].startswith("2024-03-01")


def test_scrub_removes_keys_TA_53() -> None:
    assert sentinels.scrub("GET /x?apikey=SECRET123 failed", "SECRET123") == "GET /x?apikey=*** failed"


def test_cache_ttl_hit_and_expiry_FT_099() -> None:
    clock = Clock()
    cache = ResultCache(clock=clock)
    key = make_key("bars", "ACME", as_of=when("2024-03-01"), interval="1D")
    cache.put("bars", key, [1, 2, 3], source="prov", ttl_seconds=60, as_of=when("2024-03-01"))
    hit = cache.get("bars", key)
    assert hit is not None and hit.value == [1, 2, 3] and hit.source == "prov" and hit.received_at == clock.now and hit.meta["cached"]
    clock.advance(seconds=61)
    assert cache.get("bars", key) is None          # expired: the entry is gone, not served
    assert cache.purge_expired() == 0


def test_cache_cut_as_of_never_returns_a_later_entry_FT_099() -> None:
    cache = ResultCache(clock=Clock())
    cache.put("macro", "k", {"v": 1}, source="prov", ttl_seconds=600, as_of=when("2024-03-01"))
    assert cache.get("macro", "k", as_of=when("2024-02-01")) is None
    assert cache.get("macro", "k", as_of=when("2024-03-01")) is not None


def test_cache_never_stores_failures_FT_099() -> None:
    cache = ResultCache(clock=Clock())
    assert cache.put_result("bars", "k", sentinels.no_data("prov", "empty_result"), ttl_seconds=60) is False
    assert cache.put_result("bars", "k", sentinels.unavailable("prov", "rate_limited"), ttl_seconds=60) is False
    assert cache.get("bars", "k") is None
    assert cache.put_result("bars", "k", sentinels.ok({"a": 1}, source="prov"), ttl_seconds=60) is True
    with pytest.raises(ValueError):
        cache.put("bars", "k2", None, source="prov", ttl_seconds=60)


def test_cache_coverage_reports_gaps_and_never_fills_them_FT_099() -> None:
    cache = ResultCache(clock=Clock())
    cache.record_coverage("bars", "ACME", when("2024-01-01"), when("2024-01-31"))
    cache.record_coverage("bars", "ACME", when("2024-01-20"), when("2024-02-15"))      # overlaps: merged
    cache.record_coverage("bars", "ACME", when("2024-03-01"), when("2024-03-31"))
    assert cache.coverage("bars", "ACME") == [(when("2024-01-01"), when("2024-02-15")), (when("2024-03-01"), when("2024-03-31"))]
    assert cache.covers("bars", "ACME", when("2024-01-05"), when("2024-02-01"))
    assert cache.gaps("bars", "ACME", when("2024-01-01"), when("2024-03-31")) == [(when("2024-02-15"), when("2024-03-01"))]
    assert cache.gaps("bars", "OTHER", when("2024-01-01"), when("2024-01-02")) == [(when("2024-01-01"), when("2024-01-02"))]


def test_cache_covers_the_eight_modules_and_rejects_others_FT_099(tmp_path) -> None:
    assert len(CATEGORIES) == 8
    cache = ResultCache(tmp_path / "cache.sqlite")            # a file: survives a new connection
    cache.put("filings", "k", {"x": 1}, source="s", ttl_seconds=3600)
    cache.close()
    assert ResultCache(tmp_path / "cache.sqlite").get("filings", "k").value == {"x": 1}
    with pytest.raises(ValueError):
        cache.put("prices", "k", 1, source="s", ttl_seconds=1)


def test_make_key_depends_on_every_part_FT_099() -> None:
    base = make_key("bars", "A", as_of=when("2024-01-01"), interval="1D")
    assert base == make_key("bars", "A", as_of=when("2024-01-01"), interval="1D")
    assert len({base, make_key("bars", "B", as_of=when("2024-01-01"), interval="1D"), make_key("bars", "A", as_of=when("2024-01-02"), interval="1D"),
                make_key("bars", "A", as_of=when("2024-01-01"), interval="1h")}) == 4
