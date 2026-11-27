"""strategy_brief_write with a card of the bank whose slug is long or starts with a number (task 3427): the rule slug is derived, never refused."""
from code_support import BRIEF, RESEARCHER, db, env, make_bank, pg_admin_url, pg_template, pg_url_migrated, rows  # noqa: F401  (fixtures)
from e2e_h4.support import call, caller, ok

from hokma.factory import user_rules


def test_rule_slug_is_valid_for_long_digit_first_and_odd_card_slugs():
    cases = ["a-very-long-title-about-an-inside-bar-breakout-on-nq-with-a-trailing-stop-and-filters", "5-minute-opening-range-breakout", "123", "x", "---", "ok-title"]
    seen = set()
    for n, card in enumerate(cases, 1):
        slug = user_rules.rule_slug(card, n)
        assert user_rules.SLUG.match(slug) and not slug.endswith("_") and len(slug) <= 40
        seen.add(slug)
    assert len(seen) == len(cases)
    assert user_rules.rule_slug("5-minute-opening-range-breakout", 7).startswith("s_5_minute")
    long = "a-very-long-title-about-an-inside-bar-breakout-on-nq-with-a-trailing-stop-and-filters"
    assert user_rules.rule_slug(long, 12).endswith("_12") and user_rules.rule_slug(long, 13) != user_rules.rule_slug(long, 12)


def test_the_rejected_slug_is_named_in_the_error():
    try:
        user_rules._slug("9bad")
    except user_rules.UserRuleError as exc:
        assert "'9bad'" in str(exc)
    else:
        raise AssertionError("not refused")


def test_a_long_title_and_a_title_that_starts_with_a_number_register_their_first_requirement(db):
    for title in ("An inside bar breakout on the opening range of NQ with a trailing stop and two filters of volume", "5 minute opening range breakout"):
        bank_id = make_bank(db, title)
        made = ok("strategy_brief_write", {"bank_id": bank_id, "title": title, "brief": BRIEF}, caller(RESEARCHER))
        assert user_rules.SLUG.match(made["slug"]) and made["folder"].startswith(f"service/strategies/STR-{bank_id:03d}-")
    names = [r["slug"] for r in rows(db, "SELECT slug FROM user_rules")]
    assert len(names) == 2 and len(set(names)) == 2
    answer, is_error = call("strategy_brief_write", {"bank_id": bank_id, "slug": "9 bad slug!", "title": title, "brief": BRIEF}, caller(RESEARCHER))
    assert is_error and "already a rule named" in answer["message"] and "slug" not in answer["message"].split("named")[0]   # the slug argument is ignored: a bad one is not what fails
