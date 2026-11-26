"""The tools of the factory (SPEC-020, section 4.11): list, describe, draft, propose and the paper search. Ids and words only; nothing is sent anywhere."""
import functools
from pathlib import Path

import pytest
from backtest.test_no_live_path import violations
from data.support import RecordedTransport, fixture_bytes, when
from dbsupport import db, env  # noqa: F401  (fixtures)
from e2e_h4.support import IAN, call, caller, failed, ok, spec_text

from hokma.data import papers
from hokma.data.providers import RateLimitError
from hokma.factory import researcher
from hokma.kit import backtest_tools, factory_tools, hokma_tools, source_tools, verdict_tools

NAMES = ("factory_list", "factory_describe", "factory_draft", "factory_propose", "data_papers")
FILE = Path(__file__).resolve().parents[2] / "backend" / "hokma" / "kit" / "factory_tools.py"
NOW = when("2024-04-10T12:00:00")
WRONG = """
    - name: this_is_wrong
      facts: {ema_fast: [101, 102, 103], ema_slow: [100, 100, 100]}
      expect: short
"""


def test_the_five_tools_are_published_with_a_strict_schema_and_need_no_database_unit():
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    assert set(NAMES) == set(factory_tools.TOOLS)
    for name in NAMES:
        assert listed[name]["inputSchema"]["additionalProperties"] is False and name in hokma_tools.NO_UNIT
    assert [hokma_tools.TOOLS[n][4] for n in NAMES] == [False, False, True, True, False]
    premium = {"premium_calibrate", "premium_switch_state"}        # the threshold of the premium is for the owner and the monthly routine, never for Ian
    assert set(NAMES) | (set(backtest_tools.TOOLS) - premium) | {"quant_call", "data_bars", "data_snapshot"} | set(source_tools.TOOLS) | {"strategy_brief_write", "strategy_code_list", "strategy_code_get"} | set(verdict_tools.TOOLS) | {"task_create", "task_link", "task_reopen", "project_list", "project_show", "project_create", "decision_propose", "decision_list", "task_show", "task_list", "task_claim", "task_heartbeat", "task_complete", "task_block", "task_comment", "journal_write", "journal_search", "ips_read"} == set(researcher.TOOLS)


def test_the_tool_file_has_no_road_to_a_broker_an_order_or_a_desk():
    text = FILE.read_text(encoding="utf-8")
    assert violations(text, "hokma.kit") == []
    for word in ("hokma.exec", "hokma.broker", "hokma.market", "orders_enabled", "premium_calibrate"):
        assert word not in text, word
    for name in NAMES:
        for key in hokma_tools.TOOLS[name][1]:
            assert key not in ("path", "file", "folder", "directory", "regex", "pattern", "glob", "url", "transport", "db")


def test_the_list_has_the_registry_the_ideas_the_literature_and_the_saved_specs(db):
    everything = ok("factory_list", {})
    assert set(everything) == {"registry", "ideas", "literature", "specs"}
    assert any(r["id"] == "ema_cross" and r["kind"] == "alpha" for r in everything["registry"]["rows"])
    assert everything["ideas"]["total"] > 0 and everything["literature"]["total"] == 61 and everything["specs"] == {"rows": [], "total": 0}
    only = ok("factory_list", {"source": "registry", "kind": "alpha", "limit": 3})
    assert set(only) == {"registry"} and len(only["registry"]["rows"]) == 3 and only["registry"]["total"] > 3


def test_min_t_and_asset_class_come_from_the_literature_of_the_registry_module(db):
    from hokma.factory.registry import factory_literature

    strong = ok("factory_list", {"source": "literature", "min_t": 3, "limit": 200})["literature"]
    assert [r["edge_id"] for r in strong["rows"]] == [r["edge_id"] for r in factory_literature(min_t=3)] and strong["total"] == 49
    assert all(r["t_stat"] >= 3 for r in strong["rows"]) and [r["t_stat"] for r in strong["rows"]] == sorted((r["t_stat"] for r in strong["rows"]), reverse=True)
    bonds = ok("factory_list", {"source": "literature", "asset_class": "bonds", "limit": 200})["literature"]
    assert bonds["total"] == 12 and {r["asset_class"] for r in bonds["rows"]} == {"bonds"}
    both = ok("factory_list", {"source": "literature", "asset_class": "bonds", "min_t": 3, "limit": 200})["literature"]
    assert 0 < both["total"] <= 12 and all(r["asset_class"] == "bonds" and r["t_stat"] >= 3 for r in both["rows"])
    assert failed("factory_list", {"source": "literature", "asset_class": "tulips"})["error_code"] == "bad_input"
    text, is_error = call("factory_list", {"source": "registry", "kind": "robot"})
    assert is_error and "kind must be one of" in text                                      # an enum of the schema
    assert failed("factory_list", {"line": "XX"})["error_code"] == "bad_input"
    assert failed("factory_list", {"limit": 10_000})["error_code"] == "bad_input"
    text, is_error = call("factory_list", {"source": "everything"})
    assert is_error and "source must be one of" in text


