"""Strategy specs and their tests first (SPEC-020: SKL-002). Expected values are worked out by hand."""
from decimal import Decimal as D

import pytest
import yaml
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.factory import orders, spec
from hokma.factory.researcher import MissingTests, TestsFailed
from hokma.factory.spec import BadSpec, parse_spec, require_tests

SIGNALS = """
    - name: fast_above_slow_is_long
      facts: {ema_fast: [101, 102, 103], ema_slow: [100, 100, 100]}
      expect: long
    - name: fast_below_slow_is_short
      facts: {ema_fast: [99, 98, 97], ema_slow: [100, 100, 100]}
      expect: short
    - name: equal_averages_are_flat
      facts: {ema_fast: [100, 100, 100], ema_slow: [100, 100, 100]}
      expect: flat
    - name: a_missing_average_abstains
      facts: {ema_fast: [101, 102, 103]}
      expect: abstain
"""
SIZES = """
    - name: risk_500_on_a_5_point_stop
      contract_id: FUT:NQ:2025-03
      asset_type: future
      multiplier: 20
      kind: risk_usd
      value: 500
      price: 20000
      equity: 100000
      stop_distance: 5
      expect_qty: 5
    - name: risk_450_rounds_down_to_4
      contract_id: FUT:NQ:2025-03
      asset_type: future
      multiplier: 20
      kind: risk_usd
      value: 450
      price: 20000
      equity: 100000
      stop_distance: 5
      expect_qty: 4
    - name: ten_percent_of_100000_in_a_50_dollar_stock
      contract_id: EQ:XYZ
      asset_type: equity
      multiplier: 1
      kind: pct_equity
      value: 10
      price: 50
      equity: 100000
      expect_qty: 200
"""


def text(signals=SIGNALS, sizes=SIZES, strategy_id="ema_demo", extra="") -> str:
    tests = ("tests:\n" + (f"  signal:{signals}" if signals else "") + (f"  position_size:{sizes}" if sizes else "")) if (signals or sizes) else ""
    return f"strategy_id: {strategy_id}\nline: DT\nalpha: ema_cross\nparams: {{fast: 3, slow: 5}}\nhypothesis: A fast average above a slow one means the move continues.\n{extra}{tests}"


def test_a_spec_without_a_signal_test_or_without_a_size_test_cannot_go_to_backtest_SKL_002():
    for signals, sizes, named in ((SIGNALS, "", "position_size"), ("", SIZES, "signal"), ("", "", "signal")):
        with pytest.raises(MissingTests, match=named) as caught:
            require_tests(parse_spec(text(signals, sizes)))
        assert caught.value.code == "missing_tests"
    with pytest.raises(MissingTests, match="signal and no position_size"):
        require_tests(parse_spec(text("", "")))


def test_with_both_kinds_the_tests_run_and_the_hash_comes_back_SKL_002():
    parsed = parse_spec(text())
    assert require_tests(parsed) == parsed.spec_hash and len(parsed.spec_hash) == 64
    assert parse_spec(text()).spec_hash == parsed.spec_hash                        # the same text, the same hash
    assert parse_spec(text(strategy_id="other")).spec_hash != parsed.spec_hash


def test_the_expected_values_are_the_ones_worked_by_hand():
    # (103 - 100) / 100 / 0.01 = 3, clipped to 1: long. 500 / (5 points * 20 dollars) = 5. 450 / 100 = 4.5, rounded down to 4. 100000 * 10% / 50 = 200.
    parsed = parse_spec(text())
    cases = {c.name: spec._quantity(c) for c in parsed.size_tests}
    assert cases == {"risk_500_on_a_5_point_stop": D(5), "risk_450_rounds_down_to_4": D(4), "ten_percent_of_100000_in_a_50_dollar_stock": D(200)}
    assert [spec._view(parsed, c, spec.default_registry()) for c in parsed.signal_tests] == ["long", "short", "flat", "abstain"]


def test_a_failing_signal_test_stops_the_backtest_and_names_the_case_SKL_002():
    wrong = SIGNALS.replace("expect: long", "expect: short", 1)
    with pytest.raises(TestsFailed, match="fast_above_slow_is_long: expected short, got long") as caught:
        require_tests(parse_spec(text(wrong)))
    assert caught.value.code == "tests_failed"


def test_a_failing_size_test_stops_the_backtest_and_names_the_case_SKL_002():
    wrong = SIZES.replace("expect_qty: 4", "expect_qty: 5")
    with pytest.raises(TestsFailed, match="risk_450_rounds_down_to_4: expected 5, got 4"):
        require_tests(parse_spec(text(sizes=wrong)))


def test_a_test_that_cannot_run_is_a_failed_test_with_its_reason():
    no_stop = SIZES.replace("      stop_distance: 5\n      expect_qty: 5", "      expect_qty: 5")
    with pytest.raises(TestsFailed, match="risk_500_on_a_5_point_stop: raised BadSize"):
        require_tests(parse_spec(text(sizes=no_stop)))


