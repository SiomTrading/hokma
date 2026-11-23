"""Spec loading (AH-27): a valid strategy loads; unknown field or no universe fails with a readable error."""
import pytest

from hokma.desks.core.spec_loader import SpecError, load_spec, load_strategy

STRATEGY = """
name: quality-compounders
line: LP
horizon: years
universe: [aapl, msft, AAPL]
models:
  - name: A16
    weight: 2
  - name: A17
blend:
  method: equal
benchmark: spy
"""


def write(tmp_path, text, name="s.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_valid_strategy_loads_AH_27(tmp_path):
    spec = load_strategy(write(tmp_path, STRATEGY))
    assert spec.universe == ["AAPL", "MSFT"] and spec.benchmark == "SPY"
    assert spec.model_weights == {"A16": 2.0, "A17": 1.0} and spec.blend.method == "equal"
    assert spec.title == "Quality Compounders" and spec.line == "LP"


def test_unknown_field_fails_with_a_readable_error_AH_27(tmp_path):
    with pytest.raises(SpecError, match=r"position_size: unknown field"):
        load_strategy(write(tmp_path, STRATEGY + "position_size: 3\n"))
    with pytest.raises(SpecError, match=r"models\.0\.stop: unknown field"):
        load_strategy(write(tmp_path, STRATEGY.replace("weight: 2", "weight: 2\n    stop: 5")))


@pytest.mark.parametrize("universe", ["universe: []", ""])
def test_missing_or_empty_universe_fails_AH_27(tmp_path, universe):
    text = "\n".join(l for l in STRATEGY.splitlines() if not l.startswith("universe")) + "\n" + universe + "\n"
    with pytest.raises(SpecError, match="universe"):
        load_strategy(write(tmp_path, text))


@pytest.mark.parametrize("old,new,word", [("line: LP", "line: XX", "line"), ("horizon: years", "horizon: forever", "horizon"),
                                         ("name: quality-compounders", "name: Bad Name", "name"), ("weight: 2", "weight: 0", "weight"),
                                         ("[aapl, msft, AAPL]", "[aapl, 'bad symbol!']", "universe")])
def test_bad_values_name_the_field_AH_27(tmp_path, old, new, word):
    with pytest.raises(SpecError, match=word):
        load_strategy(write(tmp_path, STRATEGY.replace(old, new)))


def test_duplicate_models_and_bad_files_fail_AH_27(tmp_path):
    with pytest.raises(SpecError, match="duplicate"):
        load_strategy(write(tmp_path, STRATEGY.replace("name: A17", "name: A16")))
    with pytest.raises(SpecError, match="cannot read"):
        load_strategy(tmp_path / "nope.yaml")
    with pytest.raises(SpecError, match="not valid YAML"):
        load_strategy(write(tmp_path, "a: [1, 2"))
    with pytest.raises(SpecError, match="mapping"):
        load_strategy(write(tmp_path, "- 1\n- 2\n"))


def test_desk_spec_loads_and_checks_AH_27(tmp_path):
    body = "name: desk-one\ncapital: 25000\nrebalance: daily\nstrategies:\n" + "".join("  " + l + "\n" for l in
        ["- name: quality-compounders", "  line: LP", "  horizon: years", "  universe: [AAPL]", "  models:", "    - name: A16"])
    spec = load_spec(write(tmp_path, body))
    assert spec.capital == 25000 and spec.rebalance == "daily" and spec.benchmark == "SPY"
    with pytest.raises(SpecError, match="capital"):
        load_spec(write(tmp_path, body.replace("capital: 25000", "capital: -1")))
    with pytest.raises(SpecError, match="required field is missing"):
        load_spec(write(tmp_path, body.replace("capital: 25000\n", "")))
    with pytest.raises(SpecError, match="duplicate strategy"):
        load_spec(write(tmp_path, body + body.split("strategies:\n")[1]))
