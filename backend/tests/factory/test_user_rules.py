"""The strategy as code from the requirement to the registry (SPEC-031 A2): the first gate, the save with its copy and its hash, the verdict that is the second gate,
the origin, the load that refuses a file changed by hand, the owner and the gates of the proposal and of real money."""
import json
import os

import pytest
from code_support import BRIEF, CODER, REVIEWER, RESEARCHER, STRATEGY, TEST, brief, make_bank, put_files, rows
from code_support import db, env, pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.factory import bank, registry as factory_registry, spec as factory_spec, user_rules
from hokma.factory.user_rules import UserRuleError

SLUG = "inside_bar"


def setup_rule(db, **over):
    bank_id = make_bank(db)
    made = user_rules.write_brief(bank_id=bank_id, slug=SLUG, title="Inside bar breakout", brief=brief(**over), caller=RESEARCHER, db=db)
    return bank_id, made


def saved(db, **kw):
    bank_id, made = setup_rule(db)
    put_files(SLUG)
    out = user_rules.save_code(rule_id=made["rule_id"], caller=CODER, db=db, **kw)
    return bank_id, made, out


def approve(db, out, **kw):
    return user_rules.verdict(rule_version_id=out["rule_version_id"], verdict="approved", findings=[], reviewer=REVIEWER, db=db, **kw)


def test_the_requirement_creates_the_rule_and_the_folder_and_a_new_one_replaces_the_current_one(db, env):
    bank_id, made = setup_rule(db)
    assert made["version"] == 1 and made["folder"] == "strategies/inside_bar" and made["duplicate"] is False
    assert (env / "work" / "strategies" / SLUG).is_dir()
    [rule] = rows(db, "SELECT kind, slug, bank_id, component, created_by, origin_desk_id FROM user_rules")
    assert (rule["kind"], rule["slug"], rule["bank_id"], rule["component"], rule["created_by"], rule["origin_desk_id"]) == ("strategy", SLUG, bank_id, "strategy", RESEARCHER, None)
    again = user_rules.write_brief(rule_id=made["rule_id"], brief=BRIEF, caller=RESEARCHER, db=db)
    assert again["duplicate"] is True and again["brief_id"] == made["brief_id"]
    newer = user_rules.write_brief(rule_id=made["rule_id"], brief=brief(market="ES"), caller=RESEARCHER, db=db)
    assert newer["version"] == 2 and newer["duplicate"] is False
    assert [(b["version"], b["status"]) for b in rows(db, "SELECT version, status FROM user_rule_briefs ORDER BY version")] == [(1, "superseded"), (2, "current")]
    with pytest.raises(UserRuleError, match="replaced"):
        user_rules.write_brief(rule_id=made["rule_id"], brief=BRIEF, caller=RESEARCHER, db=db)


def test_a_requirement_that_is_incomplete_or_asks_for_what_hokma_does_not_have_is_refused(db):
    bank_id = make_bank(db)
    ask = lambda data, **kw: user_rules.write_brief(bank_id=bank_id, slug=kw.get("slug", SLUG), title="t", brief=data, caller=RESEARCHER, db=db)  # noqa: E731
    for bad in ({k: v for k, v in BRIEF.items() if k != "cases"}, brief(cases=BRIEF["cases"][:2]), brief(extra=1), brief(timeframe="five minutes"), brief(inputs=[]),
                brief(cases=[{"name": "x", "expect": "buy", "closes": [1, 2]}] * 3), brief(params=[{"name": "sizer_n", "type": "int", "value": 1, "origin": "proposed"}])):
        with pytest.raises(UserRuleError) as raised:
            ask(bad)
        assert raised.value.code == "bad_input"
    with pytest.raises(UserRuleError) as raised:
        ask(brief(inputs=["close", "order_book_imbalance"]))
    assert raised.value.code == "blocked" and "order_book_imbalance" in raised.value.message
    with pytest.raises(UserRuleError) as raised:
        ask(brief(summary="Sells a covered call every month against the shares that the owner holds."))
    assert raised.value.code == "blocked" and "premium" in raised.value.message
    with pytest.raises(UserRuleError):
        user_rules.write_brief(bank_id=bank_id, slug="Bad Slug", title="t", brief=BRIEF, caller=RESEARCHER, db=db)
    assert rows(db, "SELECT * FROM user_rules") == []                       # nothing of the refused ones was kept
    bank.set_status(bank_id, "not_working", reason="the rule depends on a data feed that we do not have", actor="iris", db=db)
    with pytest.raises(UserRuleError, match="does not work"):
        ask(BRIEF)


