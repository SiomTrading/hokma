"""The idea loader (SPEC-020): the files of kit/template/service/strategies/ideas/ read at run time, with the schema that test_library_a_ideas enforces."""
import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from hokma.factory import ideas
from hokma.factory.ideas import IdeaError, get_idea, idea_rows, load_ideas, parse_idea
from hokma.factory.params import ParamError
from hokma.factory.spec import default_registry
from tests.factory.test_library_a_ideas import EXPECTED, IDEAS, check_idea

NOTICES = Path(ideas.__file__).resolve().parents[3] / ".notices" / "THIRD_PARTY.txt"


@pytest.fixture(scope="module")
def registry():
    return default_registry()


@pytest.fixture(scope="module")
def refs():
    return set(re.findall(r"^- (idea-\d{3}):", NOTICES.read_text(encoding="utf-8"), flags=re.M))


def raw(stem="fx"):
    return yaml.safe_load((IDEAS / f"{stem}.yaml").read_text(encoding="utf-8"))


def test_the_loader_reads_the_eleven_files_of_the_library(registry):
    loaded = load_ideas(registry=registry)
    assert len(loaded) == len(EXPECTED) == 11
    by_file = {i.source[:-5]: i for i in loaded}
    assert set(by_file) == set(EXPECTED)
    for stem, (lines, premium, events) in EXPECTED.items():
        idea = by_file[stem]
        assert (set(idea.lines), idea.requires_premium_switch, idea.requires_event_study) == (lines, premium, events)
        assert {v.line for v in idea.variants} == lines and idea.ref.startswith("idea-")


def test_the_loader_and_the_schema_check_of_the_library_agree_on_every_mutation(registry, refs):
    """A file that the schema test accepts the loader accepts, and one it refuses the loader refuses (the loader does not read the notices file)."""
    def mutations(data):
        for key in ("id", "title", "ref", "lines", "variants", "needs"):
            yield f"without {key}", {k: v for k, v in data.items() if k != key}
        yield "extra key", {**data, "extra": 1}
        yield "unknown line", {**data, "lines": ["SWC", "XX"]}
        yield "bad id", {**data, "id": "Bad Id"}
        yield "flag is text", {**data, "requires_premium_switch": "no"}
        yield "short summary", {**data, "summary": "tiny"}
        yield "a variant with a bad rebalance", {**data, "variants": [{**data["variants"][0], "rebalance": "hourly"}, *data["variants"][1:]]}
        yield "a line without a variant", {**data, "lines": [*data["lines"], "DT"]}
        yield "alpha and planned alpha", {**data, "variants": [{**data["variants"][0], "alpha": "tsmom", "planned_alpha": "rate_rank"}, *data["variants"][1:]]}
        yield "params without an alpha", {**data, "variants": [{**data["variants"][0], "params": {"x": 1}}, *data["variants"][1:]]}
        yield "an alpha that does not exist", {**data, "variants": [{**data["variants"][0], "alpha": "no_such_alpha"}, *data["variants"][1:]]}

    for stem in EXPECTED:
        good = raw(stem)
        check_idea(copy.deepcopy(good), stem, registry, refs)
        parse_idea(yaml.safe_dump(good), stem, registry)
        for label, bad in mutations(good):
            with pytest.raises((AssertionError, KeyError, ParamError)):                 # the schema test fails in its own way for each mutation
                check_idea(copy.deepcopy(bad), stem, registry, refs)
            with pytest.raises(IdeaError, match=stem):
                parse_idea(yaml.safe_dump(bad), stem, registry)


def test_parameters_that_the_alpha_refuses_are_an_error_and_a_planned_alpha_is_not(registry):
    data = raw("time-series-momentum")
    data["variants"][0]["params"] = {"periods": 1}
    with pytest.raises(IdeaError, match="periods"):
        parse_idea(yaml.safe_dump(data), "t.yaml", registry)
    parse_idea(yaml.safe_dump(raw("fx")), "fx.yaml", registry)                     # planned_alpha rate_rank is not registered, and that is fine
    parse_idea(yaml.safe_dump(data), "t.yaml")                                      # without a registry the alphas are not looked up


def test_files_that_are_not_ideas_are_refused(tmp_path):
    for name, body in (("a.yaml", "[1, 2]"), ("b.yaml", "id: [unclosed"), ("c.yaml", "x" * (ideas.MAX_FILE_BYTES + 1))):
        (tmp_path / name).write_text(body, encoding="utf-8")
        with pytest.raises(IdeaError, match=name):
            load_ideas(tmp_path)
        (tmp_path / name).unlink()
    (tmp_path / "one.yaml").write_text((IDEAS / "fx.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "two.yaml").write_text((IDEAS / "fx.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(IdeaError, match="repeated id"):
        load_ideas(tmp_path)
    assert IdeaError("f.yaml", "m").code == "bad_idea"


def test_the_ideas_are_shown_as_rows_and_as_a_full_description(registry):
    rows = idea_rows(registry=registry)
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows) and len(rows) == 11 and all(r["kind"] == "idea" for r in rows)
    swo = idea_rows(line="SWO", registry=registry)
    assert {r["id"] for r in swo} == {"calendar_effects", "volatility_risk_premium", "equity_factors"}
    with pytest.raises(ValueError, match="line is one of"):
        idea_rows(line="XX")
    full = get_idea("fx", registry=registry).to_dict()
    assert json.loads(json.dumps(full))["id"] == "fx" and full["variants"] == 5 and len(full["variant_list"]) == 5
    assert full["variant_list"][2]["alpha"] == "historical_returns" and full["variant_list"][2]["params"] == {"periods": 252, "cap": 0.15}
    assert "planned_alpha" in full["variant_list"][0] and "alpha" not in full["variant_list"][0] and "costs" in full["variant_list"][0]
    with pytest.raises(KeyError, match="no idea"):
        get_idea("ghost")


def test_the_loader_takes_no_path_from_an_agent():
    import inspect

    assert str(ideas.default_folder()).endswith("kit/template/service/strategies/ideas")
    assert list(inspect.signature(idea_rows).parameters)[0] == "folder"          # an internal argument: the tools of the kit never pass it
