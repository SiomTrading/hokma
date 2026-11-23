"""Charts, the 13F report, stable markdown and the report tree (SPEC-018: AS-016, ED-004, TA-38, TA-83, TA-85)."""
import dataclasses
import hashlib
import pathlib
from datetime import date
from decimal import Decimal as D
from html.parser import HTMLParser

import pytest
from backoffice_support import db, desks_db, env, ready  # noqa: F401
from tests.data.sec.support import DownTransport, RecordedTransport, make_client

from hokma.backoffice.reports import charts, holdings13f, markdown, tree
from hokma.desks.core import paths
from hokma.desks.core.state import make_report

ROOT = pathlib.Path(__file__).resolve().parents[3]

EQUITY = [("2026-03-02", "100000"), ("2026-03-03", "101000"), ("2026-03-04", "99000"), ("2026-03-05", "102000")]
COMPOSITION = {"EQ:AAPL": D(18900), "ETF:SPY": D(-10100), "OPT:SPY:2026-12-18:C:600": D(650), "FUT:NQ:2026-12": D(-402400)}


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs = set(), set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.attrs |= {a for a, _ in attrs}


# ----------------------------------------------------------------------------------------------------- AS-016

def test_the_drawdown_is_the_drop_from_the_highest_value_so_far_AS_016():
    values = [D(v) for _d, v in EQUITY]
    assert charts.drawdown(values) == [D(0), D(0), D(99000) / D(101000) - 1, D(0)]       # 99000 against the peak of 101000


def test_the_charts_are_one_html_file_with_nothing_loaded_from_outside_AS_016():
    page = charts.build("Desk 3: equity", EQUITY, COMPOSITION)
    assert page.startswith("<!doctype html>") and page.count("<svg") == 3 and "Equity curve" in page and "Drawdown" in page and "Composition" in page
    parser = Tags()
    parser.feed(page)
    assert parser.tags <= {"html", "head", "meta", "title", "style", "body", "h1", "h2", "svg", "line", "polyline", "polygon", "text", "rect", "table", "tr", "td", "th", "p"}
    assert not parser.attrs & {"src", "href", "srcset", "data", "action", "poster", "xlink:href"}
    for outside in ("http://", "https://", "//cdn", "<script", "<link", "<img", "<iframe", "@import", "url(", "javascript:"):
        assert outside not in page
    assert charts.build("Desk 3: equity", EQUITY, COMPOSITION) == page                      # the same data gives the same page


def test_the_composition_shares_are_computed_from_the_values_AS_016():
    page = charts.composition_table(COMPOSITION)
    total = D(18900 + 10100 + 650 + 402400)
    assert f"{(D(402400) / total * 100).quantize(D('0.01'))}%" in page and "93.14%" in page
    assert "<td>short</td><td>-402400.00</td>" in page and "<td>long</td><td>18900.00</td>" in page
    assert page.index("FUT:NQ:2026-12") < page.index("EQ:AAPL") < page.index("ETF:SPY")            # biggest first


def test_the_charts_escape_what_comes_from_data_AS_016():
    page = charts.build('A & B <img src=x onerror=alert(1)>', EQUITY, {"<script>alert(1)</script>": D(5), 'a"b': D(1)})
    assert "<script>" not in page and "<img" not in page and "&lt;script&gt;" in page and "A &amp; B &lt;img" in page
    parser = Tags()
    parser.feed(page)
    assert "onerror" not in parser.attrs


def test_the_charts_refuse_data_they_cannot_draw_AS_016():
    with pytest.raises(charts.ChartError):
        charts.equity_svg([("2026-03-02", "1")])
    with pytest.raises(charts.ChartError):
        charts.composition_table({"A": D(0)})
    with pytest.raises(charts.ChartError):
        charts.composition_table({f"S{i}": D(1) for i in range(charts.MAX_SLICES + 1)})
    with pytest.raises(charts.ChartError):
        charts.equity_svg([("d", "1")] * (charts.MAX_POINTS + 1))
    with pytest.raises(Exception):
        charts.equity_svg([("2026-03-02", "1"), ("2026-03-03", "abc")])


def test_the_origin_of_the_idea_is_cited_only_in_the_notices_file_AS_016():
    notices = (ROOT / ".notices" / "THIRD_PARTY.txt").read_text(encoding="utf-8")
    assert "written from scratch" in notices and "no code, data or asset" in notices
    code = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "backend" / "hokma" / "backoffice").rglob("*.py")).lower()
    assert not any(name in code for name in ("perspective", "d-tale", "btplotting"))


# ----------------------------------------------------------------------------------------------------- ED-004

