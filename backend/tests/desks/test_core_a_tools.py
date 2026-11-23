"""Tools by role (TA-15, TA-79) and the audit of calls (OA-15)."""
import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hokma.desks.core import graph as g
from hokma.desks.core import tools as tl
from hokma.desks.core.state import ToolCall, initial_state
from hokma.desks.core.tool_audit import ToolAuditLog, run_tool_audit, summarize_input
from hokma.quant import registry
from hokma.quant.errors import BadInput, BlockedFunction
from hokma.quant.types import QuantResult

SRC = Path(__file__).resolve().parents[2] / "hokma" / "desks" / "core"
SCHEMA = {"type": "object", "properties": {"symbol": {"type": "string"}}}


def spec(name, handler=None, **kw):
    return tl.ToolSpec(name, kw.pop("description", f"Tool {name} returns data for one symbol at a cut-off time."), SCHEMA,
                       handler or (lambda symbol="X": {"symbol": symbol, "rows": 3}), **kw)


@pytest.fixture
def catalog():
    cat = tl.ToolCatalog()
    for name in ("get_bars", "get_news", "get_filings"):
        cat.register(spec(name, example={"symbol": "AAPL"}))
    cat.assign("market", ["get_bars"])
    cat.assign("news", ["get_news", "get_filings"])
    return cat


@pytest.fixture
def probe_functions():
    @registry.register("metrics.corea_probe", module="metrics", summary="Probe used by the tool tests.", inputs={"x": {"type": "float", "required": True}})
    def probe(x):
        return QuantResult(name="metrics.corea_probe", value=float(x) * 2, unit="ratio", method="double")

    registry.register_blocked("metrics.corea_blocked", module="metrics", summary="Blocked probe.", replacement="metrics.corea_probe")
    yield
    registry._FUNCTIONS.pop("metrics.corea_probe", None)
    registry._FUNCTIONS.pop("metrics.corea_blocked", None)


def test_each_analyst_receives_only_the_tools_of_its_role_TA_15(catalog):
    assert catalog.toolbox_for("market").names() == ("get_bars",)
    assert catalog.toolbox_for("news").names() == ("get_news", "get_filings")
    assert [s["name"] for s in catalog.toolbox_for("market").schemas()] == ["get_bars"]


def test_asking_for_a_tool_of_another_role_fails_readably_TA_15(catalog):
    box = catalog.toolbox_for("market")
    with pytest.raises(tl.ToolNotAllowed, match=r"market cannot use the tool 'get_news'; its tools are: get_bars"):
        box.call("get_news", {"symbol": "AAPL"})
    text = box.run(ToolCall("1", "get_news", {"symbol": "AAPL"}))
    body = json.loads(text)
    assert body["ok"] is False and body["error"]["kind"] == "blocked" and "get_news" in body["error"]["message"]


def test_unknown_holder_and_unknown_assignment_are_refused_TA_15(catalog):
    with pytest.raises(tl.ToolNotAllowed):
        catalog.toolbox_for("ghost")
    with pytest.raises(ValueError, match="unknown tool"):
        catalog.assign("x", ["nope"])
    with pytest.raises(ValueError, match="already registered"):
        catalog.register(spec("get_bars"))
    with pytest.raises(ValueError):
        spec("Bad Name")
    with pytest.raises(ValueError):
        tl.ToolSpec("good_name", "  ", SCHEMA, lambda: 1)


def test_catalogs_do_not_share_state_TA_15(catalog):
    other = tl.ToolCatalog()
    assert other.holders() == () and "market" in catalog.holders()


