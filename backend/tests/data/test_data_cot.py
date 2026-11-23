"""Commitment of Traders from the paid provider (SPEC-017: FT-098, CC-08)."""
import json

from hokma.data import cot as cot_module, sentinels
from hokma.data.cot import cot

from .support import RecordedTransport, constant, fixture_bytes, fixture_json, no_network, when  # noqa: F401

NOW = when("2024-04-10T12:00:00")
key = lambda provider: "FMP-SECRET"


def call(as_of="2024-03-26", transport=None, **kwargs):
    return cot("NG", as_of=when(as_of), key=kwargs.pop("key", key), transport=transport or RecordedTransport(constant(200, fixture_bytes("fmp_cot.json"))),
               clock=lambda: NOW, **kwargs)


def test_source_is_the_paid_provider_not_the_regulator_CC_08() -> None:
    result = call("2024-03-29")
    assert result.ok and result.source == "fmp" and result.source != "cftc" and "regulator" in result.meta["publisher_note"]
    assert result.received_at == NOW


def test_without_the_providers_key_the_answer_is_unavailable_with_the_reason_CC_08() -> None:
    transport = RecordedTransport(constant(200, fixture_bytes("fmp_cot.json")))
    result = call(key=lambda p: None, transport=transport)
    assert (result.status, result.source, result.reason, result.value) == (sentinels.DATA_UNAVAILABLE, "fmp", "provider_key_missing", None)
    assert transport.calls == []
    rejected = call(transport=RecordedTransport(constant(401)))
    assert rejected.reason == "provider_key_missing" and rejected.source == "fmp"


def test_a_report_is_used_only_from_its_publication_day_FT_098() -> None:
    before = call("2024-03-28")                                    # the report of Tuesday 03-26 is published Friday 03-29
    assert [r["date"] for r in before.value["reports"]] == ["2024-03-12", "2024-03-19"]
    after = call("2024-03-29")
    assert [r["date"] for r in after.value["reports"]] == ["2024-03-12", "2024-03-19", "2024-03-26"]
    assert after.value["reports"][-1]["published_on"] == "2024-03-29"


def test_reports_carry_named_positions_and_no_gap_is_filled_FT_098() -> None:
    report = call("2024-03-29").value["reports"][-1]
    assert report["open_interest"] == 1500000 and report["non_commercial_long"] == 300000 and report["commercial_short"] == 600000
    assert "non_reportable_long" not in report                      # a field the provider did not send is absent, not zero
    assert call("2024-03-29").value["market"] == "NG"


def test_request_and_sentinels_FT_098() -> None:
    transport = RecordedTransport(constant(200, fixture_bytes("fmp_cot.json")))
    call("2024-03-29", transport=transport, lookback_days=30)
    url, params, _ = transport.calls[0]
    assert url == cot_module.URL and params["symbol"] == "NG" and params["from"] == "2024-02-28" and params["to"] == "2024-03-29"
    empty = call(transport=RecordedTransport(constant(200, b"[]")))
    assert empty.status == sentinels.NO_DATA and "regulator" in empty.warnings[0]
    early = call("2024-01-01")
    assert early.status == sentinels.NO_DATA and early.reason == sentinels.REASON_NOT_PUBLISHED
    down = call(transport=RecordedTransport(constant(503)))
    assert (down.status, down.reason) == (sentinels.DATA_UNAVAILABLE, "provider_unreachable")
    assert call(transport=RecordedTransport(constant(200, b"not json"))).reason == "bad_response"
    assert "FMP-SECRET" not in json.dumps(down.to_dict(), default=str)
