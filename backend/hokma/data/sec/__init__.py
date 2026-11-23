"""SEC EDGAR data: point in time filings, ownership, funds, financial facts and XBRL (SPEC-017, SVC-data).

Every public function takes a `SecClient` and an `as_of` cut and returns a `SecResult`. A filing filed after `as_of` is
never visible, a missing identity or a provider failure is `DATA_UNAVAILABLE` with a reason, and a gap is never filled.
"""
from hokma.data.sec.client import SecClient
from hokma.data.sec.result import DATA_UNAVAILABLE, NO_DATA, OK, STALE, SecResult

__all__ = ["SecClient", "SecResult", "OK", "NO_DATA", "DATA_UNAVAILABLE", "STALE"]