def test_results_and_errors_are_classified_TA_79():
    def boom(**_):
        raise RuntimeError("provider down")

    cases = {
        "ok_tool": (lambda symbol="X": {"v": 1}, True, ""),
        "no_data_tool": (lambda **_: (_ for _ in ()).throw(tl.ToolFailure("no_data", "nothing for X")), False, "no_data"),
        "stale_tool": (lambda **_: (_ for _ in ()).throw(tl.ToolFailure("stale", "old")), False, "stale"),
        "blocked_tool": (lambda **_: (_ for _ in ()).throw(BlockedFunction("a.b", "a.c")), False, "blocked"),
        "bad_tool": (lambda **_: (_ for _ in ()).throw(BadInput("shape")), False, "bad_input"),
        "boom_tool": (boom, False, "unavailable"),
    }
    cat = tl.ToolCatalog()
    for name, (fn, _, _) in cases.items():
        cat.register(spec(name, fn))
    cat.assign("r", cases)
    box = cat.toolbox_for("r")
    for name, (_, ok, kind) in cases.items():
        result = box.call(name, {})
        assert result.ok is ok and result.error_kind == kind, name
    assert box.call("ok_tool", {"wrong_arg": 1}).error_kind == "bad_input"       # a handler called with arguments it does not take
    with pytest.raises(ValueError):
        tl.ToolFailure("weird", "x")
    assert set(tl.ERROR_KINDS) == {"no_data", "unavailable", "stale", "bad_input", "blocked"}


def test_large_results_are_truncated_into_valid_json_TA_79():
    big = tl.ToolResult(True, {"rows": "x" * (tl.MAX_RESULT_CHARS + 10)}).to_text()
    assert json.loads(big)["truncated"] is True and len(big) < tl.MAX_RESULT_CHARS + 200


def test_quant_tools_call_the_registry_never_a_library_TA_15(probe_functions):
    cat = tl.ToolCatalog()
    for s in tl.quant_tool_specs():
        cat.register(s)
    cat.assign("analyst", ["quant_list", "quant_describe", "quant_call"])
    box = cat.toolbox_for("analyst")
    listed = box.call("quant_list", {"module": "metrics"}).data
    assert "metrics.corea_probe" in [i["name"] for i in listed] and "metrics.corea_blocked" not in [i["name"] for i in listed]
    assert box.call("quant_describe", {"name": "metrics.corea_probe"}).data["summary"].startswith("Probe")
    out = box.call("quant_call", {"name": "metrics.corea_probe", "arguments": {"x": 21}})
    assert out.ok and out.data["value"] == 42.0 and out.data["name"] == "metrics.corea_probe"
    assert box.call("quant_call", {"name": "metrics.corea_blocked", "arguments": {}}).error_kind == "blocked"
    assert box.call("quant_call", {"name": "metrics.nope"}).error_kind == "bad_input"
    assert box.call("quant_call", {"name": "metrics.corea_probe", "arguments": {}}).error_kind == "bad_input"
    assert box.call("quant_call", {"name": "metrics.corea_probe", "arguments": [1]}).error_kind == "bad_input"


def test_tool_modules_import_no_library_TA_15():
    banned = {"scipy", "sklearn", "skfolio", "quantstats", "ffn", "financetoolkit", "financedatabase", "edgar", "edgartools"}
    for name in ("tools.py", "tool_audit.py"):
        tree = ast.parse((SRC / name).read_text())
        imported = {n.split(".")[0] for node in ast.walk(tree) for n in
                    ([a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])}
        assert not imported & banned, name
    assert "from hokma.quant import registry" in (SRC / "tools.py").read_text()


def test_graph_analysts_see_only_their_own_schemas_TA_15(catalog):
    seen = {}

    class Model:
        def chat(self, system, messages, tools=()):
            who = system.split("speaker=")[1]
            seen.setdefault(who, [t["name"] for t in tools])
            if tools and not any(m.role == "tool" for m in messages):
                return g.ModelReply("", (ToolCall("c1", tools[0]["name"], {"symbol": "AAPL"}),))
            return g.ModelReply(f"{who} report")

    flow = g.FlowSpec((tl.analyst_spec("market", catalog.toolbox_for("market")), tl.analyst_spec("news", catalog.toolbox_for("news"))))
    state = initial_state(desk_id=1, symbol="AAPL", line="SWC", as_of=datetime(2026, 1, 2, tzinfo=timezone.utc), horizon="weeks", analysts=("market", "news"))
    result = g.build_desk_graph(flow, Model(), lambda role, speaker, s: f"role={role};speaker={speaker}").invoke(state)
    assert seen == {"market": ["get_bars"], "news": ["get_news", "get_filings"]}
    assert result.state["reports"]["news"]["text"] == "news report" and result.state["tool_rounds"] == {"market": 1, "news": 1}


