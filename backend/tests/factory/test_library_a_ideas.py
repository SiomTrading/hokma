"""Idea recipes of the library, block A (SPEC-020: AS-020a to AS-020k, CC-16): schema, notice refs and no copied text."""
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

from hokma.desks.core.signal import LINES
from hokma.factory.params import validate
from hokma.factory.registry import Registry

ROOT = Path(__file__).resolve().parents[3]
STRATEGIES = ROOT / "kit" / "template" / "service" / "strategies"
IDEAS = STRATEGIES / "ideas"
NOTICES = ROOT / ".notices" / "THIRD_PARTY.txt"
SOURCES = Path("/home/cristianibanezg/projects/aria-assistant/references-trading")      # read only copy of the idea sources, outside the repo

REQUIRED = {"id", "title", "ref", "lines", "asset_classes", "requires_premium_switch", "requires_event_study", "summary", "needs", "data", "variants"}
OPTIONAL = {"context_only", "study_only"}
VARIANT_KEYS = {"line", "sleeve", "vehicle", "alpha", "planned_alpha", "params", "rebalance", "rules", "costs"}
REBALANCE = {"daily", "weekly", "monthly", "quarterly", "yearly"}
EXPECTED = {   # file: (lines, requires_premium_switch, requires_event_study)
    "time-series-momentum": ({"SWC", "LP"}, False, False),
    "asset-rotation": ({"LP", "RF"}, False, False),
    "commodities": ({"SWC"}, False, False),
    "calendar-effects": ({"SWC", "SWO"}, False, True),
    "volatility-risk-premium": ({"SWO"}, True, False),
    "fx": ({"SWC", "RF"}, False, False),
    "pairs-etf": ({"SWC", "LP"}, False, False),
    "macro-context": ({"LP", "SWC"}, False, False),
    "value-momentum": ({"LP"}, False, False),
    "crypto": ({"LP"}, False, False),
    "equity-factors": ({"LP", "SWO"}, False, True),
}


def load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name}: an idea file is one mapping"
    return data


def check_idea(data: dict, name: str, registry: Registry, refs: set[str]) -> None:
    """The schema of an idea file. Every failure names the file."""
    keys = set(data)
    assert REQUIRED <= keys <= REQUIRED | OPTIONAL, f"{name}: keys {sorted(keys ^ REQUIRED)}"
    assert re.fullmatch(r"[a-z][a-z0-9_]{1,63}", data["id"]), name
    assert isinstance(data["ref"], str) and data["ref"] in refs, f"{name}: ref {data['ref']!r} is not a line of .notices/THIRD_PARTY.txt"
    assert data["lines"] and set(data["lines"]) <= set(LINES), name
    assert isinstance(data["requires_premium_switch"], bool) and isinstance(data["requires_event_study"], bool), name
    for key in ("title", "summary"):
        assert isinstance(data[key], str) and 10 < len(data[key]) < 1200, f"{name}: {key}"
    for key in ("asset_classes", "needs", "data"):
        assert isinstance(data[key], list) and all(isinstance(x, str) for x in data[key]), f"{name}: {key}"
    assert data["variants"], name
    covered = set()
    for variant in data["variants"]:
        assert set(variant) <= VARIANT_KEYS and {"line", "vehicle", "rebalance", "rules"} <= set(variant), f"{name}: {sorted(variant)}"
        assert variant["line"] in data["lines"], name
        covered.add(variant["line"])
        assert variant["rebalance"] in REBALANCE and variant["rules"] and all(isinstance(r, str) for r in variant["rules"]), name
        assert not ("alpha" in variant and "planned_alpha" in variant), f"{name}: a variant names an alpha or a planned alpha, not both"
        if "alpha" in variant:
            entry = registry.get(variant["alpha"])                       # an alpha that exists in the registry
            validate(entry.cls.SCHEMA, {**entry.params, **variant.get("params", {})})   # with parameters its schema accepts
        else:
            assert "params" not in variant, f"{name}: parameters need an alpha that exists"
        if "planned_alpha" in variant:
            assert re.fullmatch(r"[a-z][a-z0-9_]{1,47}", variant["planned_alpha"]), name
            with pytest.raises(KeyError):
                registry.get(variant["planned_alpha"])                   # not registered yet: it is a criterion to be built
    assert covered == set(data["lines"]), f"{name}: every declared line has a variant"


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = Registry.with_builtins()
    reg.load_dir(STRATEGIES)
    return reg


@pytest.fixture(scope="module")
def refs() -> set[str]:
    return set(re.findall(r"^- (idea-\d{3}):", NOTICES.read_text(encoding="utf-8"), flags=re.M))


def test_the_folder_holds_the_eleven_ideas_and_each_passes_the_schema(registry, refs):
    files = sorted(p.stem for p in IDEAS.glob("*.yaml"))
    assert {"time-series-momentum", "asset-rotation", "commodities", "calendar-effects", "volatility-risk-premium", "fx", "pairs-etf",
            "macro-context", "value-momentum", "crypto", "equity-factors"} <= set(files)
    for path in sorted(IDEAS.glob("*.yaml")):
        check_idea(load(path), path.name, registry, refs)