def test_the_skill_example_is_a_spec_that_passes_its_own_tests():
    import re
    from pathlib import Path

    body = (Path(spec.__file__).resolve().parents[3] / "kit/template/service/skills/test-first-strategy/SKILL.md").read_text(encoding="utf-8")
    block = re.search(r"```yaml\n(.*?)```", body, re.S).group(1)
    parsed = parse_spec(block)
    assert parsed.strategy_id == "ema_cross_demo" and len(parsed.signal_tests) == 4 and len(parsed.size_tests) == 1
    assert require_tests(parsed) == parsed.spec_hash


@pytest.mark.parametrize("change, message", [
    ("alpha: nope_alpha", "no alpha or strategy"),
    ("params: {fast: 9, slow: 5}", "fast must be shorter"),
    ("params: {unknown_param: 1}", "unknown_param"),
    ("line: XX", "line is one of"),
    ("extra_key: 1", "a spec is a mapping"),
])
def test_a_spec_is_checked_strictly(change, message):
    key = change.split(":")[0]
    lines = [ln for ln in text().splitlines() if not ln.startswith(key + ":")]
    with pytest.raises(BadSpec, match=message):
        parse_spec("\n".join([change, *lines]))


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["tests"]["signal"][0].update({"expect": "up"}), "expect is one of"),
    (lambda d: d["tests"]["signal"][0].update({"script": "import os"}), "a signal test has the keys"),
    (lambda d: d["tests"]["signal"][0]["facts"].update({"ema_fast": [float("nan")]}), "finite numbers"),
    (lambda d: d["tests"]["position_size"][0].update({"kind": "lots_of_it"}), "unknown size type"),
    (lambda d: d["tests"]["position_size"][0].update({"asset_type": "spaceship"}), "asset_type is one of"),
    (lambda d: d["tests"]["position_size"][0].update({"multiplier": -1}), "above zero"),
    (lambda d: d["tests"].update({"other": []}), "tests has the keys"),
    (lambda d: d["tests"].update({"signal": [d["tests"]["signal"][0]] * 51}), "at most 50"),
])
def test_the_tests_are_data_and_are_checked_strictly(mutate, message):
    data = yaml.safe_load(text())
    mutate(data)
    with pytest.raises(BadSpec, match=message):
        parse_spec(yaml.safe_dump(data))


def test_text_that_is_not_a_spec_is_refused():
    for bad in ("", "[1, 2]", "strategy_id: [", "x" * (spec.MAX_SPEC_BYTES + 1)):
        with pytest.raises(BadSpec):
            parse_spec(bad)


# ---------------------------------------------------------------------------------------------------- the table
def test_drafts_get_the_next_version_and_the_same_text_is_not_saved_twice(db):
    first = spec.save_draft(text(), db=db)
    assert (first.version, first.status, first.line) == (1, "draft", "DT")
    assert spec.save_draft(text(), db=db).id == first.id
    changed = spec.save_draft(text().replace("slow: 5", "slow: 6"), db=db)
    assert (changed.version, changed.spec_hash != first.spec_hash) == (2, True)
    assert spec.load("ema_demo", db=db).version == 2 and spec.load("ema_demo", 1, db=db).spec_hash == first.spec_hash
    with pytest.raises(BadSpec, match="no saved spec"):
        spec.load("ghost", db=db)


def test_the_backtest_gate_runs_the_tests_of_the_saved_version(db):
    good = spec.save_draft(text(), db=db)
    assert spec.gate_backtest("ema_demo", good.version, db=db) == good.spec_hash
    bare = spec.save_draft(text("", "", strategy_id="bare"), db=db)
    with pytest.raises(MissingTests):
        spec.gate_backtest("bare", bare.version, db=db)
    wrong = spec.save_draft(text(SIGNALS.replace("expect: flat", "expect: long"), strategy_id="wrong"), db=db)
    with pytest.raises(TestsFailed):
        spec.gate_backtest("wrong", wrong.version, db=db)


def test_propose_moves_a_draft_to_proposed_with_a_fixed_proposer_and_nothing_else(db):
    row = spec.save_draft(text(), db=db)
    done = spec.propose("ema_demo", row.version, db=db)
    assert done.status == "proposed"
    with db.read() as u:
        stored = u.session.one("SELECT status, proposed_by FROM strategy_specs WHERE id = :i", {"i": row.id})
        everything = u.session.query("SELECT status FROM strategy_specs")
    assert stored == {"status": "proposed", "proposed_by": "strategy-researcher"} and everything == [{"status": "proposed"}]
    with pytest.raises(BadSpec, match="only a draft can be proposed"):
        spec.propose("ema_demo", row.version, db=db)
    with pytest.raises(BadSpec, match="no saved spec"):
        spec.propose("ema_demo", 9, db=db)


