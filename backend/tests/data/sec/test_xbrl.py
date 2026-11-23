"""ED-021, ED-022, ED-023, ED-024 and ED-027: XBRL parse and query, readers, standardization, stitching, validation."""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from hokma.data.sec import xbrl
from hokma.data.sec.result import DATA_UNAVAILABLE, NO_DATA, OK
from tests.data.sec.support import FIXTURES, DownTransport, make_client


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_library_document_query_by_concept_and_dimension_ED_021():
    doc = xbrl.XbrlDocument.from_instance(_read("nwnd_instance_2023.xml"))
    plain = doc.facts("Revenues", dimensioned=False)
    assert sorted(r["numeric_value"] for r in plain) == [367900000.0, 412000000.0]
    segment = doc.facts("us-gaap:Revenues", dimensioned=True)
    assert [r["numeric_value"] for r in segment] == [300000000.0] and segment[0]["is_dimensioned"] is True
    fy23 = [r for r in plain if r["fiscal_year"] == 2023]
    assert fy23[0]["period_start"] == "2023-01-01" and fy23[0]["period_end"] == "2023-12-31" and fy23[0]["unit_ref"] == "usd"
    assert "us-gaap:Assets" in doc.concepts() and "nw:CalibrationContractsActive" in doc.concepts()
    assert doc.facts("NoSuchConcept") == []


def test_parse_instance_contexts_units_facts_ED_022():
    inst = xbrl.parse_instance(_read("nwnd_instance_2023.xml"))
    assert inst.units == {"usd": "USD", "usdPerShare": "USD/shares", "shares": "shares"}
    seg = inst.contexts["c_seg_fy23"]
    assert seg.dimensions == (("srt:ProductOrServiceAxis", "nw:SensorsMember"),) and seg.start == date(2023, 1, 1) and seg.end == date(2023, 12, 31)
    assert inst.contexts["c_end23"].instant == date(2023, 12, 31) and inst.contexts["c_end23"].period_key == "instant_2023-12-31"
    revenue = [f for f in inst.facts if f.concept == "us-gaap:Revenues" and f.context == "c_fy23"][0]
    assert (revenue.numeric, revenue.unit, revenue.decimals) == (412000000.0, "usd", "-5")
    name = [f for f in inst.facts if f.concept == "dei:EntityRegistrantName"][0]
    assert name.value == "Northwind Instruments Inc." and name.numeric is None


def test_parse_instance_rejects_what_is_not_an_instance_ED_022():
    with pytest.raises(ValueError):
        xbrl.parse_instance("<html><body/></html>")
    with pytest.raises(ValueError):
        xbrl.parse_instance("<xbrl")
    with pytest.raises(ValueError):                                   # an entity declaration is never expanded
        xbrl.parse_instance('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><html>&e;</html>')


def test_parse_linkbases_ED_022():
    calc = xbrl.parse_linkbase(_read("nwnd_cal.xml"), "calculation")
    assert [(a.parent, a.child, a.weight, a.order) for a in calc] == [("us-gaap:Assets", "us-gaap:Liabilities", 1.0, 1.0), ("us-gaap:Assets", "us-gaap:StockholdersEquity", 1.0, 2.0)]
    pre = xbrl.parse_linkbase(_read("nwnd_pre.xml"), "presentation")
    assert [a.child for a in pre] == ["us-gaap:EarningsPerShareDiluted", "us-gaap:NetIncomeLoss"] and pre[0].weight is None
    assert pre[0].role.endswith("/IncomeStatement")
    with pytest.raises(ValueError):
        xbrl.parse_linkbase("<a/>", "definition")


def test_standardizer_maps_and_registers_what_is_not_mapped_ED_023():
    inst = xbrl.parse_instance(_read("nwnd_instance_2023.xml"))
    std = xbrl.Standardizer()
    got = std.standardize(inst)
    assert got.values["revenue"] == {"duration_2023-01-01_2023-12-31": 412000000.0, "duration_2022-01-01_2022-12-31": 367900000.0}
    assert got.values["total_assets"]["instant_2023-12-31"] == 338000000.0 and got.values["diluted_eps"]["duration_2023-01-01_2023-12-31"] == 1.25
    assert got.unmapped == {"nw:CalibrationContractsActive": 1}          # segment facts are skipped, the custom concept is reported
    registry = std.registry()
    assert registry == [("nw:CalibrationContractsActive", 1, True)]
    std.standardize(inst)
    assert std.registry() == [("nw:CalibrationContractsActive", 2, True)]