def test_every_idea_file_has_a_ref_that_exists_in_the_notices(refs):
    """CC-16: a file without ref fails here, and so does a ref that is not a line of the notices file."""
    seen = []
    for path in sorted(IDEAS.glob("*.yaml")):
        data = load(path)
        assert "ref" in data, f"{path.name} has no ref"
        assert data["ref"] in refs, f"{path.name}: {data['ref']} is not in .notices/THIRD_PARTY.txt"
        seen.append(data["ref"])
    assert len(seen) == len(set(seen)), "two idea files share one ref"


def test_a_file_without_ref_or_with_an_unknown_ref_is_rejected(registry, refs):
    data = load(IDEAS / "fx.yaml")
    with pytest.raises(AssertionError, match="keys"):
        check_idea({k: v for k, v in data.items() if k != "ref"}, "fx.yaml", registry, refs)
    with pytest.raises(AssertionError, match="THIRD_PARTY"):
        check_idea({**data, "ref": "idea-999"}, "fx.yaml", registry, refs)
    with pytest.raises(AssertionError):
        check_idea({**data, "extra": 1}, "fx.yaml", registry, refs)


@pytest.mark.parametrize("stem", sorted(EXPECTED))
def test_lines_and_flags_of_each_idea(stem):
    lines, premium, events = EXPECTED[stem]
    data = load(IDEAS / f"{stem}.yaml")
    assert set(data["lines"]) == lines
    assert (data["requires_premium_switch"], data["requires_event_study"]) == (premium, events)


def test_time_series_momentum_has_a_recipe_for_swc_and_one_for_lp_on_the_tsmom_alpha(registry):
    data = load(IDEAS / "time-series-momentum.yaml")
    by_line = {v["line"]: v for v in data["variants"]}
    assert set(by_line) == {"SWC", "LP"} and all(v["alpha"] == "tsmom" for v in by_line.values())
    assert "futures" in by_line["SWC"]["vehicle"] and "ETF" in by_line["LP"]["vehicle"]
    alpha = registry.build("tsmom", by_line["SWC"]["params"])
    assert (alpha.periods, alpha.target_vol) == (252, 0.10)
    assert any("costs.cfd_swap" in c for c in by_line["SWC"]["costs"])


def test_other_idea_details():
    rotation = load(IDEAS / "asset-rotation.yaml")
    assert {v["line"] for v in rotation["variants"]} == {"LP", "RF"} and any("bond" in v["sleeve"] for v in rotation["variants"])
    assert "quant.stats.carry" in load(IDEAS / "commodities.yaml")["needs"]
    assert "quant.stats.events" in load(IDEAS / "calendar-effects.yaml")["needs"]
    vrp = load(IDEAS / "volatility-risk-premium.yaml")
    assert vrp["requires_premium_switch"] is True and any(v.get("alpha") == "premium_wheel_swo" for v in vrp["variants"])
    fx = load(IDEAS / "fx.yaml")
    assert any("costs.cfd_swap" in r for v in fx["variants"] for r in v["rules"] + v.get("costs", []))
    assert any(v.get("alpha") == "pairs_correlation" for v in load(IDEAS / "pairs-etf.yaml")["variants"])
    macro = load(IDEAS / "macro-context.yaml")
    assert macro["context_only"] is True and any("hokma.data.macro" in r and "vintage" in r for v in macro["variants"] for r in v["rules"])
    assert "quant.fundamentals" in load(IDEAS / "value-momentum.yaml")["needs"]
    crypto = load(IDEAS / "crypto.yaml")
    assert {v["line"] for v in crypto["variants"]} == {"LP"} and crypto["study_only"] and "hour" in crypto["study_only"][0]
    assert "quant.fundamentals" in load(IDEAS / "equity-factors.yaml")["needs"]


def test_idea_files_follow_the_repository_rules():
    spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    for path in IDEAS.glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert not rules.origin_hits(text) and not rules.spanish_hits(text) and not rules.engine_hits(text), path.name
        assert "\u2014" not in text


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def shingles(items: list[str], n: int = 7) -> set[tuple[str, ...]]:
    return {tuple(items[i:i + n]) for i in range(len(items) - n + 1)}


def source_folders() -> list[Path]:
    return [*SOURCES.glob("*systematic*"), *SOURCES.glob("*/hedge_fund/strategies")]


@pytest.mark.skipif(not SOURCES.exists() or not source_folders(), reason="the read only idea sources are not on this machine")
def test_no_text_is_copied_from_the_sources():
    """No run of seven words of an idea file, or of a strategy file, appears in any source text of the ideas."""
    source_text = []
    for base in source_folders():
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".py", ".yaml"} and ".git" not in path.parts:
                source_text.append(path.read_text(encoding="utf-8", errors="ignore"))
    pool: set[tuple[str, ...]] = set()
    for text in source_text:
        pool |= shingles(words(text))
    mine = list(IDEAS.glob("*.yaml")) + [STRATEGIES / n for n in ("deep-value.yaml", "fundamental-long-short.yaml", "inflections.yaml",
                                                                    "premium-wheel.yaml", "intraday-bot-skeleton.yaml")]
    for path in mine:
        common = shingles(words(path.read_text(encoding="utf-8"))) & pool
        assert not common, f"{path.name} shares a run of words with a source: {sorted(common)[:2]}"
