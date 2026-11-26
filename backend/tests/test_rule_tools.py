"""The five tools of the strategy creator for code (SPEC-031 A2, section 5): their shape, their classified errors, who and where are set by the code, and the bank rules for generated ids."""
import re

from bank_support import pending_sheet
from code_support import BRIEF, CODER, REVIEWER, RESEARCHER, STRATEGY, TEST, brief, db, env, make_bank, pg_admin_url, pg_template, pg_url_migrated, put_files, rows  # noqa: F401  (fixtures)
from e2e_h4.support import call, caller, failed, ok

from hokma.kit import hokma_tools, rule_tools

NAMES = ("strategy_brief_write", "strategy_code_save", "strategy_code_verdict", "strategy_code_list", "strategy_code_get")
SLUG = "inside_bar"


def written(db):
    bank_id = make_bank(db)
    made = ok("strategy_brief_write", {"bank_id": bank_id, "slug": SLUG, "title": "Inside bar", "brief": BRIEF}, caller(RESEARCHER))
    return bank_id, made


def test_the_five_tools_are_published_with_a_strict_schema_and_the_names_of_a_bank_not_of_programming():
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    assert set(rule_tools.TOOLS) == set(NAMES)
    for name in NAMES:
        assert name in listed and listed[name]["inputSchema"]["additionalProperties"] is False and name in hokma_tools.NO_UNIT
        assert not re.search(r"exec|order|desk|broker|submit|approve|flatten", name)
    assert [hokma_tools.TOOLS[n][4] for n in NAMES] == [True, True, True, False, False]
    for gone in ("coder_write", "coder_test", "rule_contract", "review_run", "rule_brief_check"):
        assert gone not in hokma_tools.TOOLS
    for name in NAMES[:3]:                       # the tools that write take no origin; the list takes it as a filter
        for key in hokma_tools.TOOLS[name][1]:
            assert key not in ("origin", "created_by", "actor", "reviewer", "caller", "path", "file", "folder", "directory")


def test_who_calls_and_where_it_was_created_are_never_arguments(db):
    bank_id = make_bank(db)
    text, is_error = hokma_tools.call("strategy_brief_write", {"bank_id": bank_id, "slug": SLUG, "title": "t", "brief": BRIEF, "origin": "somewhere"}, caller(RESEARCHER))
    assert is_error and "unknown argument: origin" in text
    text, is_error = hokma_tools.call("strategy_code_verdict", {"rule_version_id": 1, "verdict": "approved", "reviewer": "ben"}, caller(REVIEWER))
    assert is_error and "unknown argument: reviewer" in text
    ok("strategy_brief_write", {"bank_id": bank_id, "slug": SLUG, "title": "t", "brief": BRIEF}, caller(RESEARCHER))
    [rule] = rows(db, "SELECT created_by, origin_desk_id FROM user_rules")
    assert (rule["created_by"], rule["origin_desk_id"]) == (RESEARCHER, None)


def test_the_requirement_tool_classifies_what_it_refuses_and_answers_with_the_next_step(db):
    bank_id, made = written(db)
    assert made["folder"].startswith("service/strategies/STR-001-") and made["rule_id"] >= 1 and "dry_run" in made["next"]
    assert failed("strategy_brief_write", {"bank_id": bank_id, "slug": "other", "title": "t", "brief": {"summary": "x"}}, caller(RESEARCHER))["error_code"] == "bad_input"
    assert failed("strategy_brief_write", {"bank_id": 999, "slug": "other", "title": "t", "brief": BRIEF}, caller(RESEARCHER))["error_code"] == "no_data"


def test_the_save_tool_is_blocked_without_a_requirement_and_checks_with_dry_run_before_it_keeps(db):
    bank_id = make_bank(db)
    with db.write() as u:
        rule_id = u.session.insert("user_rules", {"slug": SLUG, "kind": "strategy", "title": "t", "bank_id": bank_id, "component": "strategy", "created_by": RESEARCHER})
    put_files(SLUG)
    blocked = failed("strategy_code_save", {"rule_id": rule_id}, caller(CODER))
    assert blocked["error_code"] == "blocked" and "no spec" in blocked["message"]
    made = ok("strategy_brief_write", {"bank_id": bank_id, "slug": "second", "title": "second", "brief": BRIEF}, caller(RESEARCHER))
    put_files("second", test=TEST.replace('== "hold"', '== "long"'))
    dry = ok("strategy_code_save", {"rule_id": made["rule_id"], "dry_run": True}, caller(CODER))
    assert dry["saved"] is False and dry["ok"] is False and "FAIL" in dry["output"]
    put_files("second")
    assert ok("strategy_code_save", {"rule_id": made["rule_id"], "dry_run": True}, caller(CODER))["ok"] is True and rows(db, "SELECT * FROM user_rule_versions") == []
    kept = ok("strategy_code_save", {"rule_id": made["rule_id"]}, caller(CODER))
    assert kept["saved"] and kept["status"] == "draft" and kept["registry_id"] == "u_str001_v1"


