"""ED-020: live filing feed, latest filings and alerts."""
from __future__ import annotations

from datetime import date, datetime, timezone

from hokma.data.sec import live
from hokma.data.sec.result import DATA_UNAVAILABLE, NO_DATA, OK, STALE
from tests.data.sec.support import DownTransport, RecordedTransport, make_client


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_current_feed_entries_newest_first_ED_020():
    got = live.current_filings(make_client(), as_of=_at("2024-11-22T23:00:00"))
    assert got.status == OK
    assert [(f.form, f.cik, f.accession) for f in got.value] == [
        ("8-K", 1000001, "0001000001-24-000090"), ("4", 1900002, "0001000001-24-000091"), ("8-K", 1000030, "0001000030-24-000020"), ("10-Q", 1000004, "0001000004-24-000003")]
    first = got.value[0]
    assert first.company == "Northwind Instruments Inc." and first.filing_date == date(2024, 11, 22) and first.accepted == _at("2024-11-22T22:32:10")


def test_current_feed_does_not_show_filings_after_as_of_ED_020():
    got = live.current_filings(make_client(), as_of=_at("2024-11-22T21:30:00"))     # the 8-K of Northwind was accepted at 22:32 UTC
    assert [f.accession for f in got.value] == ["0001000001-24-000091", "0001000030-24-000020", "0001000004-24-000003"]
    assert live.current_filings(make_client(), as_of=_at("2024-11-21T00:00:00")).status == NO_DATA


def test_current_feed_form_filter_ED_020():
    got = live.current_filings(make_client(), as_of=_at("2024-11-22T23:00:00"), form="8-K")
    assert [f.cik for f in got.value] == [1000001, 1000030]


def test_current_feed_older_than_a_day_and_a_half_is_stale_ED_020():
    got = live.current_filings(make_client(), as_of=_at("2024-11-25T12:00:00"))
    assert got.status == STALE and got.reason.startswith("newest_feed_entry_is_") and got.value is None      # the old rows are not returned


def test_latest_filings_of_a_company_ED_020():
    got = live.latest_filings(make_client(), "NWND", as_of=date(2024, 5, 10), count=2)
    assert [(r.form, r.filing_date.isoformat()) for r in got.value] == [("4", "2024-05-08"), ("8-K", "2024-05-02")]
    assert all(r.filing_date <= date(2024, 5, 10) for r in got.value)


def test_filings_since_alert_list_uses_acceptance_time_ED_020():
    client = make_client()
    got = live.filings_since(client, "NWND", since=_at("2024-05-02T16:07:00"), as_of=date(2024, 5, 9))
    assert [r.accession for r in got.value] == ["0001000001-24-000021", "0001000001-24-000050"]
    # the 10-Q was accepted at 16:05, before 'since'; the 8-K at 16:10, after
    assert live.filings_since(client, "NWND", since=_at("2024-05-09T00:00:00"), as_of=date(2024, 5, 9)).status == NO_DATA


def test_check_alerts_reports_each_filing_once_ED_020():
    client = make_client()
    first = live.check_alerts(client, ["NWND", "SCWO"], ["4", "8-K"], set(), as_of=date(2024, 5, 30))
    accessions = [a.ref.accession for a in first.value["alerts"]]
    assert accessions == sorted(accessions, key=lambda a: next(x.ref.accepted for x in first.value["alerts"] if x.ref.accession == a))
    assert "0001000001-24-000021" in accessions and "0001000001-24-000051" in accessions and "0001000001-24-000030" not in accessions
    second = live.check_alerts(client, ["NWND", "SCWO"], ["4", "8-K"], first.value["seen"], as_of=date(2024, 5, 30))
    assert second.value["alerts"] == []
    later = live.check_alerts(client, ["NWND"], ["8-K"], first.value["seen"], as_of=date(2024, 7, 20))
    assert [a.ref.accession for a in later.value["alerts"]] == ["0001000001-24-000030"]


def test_check_alerts_continues_when_a_symbol_is_unavailable_ED_020():
    client = make_client(RecordedTransport())
    got = live.check_alerts(client, [1000001, 999999999], ["8-K"], set(), as_of=date(2024, 5, 30))
    assert got.status == OK and len(got.value["alerts"]) == 1 and got.warnings == ()          # an unknown cik is NO_DATA, not a failure


def test_live_without_identity_or_provider_ED_020():
    assert live.current_filings(make_client(identity=None), as_of=_at("2024-11-22T23:00:00")).reason == "sec_identity_missing"
    got = live.current_filings(make_client(DownTransport()), as_of=_at("2024-11-22T23:00:00"))
    assert got.status == DATA_UNAVAILABLE and got.value is None
    down = live.check_alerts(make_client(DownTransport()), [1000001], ["8-K"], set(), as_of=date(2024, 5, 30))
    assert down.status == OK and down.warnings and down.value["alerts"] == []
