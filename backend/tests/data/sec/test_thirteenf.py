"""ED-001, ED-002 and ED-003: 13F positions, comparison and history, value unit resolution, signer heuristic."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from hokma.data.sec import thirteenf as t13
from hokma.data.sec.result import DATA_UNAVAILABLE, NO_DATA, OK
from tests.data.sec.support import DownTransport, make_client

MANAGER = 1000010


def test_latest_report_positions_ED_001():
    got = t13.latest_report(make_client(), MANAGER, as_of=date(2022, 3, 1), unit="thousands")
    report = got.value
    assert got.status == OK and report.period == date(2021, 12, 31) and report.manager_name == "HARBOR CAPITAL MANAGEMENT LLC"
    assert report.entry_count == 4 and report.ref.accession == "0001000010-22-000002"
    apple = report.by_cusip()["037833100"]
    assert (apple.issuer, apple.ticker, apple.shares, apple.value_reported) == ("APPLE INC", "AAPL", 120000.0, 21308.0)
    assert apple.value_usd == 21308.0 * 1000 and apple.sole_voting == 120000.0
    assert report.total_value_usd == (21308 + 16816 + 5284 + 3334) * 1000.0


def test_report_filed_after_as_of_is_not_visible_ED_001():
    got = t13.latest_report(make_client(), MANAGER, as_of=date(2022, 2, 10))              # Q4 was filed 2022-02-11
    assert got.value.period == date(2021, 9, 30)
    assert t13.latest_report(make_client(), MANAGER, as_of=date(2021, 11, 1)).status == NO_DATA


def test_history_newest_first_and_limited_ED_001():
    got = t13.holdings_history(make_client(), MANAGER, as_of=date(2022, 6, 1), quarters=3, unit="thousands")
    assert [r.period for r in got.value] == [date(2022, 3, 31), date(2021, 12, 31), date(2021, 9, 30)]
    two = t13.holdings_history(make_client(), MANAGER, as_of=date(2022, 6, 1), quarters=2, unit="thousands")
    assert len(two.value) == 2
    assert [h.cusip for h in got.value[0].holdings if h.issuer == "AMAZON COM INC"] == ["023135106"]


def test_position_changes_between_two_quarters_ED_001():
    got = t13.position_changes(make_client(), MANAGER, as_of=date(2022, 2, 20), unit="thousands")
    assert got.value["current_period"] == date(2021, 12, 31) and got.value["previous_period"] == date(2021, 9, 30)
    changes = {c.issuer: c for c in got.value["changes"]}
    assert changes["APPLE INC"].status == "INCREASED" and changes["APPLE INC"].shares_change == 20000.0
    assert changes["APPLE INC"].pct_change == pytest.approx(0.2, abs=1e-12)
    assert changes["MICROSOFT CORP"].status == "UNCHANGED" and changes["MICROSOFT CORP"].pct_change == 0.0
    assert changes["NVIDIA CORP"].status == "CLOSED" and changes["NVIDIA CORP"].shares_current == 0.0
    assert changes["AMAZON COM INC"].status == "NEW" and changes["AMAZON COM INC"].pct_change is None
    assert changes["TESLA INC"].status == "UNCHANGED"
    weights = sum(c.weight_current for c in got.value["changes"])
    assert weights == pytest.approx(1.0, abs=1e-12)
    top = got.value["changes"][0]
    assert top.issuer == "APPLE INC" and top.weight_current == pytest.approx(21308 / (21308 + 16816 + 5284 + 3334), abs=1e-12)


def test_position_changes_with_one_report_is_no_data_ED_001():
    got = t13.position_changes(make_client(), MANAGER, as_of=date(2021, 12, 1))
    assert got.status == NO_DATA and got.reason == "only_one_holdings_report_visible"


def test_ambiguous_unit_warns_instead_of_guessing_ED_002():
    got = t13.latest_report(make_client(), MANAGER, as_of=date(2022, 3, 1))
    report = got.value
    assert report.unit.ambiguous is True and report.unit.priceable_rows == 4 and report.unit.warning
    assert any("ambiguous" in w for w in got.warnings)
    decided = t13.latest_report(make_client(), MANAGER, as_of=date(2022, 3, 1), unit="thousands").value
    assert decided.unit.source == "override" and decided.unit.warning is None and decided.unit.multiplier == 1000


def test_unit_resolved_from_implied_prices_when_there_are_enough_rows_ED_002():
    thousands = pd.DataFrame({"Value": [5] * 12, "SharesPrnAmount": [100] * 12, "Type": ["Shares"] * 12, "Cusip": [f"c{i}" for i in range(12)]})
    got = t13.resolve_unit(thousands, "X0202", date(2022, 12, 31))
    assert (got.unit, got.multiplier, got.source, got.ambiguous, got.warning) == ("thousands", 1000, "implied_prices", False, None)
    dollars = pd.DataFrame({"Value": [5000] * 12, "SharesPrnAmount": [100] * 12, "Type": ["Shares"] * 12, "Cusip": [f"c{i}" for i in range(12)]})
    assert t13.resolve_unit(dollars, "X0202", date(2022, 12, 31)).unit == "dollars"


def test_unit_override_must_be_a_known_unit_ED_002():
    with pytest.raises(ValueError):
        t13.resolve_unit(pd.DataFrame({"Value": [1], "SharesPrnAmount": [1], "Type": ["Shares"]}), None, None, "millions")


def test_signer_heuristic_ED_003():
    assert t13.classify_signer("A B", "Chief Investment Officer").likely_portfolio_manager is True
    assert t13.classify_signer("A B", "Portfolio Manager").role == "investment_decision_maker"
    assert t13.classify_signer("A B", "Managing Partner").likely_portfolio_manager is True
    compliance = t13.classify_signer("A B", "Chief Compliance Officer")
    assert compliance.role == "control_function" and compliance.likely_portfolio_manager is False
    assert t13.classify_signer("A B", "Chief Investment Officer and Compliance Officer").likely_portfolio_manager is False
    assert t13.classify_signer("A B", None).role == "unknown"
    assert t13.classify_signer(None, "Vice Gardener").role == "unknown"


def test_manager_profile_names_who_signs_ED_003():
    got = t13.manager_profile(make_client(), MANAGER, as_of=date(2022, 6, 1))
    profile = got.value
    assert got.status == OK and profile["manager_name"] == "HARBOR CAPITAL MANAGEMENT LLC" and profile["period"] == date(2022, 3, 31)
    assert profile["signer"].name == "Dana Whitfield" and profile["signer"].title == "Chief Compliance Officer"
    assert profile["signer"].role == "control_function" and profile["entry_count"] == 3


def test_thirteenf_without_identity_or_provider_ED_001():
    assert t13.latest_report(make_client(identity=None), MANAGER, as_of=date(2022, 3, 1)).reason == "sec_identity_missing"
    got = t13.holdings_history(make_client(DownTransport()), MANAGER, as_of=date(2022, 3, 1))
    assert got.status == DATA_UNAVAILABLE and got.value is None