def test_the_reviewer_reads_runs_again_and_gives_the_verdict_and_the_listing_shows_stage_and_origin(db):
    bank_id, made = written(db)
    files = put_files(SLUG)
    kept = ok("strategy_code_save", {"rule_id": made["rule_id"]}, caller(CODER))
    got = ok("strategy_code_get", {"rule_version_id": kept["rule_version_id"], "rerun": True}, caller(REVIEWER))
    assert got["code"] == (files / "strategy.py").read_text(encoding="utf-8") and got["test_file"] == TEST and got["stage"] == "draft" and got["rerun"]["ok"] is True and "RESULT green" in got["rerun"]["output"]
    assert got["requirement"]["current"] is True and got["requirement"]["brief"]["market"] == "NQ" and got["origin"] == {"kind": "hokma"}
    assert ok("strategy_code_get", {"rule_id": made["rule_id"]}, caller(REVIEWER))["registry_id"] == "u_str001_v1"
    assert ok("strategy_code_get", {"registry_id": "u_str001_v1"}, caller(RESEARCHER))["rule_version_id"] == kept["rule_version_id"]
    assert failed("strategy_code_get", {"registry_id": "u_nothing_v9"}, caller(REVIEWER))["error_code"] == "no_data"
    assert failed("strategy_code_verdict", {"rule_version_id": kept["rule_version_id"], "verdict": "rejected"}, caller(REVIEWER))["error_code"] == "bad_input"
    rejected = ok("strategy_code_verdict", {"rule_version_id": kept["rule_version_id"], "verdict": "rejected", "findings": [{"severity": "major", "message": "no case for the short side with a minimum range", "line": 17}]}, caller(REVIEWER))
    assert rejected["status"] == "failed" and rejected["attempts_left"] == 2
    assert ok("strategy_code_list", {"stage": "failed"}, caller(CODER))["rows"][0]["registry_id"] == "u_str001_v1"
    assert ok("strategy_code_list", {"origin": "desk"}, caller(CODER))["rows"] == [] and ok("strategy_code_list", {"bank_id": bank_id}, caller(CODER))["rows"][0]["origin"] == {"kind": "hokma"}
    assert call("strategy_code_list", {"stage": "lost"}, caller(CODER))[1]                       # the schema refuses a stage that is not one of the six
    assert "findings" in ok("strategy_code_get", {"rule_version_id": kept["rule_version_id"]}, caller(REVIEWER))["reviews"][0]


def test_the_bank_does_not_take_a_generated_id_until_the_reviewer_approved_it(db):
    bank_id, made = written(db)
    put_files(SLUG)
    kept = ok("strategy_code_save", {"rule_id": made["rule_id"]}, caller(CODER))
    shown = ok("bank_get", {"bank_id": bank_id}, caller("hokma"))
    sid, pid = shown["source"]["source_id"], shown["source"]["cited"][0]["passage_id"]
    args = {"title": "Inside bar", "source_id": sid, "extraction": pending_sheet(pid), "strategy_id": kept["registry_id"], "spec_version": 1, "bank_id": bank_id}
    refused = failed("bank_create", args, caller(RESEARCHER))
    assert refused["error_code"] == "blocked" and "has not approved" in refused["message"]
    ok("strategy_code_verdict", {"rule_version_id": kept["rule_version_id"], "verdict": "approved"}, caller(REVIEWER))
    again = ok("bank_create", args, caller(RESEARCHER))
    assert again["strategy"] == {"strategy_id": kept["registry_id"], "spec_version": 1} and again["needs_new_rule"] == []
    assert [r["origin"]["kind"] for r in ok("bank_list", {"origin": "hokma"}, caller("hokma"))["rows"]] == ["hokma"]
    assert call("bank_list", {"origin": "mars"}, caller("hokma"))[1]
    listed = ok("factory_list", {"source": "registry", "kind": "strategy"}, caller(RESEARCHER))["registry"]["rows"]
    assert [r["id"] for r in listed if r.get("generated")] == [kept["registry_id"]]
