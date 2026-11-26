"""Expiration vectors (SPEC-020: L-07): last trading day of each contract month, one row per contract, checked against
`hokma.contracts.expiry`. The vectors are public exchange calendar facts kept in `fixtures/expiry_vectors.csv`."""
import csv
from datetime import date
from pathlib import Path

import pytest

from hokma.contracts import expiry

FIXTURE = Path(__file__).with_name("fixtures") / "expiry_vectors.csv"
VECTORS = list(csv.DictReader(FIXTURE.open(encoding="utf-8", newline="")))


def test_the_fixture_covers_every_rule_of_the_table():
    covered = {v["root"] for v in VECTORS}
    rules_in_use = {expiry.root_info(root).rule for root in covered}
    assert rules_in_use == {expiry.root_info(root).rule for root in expiry.roots() if root not in ("MNQ", "M2K")}
    assert len(VECTORS) == 355


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: f"{v['root']}-{v['contract_month']}")
def test_every_vector_passes(vector):
    year, month = (int(x) for x in vector["contract_month"].split("-"))
    assert expiry.rules(vector["root"]).expiry(year, month).day == date.fromisoformat(vector["last_trade"])


def test_the_listed_months_of_each_root_agree_with_the_vectors():
    for root in {v["root"] for v in VECTORS}:
        months = {int(v["contract_month"][5:7]) for v in VECTORS if v["root"] == root}
        listed = set(expiry.cycles(root))
        if expiry.root_info(root).month_policy == "yes":
            assert months <= listed, root                  # a quarterly root has no vector outside its cycle
        if len([v for v in VECTORS if v["root"] == root]) >= 20:
            assert listed <= months, root                  # a long vector list has a vector for every month of the cycle