def test_standardizer_prefers_the_first_concept_of_a_name_and_never_adds_ED_023():
    inst = xbrl.parse_instance(_read("nwnd_instance_2023.xml"))
    extra = xbrl.XFact("us-gaap:SalesRevenueNet", "1", 1.0, "c_fy23", "usd", "0")
    both = replace(inst, facts=inst.facts + (extra,))
    assert xbrl.Standardizer().standardize(both).values["revenue"]["duration_2023-01-01_2023-12-31"] == 412000000.0
    custom = xbrl.Standardizer({"sales": ("nw:Sales",)})
    assert custom.standardize(inst).values == {} and ("us-gaap:Revenues", 2, False) in custom.registry()


def test_stitch_takes_the_latest_filing_and_reports_the_restatement_ED_024():
    f23 = xbrl.FilingFacts("0001000001-24-000010", date(2024, 2, 20), xbrl.parse_instance(_read("nwnd_instance_2023.xml")))
    f22 = xbrl.FilingFacts("0001000001-23-000010", date(2023, 2, 21), xbrl.parse_instance(_read("nwnd_instance_2022.xml")))
    points = xbrl.stitch([f22, f23], "us-gaap:Revenues")
    assert [(p.period_end, p.value) for p in points] == [(date(2021, 12, 31), 330000000.0), (date(2022, 12, 31), 367900000.0), (date(2023, 12, 31), 412000000.0)]
    restated = points[1]
    assert restated.restated_from == 365000000.0 and restated.source_accession == "0001000001-24-000010" and restated.filed == date(2024, 2, 20)
    assert points[0].restated_from is None and points[0].source_accession == "0001000001-23-000010"
    assert [p.value for p in xbrl.stitch([f22, f23], "us-gaap:NetIncomeLoss")] == [27000000.0, 31000000.0, 38400000.0]


def test_stitch_instant_quarterly_and_bad_period_ED_024():
    f23 = xbrl.FilingFacts("a", date(2024, 2, 20), xbrl.parse_instance(_read("nwnd_instance_2023.xml")))
    f22 = xbrl.FilingFacts("b", date(2023, 2, 21), xbrl.parse_instance(_read("nwnd_instance_2022.xml")))
    assets = xbrl.stitch([f23, f22], "us-gaap:Assets", period="instant")
    assert [(p.period_end, p.value, p.period_start) for p in assets] == [(date(2022, 12, 31), 301000000.0, None), (date(2023, 12, 31), 338000000.0, None)]
    assert xbrl.stitch([f23], "us-gaap:Revenues", period="quarterly") == []        # the annual facts are not quarters
    with pytest.raises(ValueError):
        xbrl.stitch([f23], "us-gaap:Revenues", period="weekly")


def test_validate_clean_instance_collapses_the_identical_duplicate_ED_027():
    inst = xbrl.parse_instance(_read("nwnd_instance_2023.xml"))
    calc = xbrl.parse_linkbase(_read("nwnd_cal.xml"), "calculation")
    report = xbrl.validate_instance(inst, calculations=calc)
    assert report.ok and report.issues == () and report.duplicates_removed == 1
    assert len([f for f in report.facts if f.concept == "us-gaap:Assets" and f.context == "c_end23"]) == 1


