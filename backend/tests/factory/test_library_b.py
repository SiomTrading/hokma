"""AS-019 and AS-023: the library of rewritten strategy rules and the script that tabulates the strategies.

The library holds one YAML rule per source strategy file that was reviewed (61 files). A file that could not be rewritten needs a line in
library/SKIPPED.md with its reason; the count of YAML files plus those lines must equal the number of source files.
"""
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

from hokma.desks.core.signal import LINES
from hokma.factory.registry import EDGE_CLASSES

ROOT = Path(__file__).resolve().parents[3]
STRATEGIES = ROOT / "kit" / "template" / "service" / "strategies"
LIBRARY = STRATEGIES / "library"
SOURCE_FILES = 61

REQUIRED = ["id", "name", "ref", "asset_class", "lines", "horizon", "rebalance", "universe", "signal", "entry", "exit", "sizing", "risk_note"]
FLAGS = ["requires_premium_switch", "requires_event_study"]
TEXT = ["name", "horizon", "rebalance", "universe", "signal", "entry", "exit", "sizing", "risk_note"]
MAX_TEXT = 600
SKIPPED_LINE = re.compile(r"^- (\S+): (\S.*)$")

# Rules that sell option premium must say so; rules that are a pure calendar effect must say they need an event study.
PREMIUM = {"index_put_sold_stock_puts_bought", "straddle_sale_with_crash_put"}
CALENDAR = {"january_return_gate", "expiry_week_equity_exposure", "mid_month_pay_cycle_day", "club_match_day_short", "month_turn_hold"}


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rules = load("check_repo_rules", "scripts/check_repo_rules.py")
build = load("factory_build_table", "scripts/factory_build_table.py")


def files() -> list[Path]:
    return sorted(LIBRARY.glob("*.yaml"))


