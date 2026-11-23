"""MCP tools of the quant and data modules (SPEC-017, B9: FT-100, OA-67, ED-031, ED-032, ED-033, ED-034, CC-09, CC-11)."""
import json

import pytest

from hokma.kit import data_tools, hokma_tools, quant_tools
from hokma.kit.tool_registry import CODES, ToolError

from data.support import FakeMarketSource, market_source, when  # noqa: F401

CALLER = hokma_tools.Caller({})


def run(name, args):
    handler = hokma_tools.TOOLS[name][3]
    assert hokma_tools.validate(name, args) is None
    return json.loads(handler(None, CALLER, args))


def error(name, args):
    with pytest.raises(ToolError) as err:
        hokma_tools.TOOLS[name][3](None, CALLER, args)
    assert err.value.code in CODES
    return json.loads(err.value.payload())


RET = [0.01, -0.004, 0.006, 0.002, -0.01, 0.012, 0.003, -0.002, 0.007, 0.001] * 6


def test_server_lists_every_tool_with_its_schema_FT_100_ED_031():
    names = {d["name"]: d for d in hokma_tools.definitions()}
    for tool in ("quant_list", "quant_describe", "quant_call", "data_bars", "data_snapshot", "data_fundamentals", "data_filings",
                 "data_macro", "data_universe", "data_filings_watch"):
        assert tool in names and names[tool]["inputSchema"]["additionalProperties"] is False
        assert tool in hokma_tools.NO_UNIT


def test_quant_list_describe_and_call_by_name_OA_67():
    listed = run("quant_list", {"module": "risk", "search": "var"})
    assert any(f["name"] == "risk.var" for f in listed["functions"])
    described = run("quant_describe", {"name": "metrics.sharpe"})
    assert described["name"] == "metrics.sharpe" and described["piece_ids"]
    sharpe = run("quant_call", {"name": "metrics.sharpe", "params": {"returns": RET}})
    var = run("quant_call", {"name": "risk.var", "params": {"returns": RET, "level": 0.9}})
    psr = run("quant_call", {"name": "validation.psr", "params": {"returns": RET}})
    for result in (sharpe, var, psr):
        assert result["value"] is not None and result["method"] and result["inputs_hash"] and result["piece_ids"]


def test_quant_errors_are_classified_ED_032():
    assert error("quant_call", {"name": "metrics.nope"})["error_code"] == "bad_input"
    assert error("quant_describe", {"name": "metrics.sharpee"})["suggestions"]
    blocked = error("quant_call", {"name": "metrics.probabilistic_sharpe_ratio", "params": {}})
    assert blocked["error_code"] == "blocked" and "validation.psr" in blocked["suggestions"]
    assert error("quant_call", {"name": "metrics.sharpe", "params": {"returns": RET, "wrong": 1}})["error_code"] == "bad_input"
    assert error("quant_call", {"name": "risk.var", "params": {"returns": RET, "horizon": 10**9}})["error_code"] == "bad_input"


def test_quant_input_formats_cover_series_and_tables():
    import pandas as pd

    dated = run("quant_call", {"name": "metrics.sharpe", "params": {"returns": {"values": RET, "index": [str(d.date()) for d in pd.bdate_range("2024-01-01", periods=60)]}}})
    assert dated["value"] is not None


def test_data_snapshot_returns_numbers_with_source_and_hash_ED_031(market_source):
    market_source(FakeMarketSource())
    out = run("data_snapshot", {"symbol": "ACME", "as_of": "2024-03-15", "categories": ["market"]})
    section = out["sections"]["market"]
    assert section["status"] == "ok" and section["source"] and out["content_hash"]
    assert error("data_snapshot", {"symbol": "ACME", "as_of": "2024-03-15", "categories": []})["error_code"] == "bad_input"
    assert error("data_snapshot", {"symbol": "../etc", "as_of": "2024-03-15", "categories": ["market"]})["error_code"] == "bad_input"
    assert error("data_snapshot", {"symbol": "ACME", "as_of": "yesterday-ish", "categories": ["market"]})["error_code"] == "bad_input"


def test_data_bars_checks_range_and_classifies_provider_errors_ED_033(market_source):
    market_source(FakeMarketSource())
    out = run("data_bars", {"symbol": "ACME", "interval": "1D", "start": "2024-01-01", "end": "2024-03-01"})
    assert out["status"] == "ok" and out["source"] and out["received_at"]
    assert error("data_bars", {"symbol": "ACME", "interval": "1D", "start": "2000-01-01", "end": "2024-03-01"})["error_code"] == "bad_input"
    assert error("data_bars", {"symbol": "ACME", "interval": "1D", "start": "2024-03-01", "end": "2024-01-01"})["error_code"] == "bad_input"


def test_universe_refuses_paths_and_urls_ED_034():
    for key in ("directory", "base_url", "cache_dir", "clock", "path", "url"):
        assert error("data_universe", {"filters": {key: "/etc"}})["error_code"] == "bad_input"
    assert error("data_universe", {"filters": {"sector": ["a"]}})["error_code"] == "bad_input"
    assert error("data_universe", {"filters": {"Sector!": "x"}})["error_code"] == "bad_input"


def test_filings_watch_and_macro_validate_inputs_ED_033():
    assert error("data_filings_watch", {"symbols": [], "forms": ["8-K"]})["error_code"] == "bad_input"
    assert error("data_filings_watch", {"symbols": ["A"] * 30, "forms": ["8-K"]})["error_code"] == "bad_input"
    assert error("data_macro", {"series_id": "../../x", "as_of": "2024-01-01"})["error_code"] == "bad_input"
    assert error("data_filings", {"symbol": "ACME", "form": "10-K; drop", "as_of": "2024-01-01"})["error_code"] == "bad_input"


def test_account_ids_and_long_lists_are_cleaned_CC_09():
    cleaned = data_tools._clean({"note": "account_id: 12345678 here", "rows": list(range(5000))})
    assert "12345678" not in cleaned["note"] and len(cleaned["rows"]) == data_tools.MAX_ROWS


def test_only_hokma_tools_are_published_CC_11():
    banned = ("edgar", "company", "filing_", "xbrl", "mcp_", "skill", "agent")
    published = set(quant_tools.TOOLS) | set(data_tools.TOOLS)
    assert published == {"quant_list", "quant_describe", "quant_call", "data_bars", "data_snapshot", "data_fundamentals", "data_filings",
                         "data_macro", "data_universe", "data_filings_watch"}
    assert not [n for n in published if any(n.startswith(b) for b in banned)]


def test_discovery_tools_share_the_schema_style_of_the_rest_CC_09():
    for name in ("quant_list", "quant_describe", "data_universe"):
        desc, props, required, handler, writes = hokma_tools.TOOLS[name]
        assert desc and isinstance(props, dict) and callable(handler) and writes is False


def test_universe_text_filters_are_literal_not_regular_expressions(monkeypatch):
    import time

    import hokma.data as data
    from hokma.data import sentinels

    seen = {}

    def fake(**filters):
        seen.update(filters)
        return sentinels.no_data("catalog", "empty_result")

    monkeypatch.setattr(data, "universe", fake)
    hostile = "(a+)+$"
    with pytest.raises(ToolError):
        hokma_tools.TOOLS["data_universe"][3](None, CALLER, {"filters": {"sector": hostile, "country": "United States"}})
    assert seen["sector"] != hostile and seen["sector"] == __import__("re").escape(hostile)
    import re
    start = time.perf_counter()
    assert re.search(seen["sector"], "a" * 40 + "!") is None
    assert time.perf_counter() - start < 0.5
