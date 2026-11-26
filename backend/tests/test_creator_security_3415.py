"""The creator of strategies, the security round of task 3415: roles checked in the server and not only in the hook, a folder with only the two files, and the registry of
real money that holds only what the owner approved. Each test first shows the refusal and then the road that stays open."""
import pytest
from code_support import BRIEF, CODER, REVIEWER, RESEARCHER, make_bank, put_files, rows
from code_support import db, env, pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)
from e2e_h4.support import caller, failed, ok

from hokma.core.config import hokma_home
from hokma.factory import spec as factory_spec, user_rules
from hokma.factory.user_rules import UserRuleError

SLUG = "inside_bar"


def road(db):
    bank_id = make_bank(db)
    made = user_rules.write_brief(bank_id=bank_id, slug=SLUG, title="Inside bar breakout", brief=BRIEF, caller=RESEARCHER, db=db)
    put_files(SLUG)
    return bank_id, made, user_rules.save_code(rule_id=made["rule_id"], caller=CODER, db=db)


def test_the_requirement_is_written_only_by_the_researcher_in_the_server_itself(db):
    bank_id = make_bank(db)
    for who in (CODER, REVIEWER, "hokma", "agent", "joel"):
        with pytest.raises(UserRuleError, match="belongs to strategy-researcher"):
            user_rules.write_brief(bank_id=bank_id, slug=SLUG, title="t", brief=BRIEF, caller=who, db=db)
        refused = failed("strategy_brief_write", {"bank_id": bank_id, "slug": SLUG, "title": "t", "brief": BRIEF}, caller(who))
        assert refused["error_code"] == "blocked", who
    assert rows(db, "SELECT id FROM user_rules") == []
    assert ok("strategy_brief_write", {"bank_id": bank_id, "slug": SLUG, "title": "t", "brief": BRIEF}, caller(RESEARCHER))["rule_id"]


def test_the_code_is_saved_only_by_the_coder_in_the_server_itself(db):
    bank_id = make_bank(db)
    made = user_rules.write_brief(bank_id=bank_id, slug=SLUG, title="t", brief=BRIEF, caller=RESEARCHER, db=db)
    put_files(SLUG)
    for who in (RESEARCHER, REVIEWER, "hokma", "agent"):
        with pytest.raises(UserRuleError, match="belongs to strategy-coder"):
            user_rules.save_code(rule_id=made["rule_id"], caller=who, db=db)
        assert failed("strategy_code_save", {"rule_id": made["rule_id"]}, caller(who))["error_code"] == "blocked", who
    assert rows(db, "SELECT id FROM user_rule_versions") == []
    assert ok("strategy_code_save", {"rule_id": made["rule_id"]}, caller(CODER))["saved"] is True


def test_the_verdict_is_given_only_by_the_reviewer_and_never_by_the_one_that_saved_the_version(db):
    _bank, _made, out = road(db)
    version = out["rule_version_id"]
    for who in (CODER, RESEARCHER, "hokma", "agent"):
        with pytest.raises(UserRuleError, match="belongs to strategy-reviewer"):
            user_rules.verdict(rule_version_id=version, verdict="approved", findings=[], reviewer=who, db=db)
        assert failed("strategy_code_verdict", {"rule_version_id": version, "verdict": "approved"}, caller(who))["error_code"] == "blocked", who
    with db.write() as u:                                              # even when the saver and the reviewer are one name, the verdict is refused
        u.session.execute("UPDATE user_rule_versions SET created_by = :r WHERE id = :i", {"r": REVIEWER, "i": version})
    with pytest.raises(UserRuleError, match="never gives its verdict"):
        user_rules.verdict(rule_version_id=version, verdict="approved", findings=[], reviewer=REVIEWER, db=db)
    assert rows(db, "SELECT status FROM user_rule_versions")[0]["status"] == "draft" and rows(db, "SELECT id FROM user_rule_reviews") == []
    with db.write() as u:
        u.session.execute("UPDATE user_rule_versions SET created_by = :r WHERE id = :i", {"r": CODER, "i": version})
    assert ok("strategy_code_verdict", {"rule_version_id": version, "verdict": "approved"}, caller(REVIEWER))["status"] == "generated"


def test_a_folder_with_anything_besides_the_two_files_is_not_saved_and_not_checked(db):
    bank_id = make_bank(db)
    made = user_rules.write_brief(bank_id=bank_id, slug=SLUG, title="t", brief=BRIEF, caller=RESEARCHER, db=db)
    folder = put_files(SLUG)
    for plant in ("notes.md", "helper.py", ".hidden", "__pycache__"):
        path = folder / plant
        path.mkdir() if plant == "__pycache__" else path.write_text("x = 1\n", encoding="utf-8")
        for dry in (True, False):
            with pytest.raises(UserRuleError, match="holds only strategy.py and test_strategy.py"):
                user_rules.save_code(rule_id=made["rule_id"], caller=CODER, dry_run=dry, db=db)
        assert failed("strategy_code_save", {"rule_id": made["rule_id"]}, caller(CODER))["error_code"] == "blocked"
        path.rmdir() if plant == "__pycache__" else path.unlink()
    link = folder / "extra.py"
    link.symlink_to(folder / "strategy.py")
    with pytest.raises(UserRuleError, match="extra.py"):
        user_rules.save_code(rule_id=made["rule_id"], caller=CODER, db=db)
    link.unlink()
    assert rows(db, "SELECT id FROM user_rule_versions") == []
    assert user_rules.save_code(rule_id=made["rule_id"], caller=CODER, dry_run=True, db=db)["ok"] is True
    assert sorted(p.name for p in folder.iterdir()) == ["strategy.py", "test_strategy.py"]          # the checks leave nothing behind
    assert user_rules.save_code(rule_id=made["rule_id"], caller=CODER, db=db)["saved"] is True


def test_the_registry_of_real_money_holds_a_generated_strategy_only_after_the_owner_approved_this_code(db):
    _bank, _made, out = road(db)
    registry_id = out["registry_id"]
    user_rules.verdict(rule_version_id=out["rule_version_id"], verdict="approved", findings=[], reviewer=REVIEWER, db=db)
    assert registry_id in factory_spec.default_registry().entries                  # the optimizer and the backtest may run it
    assert registry_id not in factory_spec.live_registry().entries                 # a desk may not
    with pytest.raises(UserRuleError, match="owner's approval"):
        user_rules.live_gate(registry_id, db=db)
    user_rules.owner_decision(rule_version_id=out["rule_version_id"], approve=True, db=db)
    assert registry_id in factory_spec.live_registry().entries
    user_rules.live_gate(registry_id, db=db)
    assert "ema_cross" in factory_spec.live_registry().entries                       # the pieces of the kit are there as always
    assert hokma_home().is_dir()