def rule(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path.name
    return data


def skipped() -> list[str]:
    text = (LIBRARY / "SKIPPED.md").read_text(encoding="utf-8")
    return [m.group(1) for line in text.splitlines() if (m := SKIPPED_LINE.match(line))]


def test_every_source_file_has_a_rule_or_a_reason():
    names = [p.stem for p in files()]
    assert len(names) == len(set(names))
    assert not set(names) & set(skipped()), "a skipped file also has a rule"
    assert len(files()) + len(skipped()) == SOURCE_FILES


def test_each_rule_passes_the_schema():
    notices = (ROOT / ".notices" / "THIRD_PARTY.txt").read_text(encoding="utf-8")
    for path in files():
        data = rule(path)
        assert list(data) == REQUIRED + [k for k in FLAGS if k in data], path.name
        assert re.fullmatch(r"[a-z][a-z0-9_]{1,63}", data["id"]) and path.stem == data["id"], path.name
        assert re.fullmatch(r"LIB-\d{3}", data["ref"]), path.name
        assert f"- {data['ref']}: " in notices, f"{path.name}: {data['ref']} is not registered in .notices/THIRD_PARTY.txt"
        assert data["asset_class"] in EDGE_CLASSES, path.name
        assert isinstance(data["lines"], list) and data["lines"] and set(data["lines"]) <= set(LINES), path.name
        assert len(set(data["lines"])) == len(data["lines"]), path.name
        for key in TEXT:
            value = data[key]
            assert isinstance(value, str) and (4 if key in ("horizon", "rebalance") else 10) <= len(value) <= MAX_TEXT, f"{path.name}: {key}"
            assert not re.search(r"[<>&\x00-\x1f]", value), f"{path.name}: {key} has markup characters"
        for flag in FLAGS:
            assert data.get(flag, True) is True, f"{path.name}: {flag} is only ever true"


def test_ids_and_refs_are_unique_and_refs_are_consecutive():
    data = [rule(p) for p in files()]
    assert len({d["id"] for d in data}) == len(data)
    refs = sorted(d["ref"] for d in data)
    assert len(set(refs)) == len(refs)
    assert refs == [f"LIB-{n:03d}" for n in range(1, SOURCE_FILES + 1)]


def test_premium_and_calendar_rules_declare_their_conditions():
    by_id = {d["id"]: d for d in (rule(p) for p in files())}
    assert PREMIUM <= set(by_id) and CALENDAR <= set(by_id)
    for rule_id in PREMIUM:
        assert by_id[rule_id].get("requires_premium_switch") is True and "SWO" in by_id[rule_id]["lines"], rule_id
    for rule_id in CALENDAR:
        assert by_id[rule_id].get("requires_event_study") is True, rule_id
    flagged_premium = {i for i, d in by_id.items() if d.get("requires_premium_switch")}
    assert flagged_premium == PREMIUM


def test_every_rule_file_passes_the_repository_rules():
    relative = [p.relative_to(ROOT).as_posix() for p in files()] + [(LIBRARY / "SKIPPED.md").relative_to(ROOT).as_posix()]
    assert rules.check(ROOT, files=relative) == []


def test_the_library_is_not_loaded_by_the_registry_folder_glob():
    # Registry.load_dir reads only the YAML files directly inside the strategies folder, so the rules of the library are data, not registered entries.
    direct = {p.name for p in STRATEGIES.glob("*.yaml")}
    assert not direct & {p.name for p in files()}


def test_the_table_script_lists_every_library_rule():
    table = build.build_table(ROOT)
    rows = [line for line in table.splitlines() if line.startswith("| library |")]
    assert len(rows) == len(files())
    assert build.build_table(ROOT) == table, "the output must be the same for the same files"


def test_the_table_script_on_a_small_tree_gives_the_hand_written_table(tmp_path):
    (tmp_path / "kit/template/service/strategies/library").mkdir(parents=True)
    (tmp_path / "backend/hokma/factory/data").mkdir(parents=True)
    (tmp_path / "kit/template/service/strategies/library/b_rule.yaml").write_text(
        "id: b_rule\nname: Second | rule\nasset_class: bonds\nlines: [RF, LP]\n", encoding="utf-8")
    (tmp_path / "kit/template/service/strategies/a_entry.yaml").write_text(
        "- id: a_entry\n  kind: alpha\n  line: DT\n  description: Fast <cross>\n", encoding="utf-8")
    (tmp_path / "backend/hokma/factory/data/literature_edges.csv").write_text(
        "edge_id,asset_class,idea,sharpe_reported,sharpe_basis,t_stat,years_tested,ref\n"
        "E-001,bonds,Idea low,0.40,gross,2.5,40,LIT-001\n"
        "E-002,bonds,Idea high,0.90,gross,5.0,30,LIT-002\n"
        "E-003,crypto,Idea crypto,1.00,gross,3.0,10,LIT-003\n", encoding="utf-8")
    expected = "\n".join([
        "# Strategies of the factory", "",
        "| Folder | Id | Lines | Asset class | What it is |", "|---|---|---|---|---|",
        "| library | b_rule | RF, LP | bonds | Second / rule |",
        "| registered | a_entry | DT |  | Fast &lt;cross&gt; |", "",
        "# Published edge ideas (t statistic at or above 3)", "",
        "The Sharpe ratio is the gross figure the source reports, before any trading cost. No net value is shown or derived.", "",
        "## bonds", "", "| Idea | Sharpe (gross) | t statistic | Years tested | Ref |", "|---|---|---|---|---|",
        "| Idea high | 0.90 gross | 5.0 | 30 | LIT-002 |", "",
        "## crypto", "", "| Idea | Sharpe (gross) | t statistic | Years tested | Ref |", "|---|---|---|---|---|",
        "| Idea crypto | 1.00 gross | 3.0 | 10 | LIT-003 |", ""])
    assert build.build_table(tmp_path, min_t=3.0) == expected


def test_the_table_script_writes_a_file_only_when_asked(tmp_path, capsys):
    out = tmp_path / "table.md"
    assert build.main(["--min-t", "3", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# Strategies of the factory")
    assert capsys.readouterr().out == ""
    assert build.main(["--min-t", "3"]) == 0
    assert capsys.readouterr().out.startswith("# Strategies of the factory")


@pytest.mark.parametrize("line,expected", [("- a_stem: it needs vendor data", True), ("- a_stem:", False), ("a_stem: reason", False), ("- none", False)])
def test_skipped_lines_need_a_reason(line, expected):
    assert bool(SKIPPED_LINE.match(line)) is expected