def test_validate_reports_conflicts_missing_context_and_unit_ED_027():
    base = xbrl.parse_instance(_read("nwnd_instance_2023.xml"))
    bad = (xbrl.XFact("us-gaap:NetIncomeLoss", "38500000", 38500000.0, "c_fy23", "usd", "-5"),
           xbrl.XFact("us-gaap:Revenues", "1", 1.0, "c_missing", "usd", "0"),
           xbrl.XFact("us-gaap:Liabilities", "5", 5.0, "c_end23", "ghost", "0"),
           xbrl.XFact("us-gaap:Assets", "-1", -1.0, "c_end22", "usd", "0"))
    report = xbrl.validate_instance(replace(base, facts=base.facts + bad))
    assert not report.ok
    assert report.codes() == {"duplicate_conflict", "undefined_context", "undefined_unit", "negative_value"}
    conflict = [i for i in report.issues if i.code == "duplicate_conflict"][0]
    assert conflict.concept == "us-gaap:NetIncomeLoss" and conflict.context == "c_fy23"
    assert not [f for f in report.facts if f.concept == "us-gaap:NetIncomeLoss" and f.context == "c_fy23"]      # ambiguous facts are not passed on


def test_validate_duplicate_within_declared_rounding_is_kept_with_a_warning_ED_027():
    base = xbrl.parse_instance(_read("nwnd_instance_2023.xml"))
    near = xbrl.XFact("us-gaap:NetIncomeLoss", "38400001", 38400001.0, "c_fy23", "usd", "-5")
    report = xbrl.validate_instance(replace(base, facts=base.facts + (near,)))
    assert report.ok and "duplicate_within_rounding" in report.codes()
    assert [f.numeric for f in report.facts if f.concept == "us-gaap:NetIncomeLoss" and f.context == "c_fy23"] == [38400000.0]


def test_validate_calculation_mismatch_ED_027():
    base = xbrl.parse_instance(_read("nwnd_instance_2023.xml"))
    wrong = tuple(replace(f, value="219000000", numeric=219000000.0) if f.concept == "us-gaap:StockholdersEquity" else f for f in base.facts)
    report = xbrl.validate_instance(replace(base, facts=wrong), calculations=xbrl.parse_linkbase(_read("nwnd_cal.xml"), "calculation"))
    issue = [i for i in report.issues if i.code == "calculation_mismatch"]
    assert len(issue) == 1 and issue[0].concept == "us-gaap:Assets" and "339000000" in issue[0].detail and not report.ok


def test_filing_instance_service_point_in_time_ED_021():
    client = make_client()
    got = xbrl.filing_instance(client, "NWND", as_of=date(2024, 3, 1))
    assert got.status == OK and got.value["ref"].accession == "0001000001-24-000010" and len(got.value["instance"].facts) > 10
    before = xbrl.filing_instance(client, "NWND", as_of=date(2023, 3, 1))
    assert before.value["ref"].accession == "0001000001-23-000010"
    assert xbrl.filing_instance(client, "NWND", as_of=date(2023, 1, 1)).status == NO_DATA
    assert xbrl.filing_instance(client, "NWND", as_of=date(2024, 5, 10), form="10-Q").reason == "no_xbrl_instance_document"


def test_stitched_series_service_hides_the_filing_not_yet_filed_ED_024():
    client = make_client()
    got = xbrl.stitched_series(client, "NWND", "Revenues", as_of=date(2024, 3, 1))
    assert [(p.period_end.year, p.value) for p in got.value] == [(2021, 330000000.0), (2022, 367900000.0), (2023, 412000000.0)]
    assert got.warnings == ("restated:2022-12-31:365000000.0->367900000.0",)
    early = xbrl.stitched_series(client, "NWND", "Revenues", as_of=date(2023, 3, 1))
    assert [(p.period_end.year, p.value) for p in early.value] == [(2021, 330000000.0), (2022, 365000000.0)] and early.warnings == ()
    assert xbrl.stitched_series(client, "NWND", "NoSuchTag", as_of=date(2024, 3, 1)).reason == "concept_not_reported:NoSuchTag"


def test_xbrl_services_without_identity_or_provider_ED_021():
    assert xbrl.filing_instance(make_client(identity=None), "NWND", as_of=date(2024, 3, 1)).reason == "sec_identity_missing"
    got = xbrl.stitched_series(make_client(DownTransport()), 1000001, "Revenues", as_of=date(2024, 3, 1))
    assert got.status == DATA_UNAVAILABLE and got.value is None