def test_the_13f_report_has_positions_comparison_history_and_a_sparkline_without_network_ED_004():
    client = make_client()
    out = holdings13f.build(client, 1000010, as_of=date(2022, 3, 1), quarters=2, unit="thousands")
    assert out.status == "ok"
    page = out.html
    assert "HARBOR CAPITAL MANAGEMENT LLC" in page and "Quarter 2021-12-31" in page and "4 positions" in page and "46,742,000" in page
    assert "<td>APPLE INC</td><td>AAPL</td>" in page and "21,308,000" in page                 # 21308 thousands
    assert "Change against 2021-09-30" in page and "INCREASED" in page and "20.0%" in page and "CLOSED" in page and "NEW" in page and ">new<" in page
    assert "<h2>History</h2>" in page and "2021-09-30" in page and page.count("<svg") == 1 and "<polyline" in page
    assert isinstance(client.transport, RecordedTransport) and {c[0] for c in client.transport.calls} <= {"submissions", "submission_text", "company_tickers"}


def test_the_13f_report_escapes_the_filing_text_and_prints_the_unit_warning_ED_004():
    client = make_client()
    from hokma.data.sec import thirteenf

    got = thirteenf.holdings_history(client, 1000010, as_of=date(2022, 3, 1), quarters=2)               # no unit given: ambiguous, with a warning
    latest = got.value[0]
    evil = dataclasses.replace(latest.holdings[0], issuer='<script>x</script> & Co')
    page = holdings13f.render([dataclasses.replace(latest, holdings=(evil, *latest.holdings[1:]))], warnings=got.warnings)
    assert "<script>" not in page and "&lt;script&gt;x&lt;/script&gt; &amp; Co" in page
    assert 'class="warning"' in page and "ambiguous" in page and page.count("<svg") == 0         # one quarter has no line to draw
    assert "Change against" not in page


def test_a_manager_with_no_report_is_no_data_and_a_broken_source_is_unavailable_ED_004():
    none = holdings13f.build(make_client(), 1000010, as_of=date(2021, 11, 1))
    assert none.status == "no_data" and none.html == ""
    down = holdings13f.build(make_client(DownTransport()), 1000010, as_of=date(2022, 3, 1))
    assert down.status == "data_unavailable" and down.html == ""
    with pytest.raises(ValueError):
        holdings13f.render([])


# ----------------------------------------------------------------------------------------------------- TA-38

def test_the_markdown_has_fixed_headers_and_names_the_field_that_is_absent_TA_38():
    body = markdown.render_proposal({"action": "buy", "reasoning": "Why <b>this</b> & that", "entry_price": D("21000.250"), "stop_loss": None})
    assert body == ("**Action**: buy\n\n**Reasoning**: Why &lt;b&gt;this&lt;/b&gt; &amp; that\n\n**Entry price**: 21000.25\n\n"
                    "**Stop loss**: not provided\n\n**Position sizing**: not provided\n")
    empty = markdown.render_decision({})
    assert [line for line in empty.splitlines() if line] == [f"**{f.label}**: not provided" for f in markdown.DECISION_FIELDS]
    blank = markdown.render_proposal({"action": "  ", "reasoning": [], "entry_price": {}})
    assert blank.count("not provided") == 5


def test_two_renders_of_the_same_record_are_the_same_bytes_TA_38():
    one = {"desk_id": 3, "kind": "cycle", "status": "done", "outputs": {"b": D("1.50"), "a": [1, 2]}, "order_ids": ["S1", "S2"], "timings": None}
    other = {"timings": None, "order_ids": ["S1", "S2"], "outputs": {"a": [1, 2], "b": D("1.5")}, "status": "done", "kind": "cycle", "desk_id": 3}
    a, b = markdown.render_cycle(one).encode("utf-8"), markdown.render_cycle(other).encode("utf-8")
    assert a == b and hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()
    assert markdown.render_cycle(one).splitlines()[0] == "# Cycle record" and "**Timings**: not provided" in markdown.render_cycle(one)
    assert "**Outputs**: a: 1, 2; b: 1.5" in markdown.render_cycle(one)


# ----------------------------------------------------------------------------------------------------- TA-83 and TA-85

def state():
    return {"desk_id": 1, "symbol": "NQ", "reports": {"market": make_report("market", "Up trend <b>strong</b>"), "news": make_report("news", None)},
            "debate": {"count": 2, "summary": "bull leads", "last_speaker": "bull", "last_response": "buy the dip", "verdict": "buy"},
            "risk_debate": {"count": 1, "summary": "size small", "last_speaker": "neutral", "last_response": "ok", "verdict": "approved with a stop"},
            "trader_plan": "Buy 1 NQ at 21000.25 with a stop at 20950", "final_decision": "Approved: 1 contract"}


