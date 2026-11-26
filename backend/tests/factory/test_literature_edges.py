"""AS-018: the table of published edge ideas. Sharpe ratios are gross as reported, nothing is derived as a net value."""
import csv
import math
import re
from pathlib import Path

import pytest

from hokma.factory.registry import EDGE_CLASSES, LITERATURE_FILE, factory_literature

ROOT = Path(__file__).resolve().parents[3]
COLUMNS = ["edge_id", "asset_class", "idea", "sharpe_reported", "sharpe_basis", "t_stat", "years_tested", "ref"]
# Hand counted from the source table: rows per asset class and how many of them have t at or above 3.
PER_CLASS = {"equities": 12, "bonds": 12, "commodities": 3, "currencies": 4, "crypto": 8, "derivatives": 10, "multi_asset": 12}
ABOVE_3 = {"equities": 12, "bonds": 8, "commodities": 1, "currencies": 3, "crypto": 6, "derivatives": 7, "multi_asset": 12}


def rows() -> list[dict[str, str]]:
    with open(LITERATURE_FILE, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_the_table_has_the_columns_and_61_rows():
    with open(LITERATURE_FILE, newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == COLUMNS
    assert len(rows()) == 61


def test_rows_per_asset_class_match_the_source():
    counts = {c: sum(1 for r in rows() if r["asset_class"] == c) for c in EDGE_CLASSES}
    assert counts == PER_CLASS


def test_no_row_carries_a_net_sharpe():
    assert not [c for c in COLUMNS if "net" in c.lower()]
    assert {r["sharpe_basis"] for r in rows()} == {"gross"}


def test_ids_and_refs_are_unique_and_registered_in_the_notices():
    notices = (ROOT / ".notices" / "THIRD_PARTY.txt").read_text(encoding="utf-8")
    ids = [r["edge_id"] for r in rows()]
    refs = [r["ref"] for r in rows()]
    assert len(set(ids)) == 61 and len(set(refs)) == 61
    for ref in refs:
        assert re.fullmatch(r"LIT-\d{3}", ref)
        assert f"- {ref}: " in notices, ref


def test_t_statistic_agrees_with_sharpe_times_root_of_years():
    # The source states t = Sharpe x sqrt(years) on the strategy's own window; years are rounded, so allow 5 percent.
    for r in rows():
        expected = float(r["sharpe_reported"]) * math.sqrt(int(r["years_tested"]))
        assert abs(expected - float(r["t_stat"])) <= 0.05 * float(r["t_stat"]), r["edge_id"]


def test_ideas_are_short_plain_text():
    for r in rows():
        assert 10 <= len(r["idea"]) <= 160 and not re.search(r"[<>&]", r["idea"]), r["edge_id"]


def test_min_t_three_keeps_the_hand_counted_rows_with_the_strongest_first():
    kept = factory_literature(min_t=3)
    assert len(kept) == sum(ABOVE_3.values()) == 49
    assert kept[0]["edge_id"] == "E-032" and kept[0]["t_stat"] == 16.2 and kept[0]["sharpe_reported"] == 3.39
    assert all(r["t_stat"] >= 3 for r in kept)
    assert [r["t_stat"] for r in kept] == sorted((r["t_stat"] for r in kept), reverse=True)
    # a row at exactly 3.0 is kept, one at 2.9 is not
    assert any(r["t_stat"] == 3.0 for r in kept) and not any(r["t_stat"] == 2.9 for r in kept)


@pytest.mark.parametrize("asset_class", EDGE_CLASSES)
def test_min_t_three_by_asset_class(asset_class):
    assert len(factory_literature(min_t=3, asset_class=asset_class)) == ABOVE_3[asset_class]
    assert len(factory_literature(asset_class=asset_class)) == PER_CLASS[asset_class]


def test_without_a_filter_every_row_is_returned_as_numbers():
    everything = factory_literature()
    assert len(everything) == 61
    first = everything[0]
    assert isinstance(first["sharpe_reported"], float) and isinstance(first["t_stat"], float) and isinstance(first["years_tested"], int)


@pytest.mark.parametrize("bad", [True, "3", float("nan"), float("inf"), [3]])
def test_a_bad_min_t_is_rejected(bad):
    with pytest.raises(ValueError):
        factory_literature(min_t=bad)


def test_an_unknown_asset_class_is_rejected():
    with pytest.raises(ValueError):
        factory_literature(asset_class="options")