def test_without_a_current_requirement_no_code_is_saved_even_when_the_files_exist(db):
    bank_id = make_bank(db)
    with db.write() as u:
        rule_id = u.session.insert("user_rules", {"slug": SLUG, "kind": "strategy", "title": "t", "bank_id": bank_id, "component": "strategy", "created_by": RESEARCHER})
    put_files(SLUG)
    with pytest.raises(UserRuleError) as raised:
        user_rules.save_code(rule_id=rule_id, caller=CODER, db=db)
    assert raised.value.code == "blocked" and "no requirement" in raised.value.message
    assert rows(db, "SELECT * FROM user_rule_versions") == []


def test_green_code_is_saved_as_a_draft_with_its_hash_its_read_only_copy_and_its_requirement(db, env):
    bank_id, made, out = saved(db)
    assert out["saved"] is True and out["status"] == "draft" and out["registry_id"] == "u_inside_bar_v1" and out["tests"] == 4
    [row] = rows(db, "SELECT * FROM user_rule_versions")
    assert row["brief_id"] == made["brief_id"] and row["code_text"] == STRATEGY and row["code_sha256"] == out["code_sha256"] and row["status"] == "draft"
    assert json.loads(row["test_json"])["ok"] is True and json.loads(row["manifest_json"])["schema"][0]["name"] == "min_range"
    assert json.loads(row["cases_json"])["test_file"] == TEST and (row["origin_desk_id"], row["created_by"]) == (None, CODER)
    copy = user_rules.copy_folder(SLUG, 1, out["code_sha256"]) / "strategy.py"
    assert copy.read_text(encoding="utf-8") == STRATEGY and not os.access(copy, os.W_OK)
    assert [e["to_status"] for e in rows(db, "SELECT to_status FROM user_rule_events")] == ["draft"]
    again = user_rules.save_code(rule_id=made["rule_id"], caller=CODER, db=db)
    assert again["duplicate"] is True and again["rule_version_id"] == out["rule_version_id"] and len(rows(db, "SELECT * FROM user_rule_versions")) == 1


def test_red_code_and_a_static_violation_are_not_saved_and_say_why_and_a_dry_run_stores_nothing(db):
    bank_id, made = setup_rule(db)
    put_files(SLUG, test=TEST.replace('assert decisions[-1].action == "short"', 'assert decisions[-1].action == "long"'))
    red = user_rules.save_code(rule_id=made["rule_id"], caller=CODER, db=db)
    assert red["saved"] is False and red["ok"] is False and "FAIL test_a_close_below_the_inside_bar_goes_short" in red["output"]
    put_files(SLUG, strategy="import os\n" + STRATEGY)
    static = user_rules.save_code(rule_id=made["rule_id"], caller=CODER, db=db)
    assert static["saved"] is False and static["static"][0]["rule"] == "import_not_allowed" and "STATIC strategy.py" in static["output"]
    put_files(SLUG)
    dry = user_rules.save_code(rule_id=made["rule_id"], caller=CODER, dry_run=True, db=db)
    assert dry == {**dry, "saved": False, "ok": True, "tests": 4} and "RESULT green" in dry["output"]
    assert rows(db, "SELECT * FROM user_rule_versions") == [] and not (user_rules.copies_dir() / SLUG).exists()
    with pytest.raises(UserRuleError, match="does not exist"):
        user_rules.save_code(rule_id=made["rule_id"], caller=CODER, db=db) if (user_rules.drafts_dir() / SLUG / "strategy.py").unlink() is None else None


def test_a_new_version_gets_the_next_number_and_a_new_copy(db):
    bank_id, made, first = saved(db)
    put_files(SLUG, strategy=STRATEGY.replace("min_range: float = 0.0", "min_range: float = 0.0\n    # second version"))
    second = user_rules.save_code(rule_id=made["rule_id"], caller=CODER, db=db)
    assert (second["version"], second["registry_id"]) == (2, "u_inside_bar_v2") and second["code_sha256"] != first["code_sha256"]
    assert user_rules.copy_folder(SLUG, 1, first["code_sha256"]).is_dir() and user_rules.copy_folder(SLUG, 2, second["code_sha256"]).is_dir()