# ---- audit (OA-15) ----

def test_every_call_is_recorded_with_agent_tool_input_and_result_OA_15(catalog):
    log = ToolAuditLog()
    box = catalog.toolbox_for("market", audit=log)
    box.call("get_bars", {"symbol": "AAPL"}, agent="A07")
    with pytest.raises(tl.ToolNotAllowed):
        box.call("get_news", {"symbol": "AAPL"}, agent="A07")
    first, second = log.records()
    assert (first.agent, first.tool, first.ok) == ("A07", "get_bars", True)
    assert '"symbol": "AAPL"' in first.input_summary and "chars:" in first.result_summary and first.at.endswith("+00:00")
    assert (second.tool, second.ok, second.error_kind) == ("get_news", False, "blocked")
    assert [r.tool for r in log.records(tool="get_bars")] == ["get_bars"] and len(log.records(agent="other")) == 0


def test_failures_are_recorded_too_OA_15():
    cat = tl.ToolCatalog()
    cat.register(spec("fail_tool", lambda **_: (_ for _ in ()).throw(tl.ToolFailure("no_data", "nothing"))))
    cat.assign("r", ["fail_tool"])
    log = ToolAuditLog()
    cat.toolbox_for("r", audit=log).call("fail_tool", {})
    (rec,) = log.records()
    assert rec.ok is False and rec.error_kind == "no_data" and rec.result_summary.startswith("error no_data")


def test_secrets_never_reach_the_record_OA_15():
    text = summarize_input({"symbol": "AAPL", "api_key": "SECRET123", "broker_account": "9988", "nested": {"token": "T0K", "ok": 1},
                            "long": "y" * 500})
    assert "SECRET123" not in text and "9988" not in text and "T0K" not in text and "AAPL" in text and len(text) <= 240
    assert summarize_input(None) == "{}"


def test_log_keeps_a_window_writes_jsonl_and_feeds_a_sink_OA_15(tmp_path):
    got = []
    log = ToolAuditLog(sink=got.append, path=tmp_path / "audit" / "calls.jsonl", max_records=2)
    for i in range(3):
        log.record(agent="a", tool=f"t{i}", arguments={"i": i}, ok=True, result_text="x")
    assert [r.tool for r in log.records()] == ["t1", "t2"] and len(got) == 3
    lines = (tmp_path / "audit" / "calls.jsonl").read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["tool"] == "t0"
    with pytest.raises(ValueError):
        ToolAuditLog(max_records=0)


def test_tool_audit_walks_every_tool_with_its_example_OA_15():
    cat = tl.ToolCatalog()
    cat.register(spec("good_tool", example={"symbol": "AAPL"}))
    cat.register(spec("noexample_tool"))
    cat.register(spec("failing_tool", lambda **_: (_ for _ in ()).throw(tl.ToolFailure("unavailable", "down")), example={}))
    cat.register(spec("empty_tool", lambda **_: [], example={}))
    cat.register(spec("thin_tool", description="Short.", example={"symbol": "A"}))
    calls = []
    cat.register(spec("order_tool", lambda **_: calls.append(1), example={}, mutates=True))
    cat.assign("auditor", ["good_tool", "noexample_tool", "failing_tool", "empty_tool", "thin_tool", "order_tool"])
    log = ToolAuditLog()
    rows = {r.tool: r for r in run_tool_audit(cat.toolbox_for("auditor", audit=log))}
    assert rows["good_tool"].clean
    assert "no runnable example" in rows["noexample_tool"].issues[0] and not rows["noexample_tool"].ran
    assert rows["failing_tool"].error_kind == "unavailable" and not rows["failing_tool"].ran
    assert "returns nothing" in rows["empty_tool"].issues[0] and rows["empty_tool"].ran
    assert "too thin" in rows["thin_tool"].issues[0]
    assert "skipped" in rows["order_tool"].issues[-1] and calls == []        # a tool that changes state is never called
    assert {r.agent for r in log.records()} == {"tool-audit"} and "order_tool" not in {r.tool for r in log.records()}
