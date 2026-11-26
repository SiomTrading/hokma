"""The additive changes of the audit for the verdict (SPEC-032, OPT-03 and OPT-04): cost share, border of the grid, regime declaration, codes at the
start of the notes and the one place of the verdict precedence. test_audit.py keeps its cases; the old behavior is checked here without the new fields."""
import dataclasses
import re
from decimal import Decimal
from pathlib import Path

import pytest
from factory.test_audit import CONFIG, GOOD, by_key, result, spec, trade

import hokma
from hokma.factory import audit as au
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult

LIMITED = dataclasses.replace(CONFIG, max_cost_share=0.33)
ROOT = Path(hokma.__file__).resolve().parent


def item(key, evidence=GOOD, config=CONFIG, run=None):
    return by_key(au.audit(run or result(), spec(), evidence, config))[key]


# ---- costs ---------------------------------------------------------------------------------------------------------------------------

def test_costs_over_the_limit_fail_with_their_code_and_the_share():
    heavy = dataclasses.replace(GOOD, cost_sharpe=0.6, gross_sharpe=1.5)                       # 40 percent of the gross Sharpe
    found = item("costs_charged", heavy, LIMITED)
    assert found.status == "fail" and found.note.startswith("cost_share:") and "40%" in found.note and "33%" in found.note
    assert au.audit(result(), spec(), heavy, LIMITED).verdict == "no_go"


def test_costs_at_or_under_the_limit_pass_and_a_missing_piece_leaves_the_check_as_it_was():
    light = dataclasses.replace(GOOD, cost_sharpe=0.3, gross_sharpe=1.5)                       # 20 percent
    assert item("costs_charged", light, LIMITED).status == "pass"
    at_limit = dataclasses.replace(GOOD, cost_sharpe=0.5, gross_sharpe=1.5)
    assert item("costs_charged", at_limit, dataclasses.replace(CONFIG, max_cost_share=1 / 3)).status == "pass"      # the limit is "over", not "at"
    heavy = dataclasses.replace(GOOD, cost_sharpe=0.6, gross_sharpe=1.5)
    assert item("costs_charged", heavy, CONFIG).status == "pass"                              # no limit in the config: as today
    assert item("costs_charged", dataclasses.replace(GOOD, cost_sharpe=0.6), LIMITED).status == "pass"
    assert item("costs_charged", dataclasses.replace(GOOD, cost_sharpe=0.6, gross_sharpe=0.0), LIMITED).status == "pass"
    assert item("costs_charged", dataclasses.replace(GOOD, cost_sharpe=0.6, gross_sharpe=-1.0), LIMITED).status == "pass"


def test_costs_not_charged_keep_their_text_and_get_the_code():
    free = result(costs={"commission": Decimal(0), "slippage": Decimal(0), "swap": Decimal(0), "impact": Decimal(0)})
    found = item("costs_charged", GOOD, CONFIG, free)
    assert found.status == "fail" and found.note.startswith("no_costs:")
    assert found.note == "no_costs: commission 0 and slippage 0 USD: both must be above zero when trades exist"
    assert "cost_share" not in found.note


def test_max_cost_share_is_checked_when_it_is_given():
    assert dataclasses.replace(CONFIG, max_cost_share=1.0).max_cost_share == 1.0 and CONFIG.max_cost_share is None
    for bad in (0, 0.0, -0.1, 1.01, True, "0.3"):
        with pytest.raises(BadInput):
            dataclasses.replace(CONFIG, max_cost_share=bad)


# ---- neighbors and the border of the grid ---------------------------------------------------------------------------------------------

def test_the_border_of_the_grid_warns_when_the_neighbors_would_pass():
    edge = dataclasses.replace(GOOD, edge_of_space=("slow", "fast"))
    found = item("parameter_neighbors", edge)
    assert found.status == "warn" and found.note.startswith("edge:") and "slow, fast" in found.note and "worst neighbor" in found.note
    report = au.audit(result(), spec(), edge, CONFIG)
    assert report.verdict == "review" and report.phases[4].status == "warn"


def test_without_a_border_or_with_an_empty_one_the_check_is_the_old_one():
    assert item("parameter_neighbors").status == "pass" and item("parameter_neighbors").note.startswith("worst neighbor")
    assert item("parameter_neighbors", dataclasses.replace(GOOD, edge_of_space=())).status == "pass"
    assert item("parameter_neighbors", dataclasses.replace(GOOD, edge_of_space=None)).note == item("parameter_neighbors").note