def test_a_rejection_needs_findings_marks_the_version_failed_and_the_third_one_blocks_the_rule(db):
    bank_id, made, out = saved(db)
    with pytest.raises(UserRuleError, match="needs its findings"):
        user_rules.verdict(rule_version_id=out["rule_version_id"], verdict="rejected", findings=[], reviewer=REVIEWER, db=db)
    finding = [{"severity": "major", "line": 14, "message": "the short side does not check the minimum range"}]
    first = user_rules.verdict(rule_version_id=out["rule_version_id"], verdict="rejected", findings=finding, reviewer=REVIEWER, db=db)
    assert (first["status"], first["attempts_left"]) == ("failed", 2)
    assert rows(db, "SELECT status FROM user_rule_versions")[0]["status"] == "failed"
    with pytest.raises(UserRuleError, match="only a draft"):
        user_rules.verdict(rule_version_id=out["rule_version_id"], verdict="approved", findings=[], reviewer=REVIEWER, db=db)
    for n in (2, 3):
        put_files(SLUG, strategy=STRATEGY + f"\n# change {n}\n")
        again = user_rules.save_code(rule_id=made["rule_id"], caller=CODER, db=db)
        last = user_rules.verdict(rule_version_id=again["rule_version_id"], verdict="rejected", findings=finding, reviewer=REVIEWER, db=db)
    assert last["attempts_left"] == 0
    put_files(SLUG, strategy=STRATEGY + "\n# change 4\n")
    with pytest.raises(UserRuleError) as raised:
        user_rules.save_code(rule_id=made["rule_id"], caller=CODER, db=db)
    assert raised.value.code == "blocked" and "3 times" in raised.value.message


def test_an_approval_with_a_blocker_or_a_major_finding_is_refused(db):
    bank_id, made, out = saved(db)
    for severity in ("blocker", "major"):
        with pytest.raises(UserRuleError, match="blocker or a major"):
            user_rules.verdict(rule_version_id=out["rule_version_id"], verdict="approved", findings=[{"severity": severity, "message": "no"}], reviewer=REVIEWER, db=db)
    with pytest.raises(UserRuleError):
        user_rules.verdict(rule_version_id=out["rule_version_id"], verdict="approved", findings=[{"severity": "huge", "message": "no"}], reviewer=REVIEWER, db=db)
    assert rows(db, "SELECT * FROM user_rule_reviews") == []


def test_an_approval_generates_the_version_and_creates_the_bank_version_the_spec_and_the_link_together(db):
    bank_id, made, out = saved(db)
    told = []
    result = approve(db, out, notify=told.append)
    assert told == [result] and (result["status"], result["bank_version"], result["strategy_id"], result["spec_version"]) == ("generated", 2, "u_inside_bar_v1", 1)
    assert rows(db, "SELECT status FROM user_rule_versions")[0]["status"] == "generated"
    [review] = rows(db, "SELECT verdict, reviewer, code_sha256, brief_id FROM user_rule_reviews")
    assert (review["verdict"], review["reviewer"], review["code_sha256"], review["brief_id"]) == ("approved", REVIEWER, out["code_sha256"], made["brief_id"])
    versions = rows(db, "SELECT version, strategy_id, spec_version, needs_rule_json, origin_desk_id, created_by, derived_from FROM strategy_bank_versions ORDER BY version")
    assert [(v["version"], v["strategy_id"], v["needs_rule_json"]) for v in versions] == [(1, None, versions[0]["needs_rule_json"]), (2, "u_inside_bar_v1", "[]")]
    assert versions[0]["needs_rule_json"] != "[]" and versions[1]["derived_from"] is not None and versions[1]["origin_desk_id"] is None
    [row] = rows(db, "SELECT strategy_id, version, line, status, spec_yaml FROM strategy_specs")
    assert (row["strategy_id"], row["version"], row["status"]) == ("u_inside_bar_v1", 1, "draft") and "alpha: u_inside_bar_v1" in row["spec_yaml"] and "sizer: fixed_contracts" in row["spec_yaml"]
    [spec_link] = rows(db, "SELECT strategy_id, spec_version, code_sha256 FROM strategy_spec_rules")
    assert (spec_link["strategy_id"], spec_link["code_sha256"]) == ("u_inside_bar_v1", out["code_sha256"])
    assert bank.get(bank_id, db=db)["status"] == "draft"
    got = bank.get(bank_id, db=db)["version"]
    assert (got["strategy_id"], got["code"], got["agent_reviewed"], got["owner_reviewed"], got["origin"]) == ("u_inside_bar_v1", None, True, False, {"kind": "hokma"})
    [listed] = bank.list_bank(db=db)["rows"]
    assert (listed["code"], listed["agent_reviewed"], listed["owner_reviewed"], listed["origin"], listed["needs_new_rule"]) == (None, True, False, {"kind": "hokma"}, False)
    assert bank.list_bank(origin="desk", db=db)["rows"] == [] and len(bank.list_bank(origin="hokma", db=db)["rows"]) == 1