def tree_files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_the_tree_has_fixed_folders_per_stage_and_running_it_twice_changes_nothing_TA_83(ready):
    first = tree.ensure_tree(ready.desk_id, "NQ", "2026-03-12")
    base = paths.desk_dir(ready.desk_id) / "reports" / "NQ" / "2026-03-12"
    assert list(first) == ["analysts", "debate", "judge", "trader", "risk", "management"]
    assert sorted(p.name for p in base.iterdir()) == ["1_analysts", "2_debate", "3_judge", "4_trader", "5_risk", "6_management"]
    assert all(p.is_dir() and p.parent == base for p in first.values())
    marker = first["trader"] / "keep.md"
    marker.write_text("mine", encoding="utf-8")
    again = tree.ensure_tree(ready.desk_id, "NQ", "2026-03-12")
    assert again == first and marker.read_text(encoding="utf-8") == "mine"
    assert sorted(p.name for p in base.iterdir()) == ["1_analysts", "2_debate", "3_judge", "4_trader", "5_risk", "6_management"]


def test_the_tree_refuses_names_that_leave_the_desk_folder_TA_83(ready):
    for symbol in ("../x", "a/b", "..", "", "x" * 40):
        with pytest.raises(ValueError):
            tree.ensure_tree(ready.desk_id, symbol, "2026-03-12")
    for day in ("2026-3-12", "..", "2026-02-30"):
        with pytest.raises(ValueError):
            tree.ensure_tree(ready.desk_id, "NQ", day)
    with pytest.raises(paths.PathEscape):
        tree.ensure_tree(0, "NQ", "2026-03-12")


def test_the_reports_are_written_to_their_stage_and_joined_in_the_complete_report_TA_83(ready):
    target = tree.save_reports(ready.desk_id, "NQ", "2026-03-12", state())
    base = paths.desk_dir(ready.desk_id) / "reports" / "NQ" / "2026-03-12"
    assert target == base / "complete_report.md"
    files = tree_files(base)
    assert sorted(files) == ["1_analysts/market.md", "1_analysts/news.md", "2_debate/debate.md", "3_judge/verdict.md", "4_trader/plan.md", "5_risk/risk.md",
                             "6_management/decision.md", "complete_report.md"]
    assert files["4_trader/plan.md"].decode() == "## plan\n\nBuy 1 NQ at 21000.25 with a stop at 20950\n"
    assert b"&lt;b&gt;strong&lt;/b&gt;" in files["1_analysts/market.md"] and b"<b>" not in files["1_analysts/market.md"]
    complete = files["complete_report.md"].decode()
    assert [h for h in complete.splitlines() if h.startswith("## ")] == ["## I. Analyst reports", "## II. Debate", "## III. Judge", "## IV. Trader plan", "## V. Risk review",
                                                                         "## VI. Management decision"]
    assert complete.startswith("# Trading analysis report") and "**Symbol**: NQ" in complete and "(No news report in this run" in complete
    assert "approved with a stop" in complete and "Approved: 1 contract" in complete


def test_a_stage_with_nothing_says_so_in_the_complete_report_TA_83(ready):
    target = tree.save_reports(ready.desk_id, "ES", "2026-03-12", {"desk_id": 1, "symbol": "ES", "trader_plan": "Stand aside"})
    complete = target.read_text(encoding="utf-8")
    assert complete.count("not provided") == 5                                           # every stage but the trader says it has nothing
    assert sorted(p.name for p in (target.parent / "1_analysts").iterdir()) == []


def test_the_same_reports_come_out_of_the_app_the_chat_and_code_TA_85(ready):
    outputs = []
    for caller in ("app", "chat", "code"):
        target = tree.save_reports(ready.desk_id, "NQ", "2026-03-12", state(), caller=caller)
        outputs.append(tree_files(target.parent))
    assert outputs[0] == outputs[1] == outputs[2] and len(outputs[0]) == 8                # the same files with the same bytes
    mtimes = {p: p.stat().st_mtime_ns for p in (paths.desk_dir(ready.desk_id) / "reports" / "NQ" / "2026-03-12").rglob("*.md")}
    tree.save_reports(ready.desk_id, "NQ", "2026-03-12", state())
    assert {p: p.stat().st_mtime_ns for p in mtimes} == mtimes                           # an identical file is not even touched
    with pytest.raises(tree.TreeError):
        tree.save_reports(ready.desk_id, "NQ", "2026-03-12", state(), caller="cron")
    changed = tree.save_reports(ready.desk_id, "NQ", "2026-03-12", {**state(), "trader_plan": "Sell 1 NQ"})
    assert "Sell 1 NQ" in changed.read_text(encoding="utf-8")