def test_describe_reads_an_alpha_an_idea_and_a_saved_spec_and_refuses_what_it_does_not_know(db):
    alpha = ok("factory_describe", {"id": "ema_cross"})
    assert alpha["kind"] == "alpha" and {p["name"] for p in alpha["schema"]} >= {"fast", "slow"} and alpha["class"].startswith("hokma.factory.alphas.")
    idea_id = ok("factory_list", {"source": "ideas", "limit": 1})["ideas"]["rows"][0]["id"]
    idea = ok("factory_describe", {"id": idea_id})
    assert idea["id"] == idea_id and idea["variant_list"] and idea["summary"]
    ok("factory_draft", {"spec_yaml": spec_text("my_cross")})
    saved = ok("factory_describe", {"id": "my_cross"})
    assert (saved["kind"], saved["version"], saved["status"]) == ("spec", 1, "draft") and "strategy_id: my_cross" in saved["spec_yaml"]
    assert ok("factory_describe", {"id": "my_cross", "version": 1})["spec_hash"] == saved["spec_hash"]
    missing = failed("factory_describe", {"id": "nothing_here"})
    assert missing["error_code"] == "no_data" and missing["suggestions"] == ["factory_list"]
    assert failed("factory_describe", {"id": "my_cross", "version": 9})["error_code"] == "no_data"
    assert failed("factory_describe", {"id": "../../etc/passwd"})["error_code"] == "no_data"           # an id is looked up, never opened
    assert failed("factory_describe", {"id": "x\x00y"})["error_code"] == "bad_input"


def test_a_draft_is_saved_as_the_next_version_and_a_bad_spec_is_refused(db):
    first = ok("factory_draft", {"spec_yaml": spec_text("ema_demo")})
    assert (first["version"], first["status"], first["line"]) == (1, "draft", "DT") and len(first["spec_hash"]) == 64
    assert ok("factory_draft", {"spec_yaml": spec_text("ema_demo")})["version"] == 1                      # the same text is not saved again
    assert ok("factory_draft", {"spec_yaml": spec_text("ema_demo", hypothesis="A different hypothesis.")})["version"] == 2
    for bad in ("not: [valid", "strategy_id: x", spec_text("x").replace("alpha: ema_cross", "alpha: nothing"), spec_text("x").replace("line: DT", "line: ZZ"), "x" * 40_000):
        assert failed("factory_draft", {"spec_yaml": bad})["error_code"] == "bad_input", bad[:30]
    assert failed("factory_draft", {"spec_yaml": spec_text("ema_demo", hypothesis="Another."), "root_cause": "x" * 5_000})["error_code"] == "bad_input"
    assert failed("factory_draft", {"spec_yaml": spec_text("ema_demo", hypothesis="Another."), "root_cause": "short"})["error_code"] == "bad_input"


def test_the_third_failed_attempt_closes_the_strategy_until_a_root_cause_is_written(db):
    who = caller(IAN, conversation=7, task=7)
    ok("factory_draft", {"spec_yaml": spec_text("spent")}, who)
    budget = backtest_tools.research_session(who).budget
    for _ in range(3):
        budget.fail("spent", 1)                                       # a result that ran but made no sense is reported by the session
    closed = failed("factory_draft", {"spec_yaml": spec_text("spent", hypothesis="Try again.")}, who)
    assert closed["error_code"] == "attempt_budget_spent" and "root_cause" in closed["message"]
    cause = "The average cross buys the top of every range, so costs eat the edge: the entry needs a volatility filter."
    again = ok("factory_draft", {"spec_yaml": spec_text("spent", hypothesis="Try again."), "root_cause": cause}, who)
    assert again["version"] == 2                                      # the count starts again from the version that carries the cause
    assert ok("factory_draft", {"spec_yaml": spec_text("spent", hypothesis="And again.")}, who)["version"] == 3