def test_the_origin_is_the_desk_where_it_was_created_and_never_something_the_agent_says(db):
    bank_id = make_bank(db)
    made = user_rules.write_brief(bank_id=bank_id, slug=SLUG, title="Inside bar breakout", brief=BRIEF, caller=RESEARCHER, origin=(7, "NQ desk"), db=db)
    put_files(SLUG)
    out = user_rules.save_code(rule_id=made["rule_id"], caller=CODER, db=db)
    approve(db, out)
    [row] = rows(db, "SELECT origin_desk_id, origin_desk_name FROM user_rule_versions")
    assert (row["origin_desk_id"], row["origin_desk_name"]) == (7, "NQ desk")
    got = bank.get(bank_id, db=db)["version"]
    assert got["origin"] == {"kind": "desk", "desk_id": 7, "desk_name": "NQ desk"}
    assert bank.list_bank(origin="desk", db=db)["rows"][0]["origin"]["desk_name"] == "NQ desk"
    assert user_rules.list_code(origin="desk", db=db)["rows"][0]["origin"]["desk_id"] == 7 and user_rules.list_code(origin="hokma", db=db)["rows"] == []


def test_only_an_approved_intact_version_loads_and_the_registry_marks_it_as_generated(db):
    bank_id, made, out = saved(db)
    registry_id = out["registry_id"]
    with pytest.raises(UserRuleError, match="does not load"):                   # a draft
        user_rules.load_class(registry_id, db=db)
    assert registry_id not in factory_spec.default_registry().entries
    approve(db, out)
    registry = factory_spec.default_registry()
    entry = registry.get(registry_id)
    assert entry.kind == "strategy" and entry.source == f"user:{registry_id}" and "min_range" in entry.cls.SCHEMA.names and "sizer" in entry.cls.SCHEMA.names
    listed = {r["id"]: r for r in factory_registry.factory_list(registry)}
    assert listed[registry_id]["generated"] is True and "generated" not in listed["ema_cross"]
    assert registry.build(registry_id, {"min_range": 1.0, "sizer_n": 2}).values["sizer_n"] == 2


def test_a_file_edited_by_hand_stops_loading_and_cannot_be_approved(db):
    bank_id, made, out = saved(db)
    copy = user_rules.copy_folder(SLUG, 1, out["code_sha256"]) / "strategy.py"
    os.chmod(copy, 0o644)
    copy.write_text(STRATEGY + "\n# a change by hand\n", encoding="utf-8")
    with pytest.raises(UserRuleError, match="not the code the base keeps"):
        approve(db, out)
    copy.write_text(STRATEGY, encoding="utf-8")
    approve(db, out)
    assert "u_inside_bar_v1" in factory_spec.default_registry().entries
    os.chmod(copy, 0o644)
    copy.write_text(STRATEGY.replace("min_range: float = 0.0", "min_range: float = 5.0"), encoding="utf-8")
    assert "u_inside_bar_v1" not in factory_spec.default_registry().entries      # the next call of the registry no longer finds it
    with pytest.raises(UserRuleError, match="not the code the base keeps"):
        user_rules.load_class("u_inside_bar_v1", db=db)