def test_a_neighbor_that_fails_stays_a_fail_and_a_missing_neighbor_stays_unknown_even_on_the_border():
    weak = dataclasses.replace(GOOD, neighbor_sharpes={"fast-": 0.7, "slow+": 0.4}, edge_of_space=("slow",))
    found = item("parameter_neighbors", weak)
    assert found.status == "fail" and not found.note.startswith("edge:") and "slow+" in found.note
    assert item("parameter_neighbors", dataclasses.replace(GOOD, neighbor_sharpes=None, edge_of_space=("slow",))).status == "unknown"


# ---- regimes ---------------------------------------------------------------------------------------------------------------------------

def test_a_strategy_that_says_it_has_no_regimes_gets_na_and_none_or_true_act_as_before():
    declared = item("regimes", dataclasses.replace(GOOD, uses_regimes=False, regime_sharpes=None))
    assert declared.status == "na" and declared.note == "the strategy does not use market regimes"
    assert au.audit(result(), spec(), dataclasses.replace(GOOD, uses_regimes=False, regime_sharpes=None), CONFIG).verdict == "go"
    for flag in (None, True):
        assert item("regimes", dataclasses.replace(GOOD, uses_regimes=flag, regime_sharpes=None)).status == "unknown"
        assert item("regimes", dataclasses.replace(GOOD, uses_regimes=flag)) == item("regimes")
    one = dataclasses.replace(GOOD, regime_sharpes={"bull": 1.0, "bear": -0.5, "flat": -0.2}, uses_regimes=True)
    assert item("regimes", one).status == "fail"


# ---- codes of the red flags ------------------------------------------------------------------------------------------------------------

def test_every_cause_of_the_red_flags_check_starts_with_its_code_and_keeps_its_text():
    absurd = result(stats={"sharpe": QuantResult("metrics.sharpe", 3.5, "ratio", "sharpe")})
    assert item("red_flags", GOOD, CONFIG, absurd).note == "sharpe_high: annual Sharpe 3.50 is above 3.0"
    carried = result(trades=[trade("1000"), trade("20", day=8), trade("-10", day=9)])
    assert item("red_flags", GOOD, CONFIG, carried).note.startswith("top_trade: one trade carries 98%")
    degraded = dataclasses.replace(GOOD, is_sharpe=1.0, oos_sharpe=0.3)
    assert item("red_flags", degraded).note == "oos_ratio: out-of-sample Sharpe is 30% of in-sample; the minimum is 50%"
    both = item("red_flags", degraded, CONFIG, absurd)
    assert both.note.startswith("sharpe_high:") and "; oos_ratio:" in both.note
    assert item("red_flags").note == "no red flag found"


# ---- the one precedence ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("statuses, expected", [
    (("fail", "unknown", "warn", "pass", "na"), "no_go"), (("unknown", "warn", "pass"), "needs_evidence"), (("warn", "pass", "na"), "review"),
    (("pass", "na"), "go"), (("na",), "go"), ((), "go"), (["fail"], "no_go")])
def test_verdict_for_gives_the_precedence_no_go_needs_evidence_review_go(statuses, expected):
    assert au.verdict_for(statuses) == expected
    assert au.verdict_for(s for s in statuses) == expected                               # any iterable, a generator included


def test_the_audit_takes_its_verdict_from_verdict_for(monkeypatch):
    assert au.audit(result(), spec(), GOOD, CONFIG).verdict == "go"
    seen = []
    monkeypatch.setattr(au, "verdict_for", lambda statuses: (seen.append(sorted(set(statuses))), "review")[1])
    assert au.audit(result(), spec(), GOOD, CONFIG).verdict == "review"
    assert len(seen) == 1 and "pass" in seen[0]                                         # it was given the statuses of the 24 checks


def test_the_precedence_lives_in_one_place():
    found = {}
    for path in sorted(ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        hits = len(re.findall(r'"needs_evidence"\s+if\b', text)) + len(re.findall(r"'needs_evidence'\s+if\b", text))
        if hits:
            found[path.relative_to(ROOT).as_posix()] = hits
    assert found == {"factory/audit.py": 1}
    body = (ROOT / "factory" / "audit.py").read_text(encoding="utf-8")
    start = body.index("def verdict_for")
    assert body.index('"needs_evidence" if') > start and "verdict = verdict_for(" in body