def test_propose_only_moves_a_draft_to_proposed_and_the_proposer_is_fixed_by_the_code(db):
    ok("factory_draft", {"spec_yaml": spec_text("ready_one")})
    proposed = ok("factory_propose", {"strategy_id": "ready_one", "version": 1}, caller("hokma"))
    assert proposed["status"] == "proposed"
    with db.read() as u:
        row = u.session.one("SELECT status, proposed_by FROM strategy_specs WHERE strategy_id = 'ready_one'")
    assert (row["status"], row["proposed_by"]) == ("proposed", researcher.AGENT_SLUG)                      # whoever asks, the code writes it
    assert failed("factory_propose", {"strategy_id": "ready_one", "version": 1})["error_code"] == "bad_input"        # only a draft can be proposed
    assert failed("factory_propose", {"strategy_id": "ready_one", "version": 5})["error_code"] == "bad_input"
    assert "proposed_by" not in hokma_tools.TOOLS["factory_propose"][1] and "status" not in hokma_tools.TOOLS["factory_propose"][1]


def test_a_spec_without_tests_cannot_be_proposed_and_one_whose_test_fails_is_not_either(db):
    ok("factory_draft", {"spec_yaml": spec_text("untested", signals="", sizes="")})
    assert failed("factory_propose", {"strategy_id": "untested", "version": 1})["error_code"] == "missing_tests"
    ok("factory_draft", {"spec_yaml": spec_text("failing", signals=WRONG)})
    answer = failed("factory_propose", {"strategy_id": "failing", "version": 1})
    assert answer["error_code"] == "bad_input" and "tests_failed" in answer["message"] and "this_is_wrong" in answer["message"]
    with db.read() as u:
        assert {r["status"] for r in u.session.query("SELECT status FROM strategy_specs")} == {"draft"}


@pytest.fixture
def recorded(monkeypatch):
    def answer(url, params, headers):
        if url == papers.ARXIV_URL:
            return 200, fixture_bytes("papers_arxiv_search.xml")
        known = {"2101.00001": "papers_scholar_2101.00001.json", "2102.00002": "papers_scholar_2102.00002.json"}
        return 200, fixture_bytes(known.get(url.split("ARXIV:")[1], "papers_scholar_unknown.json"))

    transport = RecordedTransport(answer)
    for name in ("search_papers", "read_paper"):
        monkeypatch.setattr(papers, name, functools.partial(getattr(papers, name), transport=transport, clock=lambda: NOW))
    return transport


def test_papers_are_searched_and_read_by_words_and_ids_only(recorded):
    found = ok("data_papers", {"action": "search", "query": "momentum futures", "limit": 2})
    assert found["source"] == "arxiv" and found["value"][0]["arxiv_id"] == "2101.00001" and found["value"][0]["citations"] == 412
    assert recorded.calls[0][1]["max_results"] == 2
    cut = ok("data_papers", {"action": "search", "query": "momentum futures", "as_of": "2021-01-31"})
    assert [p["arxiv_id"] for p in cut["value"]] == ["2101.00001", "q-fin.TR/1907003"] and any(w.startswith("papers_after_as_of_removed") for w in cut["warnings"])
    one = ok("data_papers", {"action": "read", "arxiv_id": "2101.00001"})
    assert one["value"][0]["title"] == "Fixture Paper A: momentum in futures"
    for bad in ({"action": "search"}, {"action": "search", "query": "x"}, {"action": "search", "query": "a" * 300}, {"action": "read"}, {"action": "read", "arxiv_id": "../../etc/passwd"},
                {"action": "search", "query": "momentum", "limit": 99}, {"action": "search", "query": "momentum", "as_of": "someday"}):
        assert failed("data_papers", bad)["error_code"] == "bad_input", bad


def test_a_provider_that_is_down_is_unavailable_and_never_an_empty_list(monkeypatch):
    def down(url, params, headers):
        raise RateLimitError("network failure: URLError", reason="unreachable")

    monkeypatch.setattr(papers, "search_papers", functools.partial(papers.search_papers, transport=RecordedTransport(down), clock=lambda: NOW))
    answer = failed("data_papers", {"action": "search", "query": "momentum futures"})
    assert answer["error_code"] == "unavailable" and "value" not in answer