def test_a_failed_version_never_loads_and_the_base_refuses_to_generate_it_without_a_verdict(db):
    bank_id, made, out = saved(db)
    user_rules.verdict(rule_version_id=out["rule_version_id"], verdict="rejected", findings=[{"severity": "major", "message": "wrong"}], reviewer=REVIEWER, db=db)
    assert "u_inside_bar_v1" not in factory_spec.default_registry().entries
    bank_id, made, out = (bank_id, made, out)
    with pytest.raises(Exception, match="code reviewer has not approved"):
        with db.write() as u:
            u.session.execute("UPDATE user_rule_versions SET status = 'generated' WHERE id = :i", {"i": out["rule_version_id"]})


def test_the_owner_decides_after_the_reviewer_and_the_gates_of_the_proposal_and_of_real_money_wait_for_the_owner(db):
    bank_id, made, out = saved(db)
    registry_id = out["registry_id"]
    with pytest.raises(UserRuleError):                                          # the owner cannot decide on a draft
        user_rules.owner_decision(rule_version_id=out["rule_version_id"], approve=True, db=db)
    approve(db, out)
    for gate in (user_rules.propose_gate, user_rules.live_gate):
        with pytest.raises(UserRuleError, match="owner"):
            gate(registry_id, db=db)
    user_rules.propose_gate("ema_cross", db=db)                                 # a piece of the factory is not asked
    with pytest.raises(factory_spec.BadSpec, match="owner"):
        factory_spec.propose(registry_id, 1, db=db)
    decided = user_rules.owner_decision(rule_version_id=out["rule_version_id"], approve=True, note="read it", db=db)
    assert decided["status"] == "owner_reviewed"
    for gate in (user_rules.propose_gate, user_rules.live_gate):
        gate(registry_id, db=db)
    [row] = rows(db, "SELECT status, reviewed_by, reviewed_sha256, code_sha256, review_note FROM user_rule_versions")
    assert (row["status"], row["reviewed_by"], row["review_note"]) == ("owner_reviewed", "owner", "read it") and row["reviewed_sha256"] == row["code_sha256"]
    assert factory_spec.propose(registry_id, 1, db=db).status == "proposed"
    assert registry_id in factory_spec.default_registry().entries
    with pytest.raises(UserRuleError):
        user_rules.owner_decision(rule_version_id=out["rule_version_id"], approve=False, db=db)


def test_the_owner_can_say_no_and_that_version_is_rejected_and_never_proposed(db):
    bank_id, made, out = saved(db)
    approve(db, out)
    assert user_rules.owner_decision(rule_version_id=out["rule_version_id"], approve=False, note="I do not trade breakouts", db=db)["status"] == "rejected"
    with pytest.raises(UserRuleError, match="owner"):
        user_rules.propose_gate(out["registry_id"], db=db)
    assert out["registry_id"] not in factory_spec.default_registry().entries


def test_the_green_run_of_the_checks_stands_for_the_signal_and_size_tests_of_the_spec_and_only_for_generated_ids(db):
    bank_id, made, out = saved(db)
    registry_id = out["registry_id"]
    approve(db, out)
    registry = factory_spec.default_registry()
    saved_spec = factory_spec.load(registry_id, 1, db=db)
    parsed = factory_spec.parse_spec(saved_spec.spec_yaml, registry)
    assert parsed.signal_tests == () and parsed.size_tests == () and parsed.spec_hash == saved_spec.spec_hash
    assert factory_spec.require_tests(parsed, registry) == saved_spec.spec_hash and factory_spec.gate_backtest(registry_id, 1, registry=registry, db=db) == saved_spec.spec_hash
    plain = factory_spec.parse_spec("strategy_id: bare\nline: DT\nalpha: ema_cross\nparams: {fast: 3, slow: 5}\n", registry)
    with pytest.raises(factory_spec.MissingTests):                               # the rule of the pieces of the factory did not change
        factory_spec.require_tests(plain, registry)
    with db.write() as u:
        u.session.execute("UPDATE user_rule_versions SET test_json = :t", {"t": json.dumps({"ok": False})})
    with pytest.raises(factory_spec.TestsFailed):
        factory_spec.require_tests(parsed, registry)


def test_a_yaml_file_cannot_declare_an_id_of_a_generated_strategy(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text("id: u_sneaky_v1\nkind: alpha\nclass: hokma.factory.alphas.ema_cross.EmaCross\n", encoding="utf-8")
    with pytest.raises(factory_registry.RegistryError, match="u_"):
        factory_registry.Registry().load_file(path)