def test_a_spec_without_tests_or_with_failing_tests_is_never_proposed(db):
    bare = spec.save_draft(text("", "", strategy_id="bare"), db=db)
    with pytest.raises(MissingTests):
        spec.propose("bare", bare.version, db=db)
    wrong = spec.save_draft(text(SIGNALS.replace("expect: flat", "expect: long"), strategy_id="wrong"), db=db)
    with pytest.raises(TestsFailed):
        spec.propose("wrong", wrong.version, db=db)
    with db.read() as u:
        assert u.session.query("SELECT DISTINCT status FROM strategy_specs") == [{"status": "draft"}]


def test_the_orders_function_is_the_one_of_the_factory_not_a_second_copy():
    assert spec.to_contracts is orders.to_contracts


# ---------------------------------------------------------------------------------------------------- filters in the spec (SPEC-030, A1-3)
SIGNAL_ONE = "\n    - {name: up, facts: {ema_fast: [101, 102, 103], ema_slow: [100, 100, 100]}, expect: long}\n"
SIZE_ONE = ('\n    - {name: one, contract_id: "FUT:NQ:2025-03", asset_type: future, multiplier: 20, kind: contracts, value: 1, price: 100, '
            'equity: 100000, expect_qty: 1}\n')

# The hashes below were taken from the code before filters existed (the previous commit): a spec saved then must keep its hash.
OLD_HASHES = {
    None: "a1a7cd1cb4014b3dd89a2d050a6ab9f2381632ab983524c99aaecfcc71726d3b",
    "{sizer: {kind: fixed_contracts, n: 3}}": "1043e2507b64c49642b625722588058b162edc8fc92fe88f66c7c7b48fc1a966",
    "{sizer: {kind: fixed_risk, risk_pct: 1.0}, exit: {kind: atr, length: 14, stop_atr: 3.0, r_multiple: 2.0}, execution: {kind: twap, n_slices: 3}}":
        "e2b377e7ad14a324668fbcaa218611391a5bfb2195a55791d7a54b870c48eb1a",
}


def with_parts(parts):
    return text(SIGNAL_ONE, SIZE_ONE, extra="" if parts is None else f"parts: {parts}\n").replace("hypothesis: A fast average above a slow one means the move continues.",
                                                                                               "hypothesis: A fast average above a slow one.")


@pytest.mark.parametrize("parts", list(OLD_HASHES))
def test_the_hash_of_a_spec_saved_before_filters_existed_does_not_change(parts):
    assert parse_spec(with_parts(parts)).spec_hash == OLD_HASHES[parts]


def test_a_spec_with_no_filters_and_one_with_an_empty_list_hash_alike():
    plain = parse_spec(with_parts("{sizer: {kind: fixed_contracts, n: 3}}"))
    empty = parse_spec(with_parts("{sizer: {kind: fixed_contracts, n: 3}, filters: []}"))
    assert plain.spec_hash == empty.spec_hash == OLD_HASHES["{sizer: {kind: fixed_contracts, n: 3}}"] and "filters" not in empty.parts


SESSION = "{kind: session, start: '09:45', end: '11:30'}"


def test_a_spec_with_filters_is_checked_runs_its_tests_and_gets_another_hash():
    filtered = parse_spec(with_parts(f"{{sizer: {{kind: fixed_contracts, n: 3}}, filters: [{SESSION}, {{kind: max_trades_per_day, n: 2}}]}}"))
    assert [f["kind"] for f in filtered.parts["filters"]] == ["session", "max_trades_per_day"] and filtered.parts["filters"][0]["tz"] == "America/New_York"
    assert require_tests(filtered) == filtered.spec_hash != OLD_HASHES["{sizer: {kind: fixed_contracts, n: 3}}"]
    other = parse_spec(with_parts(f"{{sizer: {{kind: fixed_contracts, n: 3}}, filters: [{SESSION}, {{kind: max_trades_per_day, n: 3}}]}}"))
    assert other.spec_hash != filtered.spec_hash


def test_two_spellings_of_the_same_filters_hash_alike():
    a = parse_spec(with_parts(f"{{filters: [{SESSION}]}}"))
    b = parse_spec(with_parts("{filters: [{end: '11:30', start: '09:45', tz: America/New_York, days: [mon, tue, wed, thu, fri], kind: session}]}"))
    assert a.spec_hash == b.spec_hash


@pytest.mark.parametrize("filters", ["[{kind: session, start: '9:45', end: '11:30'}]", "[{kind: nothing}]", "[{kind: max_trades_per_day, n: 0}]",
                                     "[{kind: session, start: '09:45', end: '11:30', tz: Mars/Base}]", "session", "[{kind: regime_adx, length: 14}]"])
def test_a_bad_filter_is_a_bad_spec(filters):
    with pytest.raises(BadSpec):
        parse_spec(with_parts(f"{{filters: {filters}}}"))


def test_a_spec_with_filters_is_saved_as_a_draft(db):
    row = spec.save_draft(with_parts(f"{{filters: [{SESSION}]}}"), db=db)
    assert row.status == "draft" and spec.gate_backtest(row.strategy_id, row.version, db=db) == row.spec_hash
