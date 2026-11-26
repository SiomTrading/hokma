"""Rules with a mode and a scope (SPEC-026, piece 3390a, task 3390): restriction or notice, on_trip, account and desk-in-account rules,
the list of assets as a notice, proposal against direct edit, the close of the day lifted by hand, and the migration 0027 with its way back."""
import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest
from account_links_support import link_principal
from dbsupport import env, take_back_the_migrations_after_0033  # noqa: F401  (fixture)
from desks_support import db, desks_db, make_desk  # noqa: F401  (fixtures)
from test_desk_rules import AGENT, NQ, OWNER, SPY, book, config, ctx, legacy, prop

from hokma.broker import account_rules, day_close, desk_rules, rule_modes, service
from hokma.broker.service import BrokerError
from hokma.db import migrate
from hokma.db.downgrade import AccountRulesExist, rollback_0027, rollback_0028, rollback_0029, rollback_0030, rollback_0031, rollback_0032, rollback_0033
from hokma.db.migrate import MIGRATIONS_DIR
from hokma.exec.guards import pipeline, registry
from hokma.exec.guards.limits_guard import LimitsGuard


def _only(cfg):
    return [g for g in registry.build(cfg) if g.name in cfg["guards"]]


def _desk_with_account(db, name="Mesa", **kw):
    desk = make_desk(db, name=name, **kw)
    acc = service.create_account(1, f"Cuenta {name}", 5000, db)["id"]
    with db.write() as u:
        link_principal(u, desk["id"], acc)
    return desk["id"], acc


def _run(cfg, p, **kw):
    only = [g for g in registry.build(cfg) if g.name in cfg["guards"]]            # the fixed guards need a heartbeat of the monitor: not what is tested here
    return pipeline.run(only, ctx(p, cfg, **kw))


# ---- the mode is asked and saved --------------------------------------------------------------------------------------

def test_a_control_is_not_saved_without_its_mode_and_a_loss_asks_what_happens_at_the_limit(desks_db):
    d = make_desk(desks_db)["id"]
    with pytest.raises(BrokerError) as no_mode:
        desk_rules.set_control(d, "size_premium", 500, OWNER, db=desks_db)
    assert no_mode.value.status == 400 and "mode_required" in no_mode.value.text
    with pytest.raises(BrokerError) as no_trip:
        desk_rules.set_control(d, "daily_loss", 400, OWNER, mode="restriction", db=desks_db)
    assert "on_trip_required" in no_trip.value.text and "block_and_flatten_all" in no_trip.value.text
    assert config(desks_db, d) == {"guards": []}                                           # nothing was saved
    shown = desk_rules.set_control(d, "daily_loss", 400, OWNER, mode="restriction", on_trip="block_and_flatten", db=desks_db)
    rule = next(r for r in shown["active"] if r["id"] == "daily_loss")
    assert rule["mode"] == "restriction" and rule["on_trip"] == "block_and_flatten" and rule["scope"] == "desk"
    notice = desk_rules.set_control(d, "size_premium", 500, OWNER, mode="notice", db=desks_db)
    assert next(r for r in notice["active"] if r["id"] == "size_premium")["timeout_min"] == 30       # the default wait
    with pytest.raises(BrokerError):
        desk_rules.set_control(d, "cooldown", 5, OWNER, mode="notice", on_trip="block_entries", db=desks_db)   # on_trip is only for a restriction


def test_editing_the_value_keeps_the_mode_and_set_mode_changes_it(desks_db):
    d = make_desk(desks_db)["id"]
    desk_rules.set_control(d, "cooldown", 5, OWNER, mode="notice", timeout_min=10, db=desks_db)
    desk_rules.set_control(d, "cooldown", 8, OWNER, db=desks_db)
    assert config(desks_db, d)["rule_modes"]["cooldown"] == {"mode": "notice", "timeout_min": 10}
    desk_rules.set_mode(d, "cooldown", OWNER, mode="restriction", db=desks_db)
    assert config(desks_db, d)["rule_modes"]["cooldown"] == {"mode": "restriction"}
    desk_rules.remove_rule(d, "cooldown", OWNER, desks_db)
    assert "rule_modes" not in config(desks_db, d)


# ---- the guards obey the mode ------------------------------------------------------------------------------------------

def test_a_restriction_blocks_and_the_same_rule_as_a_notice_asks_the_owner(desks_db):
    cfg = {"guards": ["account"], "funding": {"daily_loss_usd": 400.0}}
    hit = {"book": book(day_pnl=-500)}
    assert _run(cfg, prop(), **hit).verdict == "BLOCK"
    soft = _run({**cfg, "rule_modes": {"daily_loss": {"mode": "notice", "timeout_min": 30}}}, prop(), **hit)
    assert soft.verdict == "PASS" and soft.needs_owner is True
    still = _run({**cfg, "rule_modes": {"daily_loss": {"mode": "notice"}}}, prop(), book=book(day_pnl=None))
    assert still.verdict == "BLOCK"                                                        # a number that cannot be measured never becomes a notice


def test_a_notice_never_hides_a_restriction_that_trips_at_the_same_time(desks_db):
    cfg = {"guards": ["account"], "funding": {"daily_loss_usd": 400.0, "trailing_drawdown_usd": 5000.0},
           "rule_modes": {"daily_loss": {"mode": "notice", "timeout_min": 30}}}
    both = {"book": book(day_pnl=-500, equity=990_000)}                    # the loss (notice) and the drawdown (restriction) both trip
    out = _run(cfg, prop(), **both)
    assert out.verdict == "BLOCK" and out.blocked_by.code == "trailing_drawdown"
    only_notice = _run(cfg, prop(), book=book(day_pnl=-500))
    assert only_notice.verdict == "PASS" and only_notice.needs_owner
    # the same with the list of assets in notice and a restriction of the account
    import dataclasses

    mixed = {"guards": ["whitelist", "account"], "funding": {"daily_loss_usd": 400.0}, "rule_modes": {"assets_only": {"mode": "notice"}}}
    c = dataclasses.replace(ctx(prop(SPY), mixed, book=book(day_pnl=-500)), assets=("NQ",))
    assert pipeline.run(_only(mixed), c).blocked_by.code == "daily_loss"


def test_the_list_of_assets_in_notice_does_not_block_and_in_restriction_does(desks_db):
    cfg = {"guards": ["whitelist"], "rule_modes": {"assets_only": {"mode": "notice", "timeout_min": 30}}}
    c = ctx(prop(SPY), cfg)
    import dataclasses

    c = dataclasses.replace(c, assets=("NQ",))
    out = pipeline.run(_only(cfg), c)
    assert out.verdict == "PASS" and out.needs_owner
    hard = {"guards": ["whitelist"], "rule_modes": {"assets_only": {"mode": "restriction"}}}
    assert pipeline.run(_only(hard), dataclasses.replace(ctx(prop(SPY), hard), assets=("NQ",))).verdict == "BLOCK"


def test_a_size_limit_in_notice_does_not_cut_the_order_and_asks(desks_db):
    cfg = {"guards": ["limits"], "funding": {"max_contracts": 1}, "rule_modes": {"size_contracts": {"mode": "notice", "timeout_min": 30}}}
    p = prop(qty="3")
    out = LimitsGuard().check(ctx(p, cfg))
    assert out.verdict == "pass" and out.code == "needs_owner"
    assert LimitsGuard().check(ctx(p, {"guards": ["limits"], "funding": {"max_contracts": 1}})).verdict != "pass"


def test_a_new_desk_with_assets_has_no_forced_block(desks_db):
    d = make_desk(desks_db, assets=["NQ"])["id"]
    cfg = config(desks_db, d)
    assert cfg["rule_modes"]["assets_only"]["mode"] == "notice"
    shown = desk_rules.view(d, desks_db)
    assert next(r for r in shown["active"] if r["id"] == "assets_only")["mode"] == "notice"


# ---- proposal of an agent against direct edit of the owner ---------------------------------------------------------------

def test_a_proposal_of_a_control_needs_the_answers_and_an_agent_cannot_write_directly(desks_db):
    d = make_desk(desks_db)["id"]
    with pytest.raises(BrokerError) as e:
        desk_rules.propose(d, "at most 500 dollars of premium", db=desks_db)
    assert "mode_required" in e.value.text
    with pytest.raises(BrokerError) as e2:
        desk_rules.propose(d, "daily loss of 300 dollars", mode="restriction", db=desks_db)
    assert "on_trip_required" in e2.value.text
    item = desk_rules.propose(d, "daily loss of 300 dollars", mode="restriction", on_trip="block_entries", db=desks_db)
    assert item["mode"] == "restriction" and item["on_trip"] == "block_entries"
    for call in (lambda: desk_rules.set_control(d, "daily_loss", 1, AGENT, mode="restriction", on_trip="block_entries", db=desks_db),
                 lambda: desk_rules.set_mode(d, "daily_loss", AGENT, mode="notice", db=desks_db),
                 lambda: account_rules.set_rule(d, 1, "daily_loss", 1, AGENT, mode="notice", db=desks_db)):
        with pytest.raises(BrokerError) as denied:
            call()
        assert denied.value.status == 403
    with pytest.raises(BrokerError) as agent_limits:
        service.set_limits(d, {"rule_modes": {"daily_loss": {"mode": "notice"}}}, desks_db, tighten_only=True)
    assert agent_limits.value.status == 403


def test_a_proposal_that_turns_a_restriction_into_a_notice_loosens_and_asks_for_confirmation(desks_db):
    d = make_desk(desks_db)["id"]
    desk_rules.set_control(d, "daily_loss", 300, OWNER, mode="restriction", on_trip="block_entries", db=desks_db)
    soft = desk_rules.propose(d, "daily loss of 300 dollars", mode="notice", db=desks_db)
    with pytest.raises(BrokerError) as e:
        desk_rules.approve(d, soft["id"], OWNER, desks_db)
    assert e.value.status == 409 and e.value.code == "loosens_limit"
    desk_rules.approve(d, soft["id"], OWNER, desks_db, confirm_loosen=True)
    assert config(desks_db, d)["rule_modes"]["daily_loss"]["mode"] == "notice"
    # the owner edits by hand to the weakest and nothing is asked
    desk_rules.set_control(d, "daily_loss", 9000, OWNER, mode="restriction", on_trip="block_entries", db=desks_db)
    desk_rules.set_mode(d, "daily_loss", OWNER, mode="notice", db=desks_db)


# ---- rules of an account and of a desk inside an account -------------------------------------------------------------------

def test_a_rule_of_the_account_and_a_rule_of_this_desk_in_the_account(desks_db):
    d, acc = _desk_with_account(desks_db)
    other = make_desk(desks_db, name="Otra")["id"]
    account_rules.set_rule(d, acc, "daily_loss", 500, OWNER, mode="restriction", on_trip="block_and_flatten_all", db=desks_db)
    shown = account_rules.set_rule(d, acc, "daily_loss", 200, OWNER, mode="notice", only_desk=True, db=desks_db)          # made while the account is this desk's alone
    with desks_db.write() as u:
        link_principal(u, other, acc)                                                                                 # a second desk joins afterwards: a new desk rule would be refused (SPEC-026 v2, decision 1299)
    ids = sorted(r["id"] for r in shown["account_rules"])
    assert ids == [f"a{acc}:d{d}:daily_loss", f"a{acc}:daily_loss"]
    assert {r["scope"] for r in shown["account_rules"]} == {"account", "desk_account"}
    mine = {r["id"]: r for r in shown["account_rules"]}
    assert mine[f"a{acc}:daily_loss"]["on_trip"] == "block_and_flatten_all" and mine[f"a{acc}:d{d}:daily_loss"]["timeout_min"] == 30
    other_view = desk_rules.view(other, desks_db)
    assert [r["id"] for r in other_view["account_rules"]] == [f"a{acc}:daily_loss"]       # the other desk sees the account rule, not mine
    with pytest.raises(BrokerError):
        account_rules.set_rule(d, acc + 99, "daily_loss", 5, OWNER, mode="notice", db=desks_db)      # an account that is not the desk's
    account_rules.edit_rule(d, f"a{acc}:daily_loss", OWNER, value=450, db=desks_db)
    assert next(r for r in desk_rules.view(d, desks_db)["account_rules"] if r["id"] == f"a{acc}:daily_loss")["value"] == 450
    account_rules.remove_rule(d, f"a{acc}:daily_loss", OWNER, desks_db)
    assert [r["id"] for r in desk_rules.view(d, desks_db)["account_rules"]] == [f"a{acc}:d{d}:daily_loss"]


def test_an_agent_proposal_for_an_account_is_approved_into_the_account_and_loosening_asks(desks_db):
    d, acc = _desk_with_account(desks_db)
    account_rules.set_rule(d, acc, "daily_loss", 300, OWNER, mode="restriction", on_trip="block_entries", db=desks_db)
    item = desk_rules.propose(d, "daily loss of 900 dollars", mode="restriction", on_trip="block_entries", account_id=acc, db=desks_db)
    with pytest.raises(BrokerError) as e:
        desk_rules.approve(d, item["id"], OWNER, desks_db)
    assert e.value.code == "loosens_limit"
    desk_rules.approve(d, item["id"], OWNER, desks_db, confirm_loosen=True)
    shown = desk_rules.view(d, desks_db)
    assert shown["account_rules"][0]["value"] == 900 and shown["proposed"] == []
    assert "daily_loss" not in {r["id"] for r in shown["active"]}                            # it is not a rule of the desk


# ---- the close of the day --------------------------------------------------------------------------------------------------

def test_a_closed_account_blocks_openings_and_not_closings_and_the_owner_lifts_it_with_a_record(desks_db):
    d, acc = _desk_with_account(desks_db)
    day_close.close_day(acc, "daily_loss", 520, "block_entries", desks_db)
    cfg = {"guards": ["account"]}
    from test_desk_rules import ctx as make_ctx
    import dataclasses

    closed = {"rule_id": "daily_loss"}
    c = dataclasses.replace(make_ctx(prop(), cfg), state={"account:day_closed": closed})
    assert pipeline.run(_only(cfg), c).blocked_by.code == "day_closed"
    closing = dataclasses.replace(make_ctx(prop(side="sell", effect="close"), cfg), state={"account:day_closed": closed},
                                  positions=(SimpleNamespace(contract_id=NQ, qty=Decimal(1)),))
    assert pipeline.run(_only(cfg), closing).verdict == "PASS"
    assert desk_rules.view(d, desks_db)["accounts"][0]["state"] == "day_closed"
    with pytest.raises(BrokerError) as agent:
        day_close.lift(acc, AGENT, confirm=True, db=desks_db)
    assert agent.value.status == 403
    with pytest.raises(BrokerError) as ask:
        day_close.lift(acc, OWNER, db=desks_db)
    assert ask.value.status == 409 and ask.value.code == "confirm_required" and "520" in ask.value.text
    assert day_close.lift(acc, OWNER, confirm=True, db=desks_db)["state"] == "open"
    events = day_close.events(acc, desks_db)
    assert [e["kind"] for e in events] == ["lifted", "closed"] and events[0]["who"] == "owner" and events[0]["rule_id"] == "daily_loss"
    with pytest.raises(BrokerError) as again:
        day_close.lift(acc, OWNER, confirm=True, db=desks_db)
    assert again.value.code == "not_closed"


def test_the_close_of_the_day_applies_even_when_the_desk_does_not_list_the_account_guard(desks_db):
    import dataclasses

    state = {"account:day_closed": {"rule_id": "daily_loss"}}
    c = dataclasses.replace(ctx(prop(), {"guards": []}), state=state)
    assert pipeline.run([], c).blocked_by.code == "day_closed"                              # no guard at all in the list
    closing = dataclasses.replace(ctx(prop(side="sell", effect="close"), {"guards": []}), state=state,
                                  positions=(SimpleNamespace(contract_id=NQ, qty=Decimal(1)),))
    assert pipeline.run([], closing).verdict == "PASS"


def test_the_state_reaches_the_guards_through_the_prefilter_context(desks_db):
    from hokma.exec import prefilter

    d, acc = _desk_with_account(desks_db)
    assert "account:day_closed" not in prefilter.load_context(d, prop(), db=desks_db, sheets={}).state
    day_close.close_day(acc, "daily_loss", 1, None, desks_db)
    assert prefilter.load_context(d, prop(), db=desks_db, sheets={}).state["account:day_closed"]["rule_id"] == "daily_loss"


# ---- the pieces of the model --------------------------------------------------------------------------------------------------

def test_an_implicit_restriction_turned_into_a_notice_is_a_loosening():
    from hokma.broker import rules

    before = {"funding": {"daily_loss_usd": 300.0}}                                          # no entry: a restriction by default
    after = {"funding": {"daily_loss_usd": 300.0}, "rule_modes": {"daily_loss": {"mode": "notice"}}}
    assert rules.loosened_limits(before, after) == ["rule_modes.daily_loss"]
    assert rules.loosened_limits(before, {**before, "rule_modes": {"daily_loss": {"mode": "restriction"}}}) == []
    assert rules.loosened_limits({}, {"rule_modes": {"daily_loss": {"mode": "notice"}}}) == []   # a new rule is not a loosening


def test_the_loosening_of_a_mode_is_a_loosening():
    from hokma.broker import rules

    a = {"rule_modes": {"daily_loss": {"mode": "restriction", "on_trip": "block_and_flatten_all"}}}
    assert rules.loosened_limits(a, {"rule_modes": {"daily_loss": {"mode": "notice"}}}) == ["rule_modes.daily_loss"]
    assert rules.loosened_limits(a, {"rule_modes": {"daily_loss": {"mode": "restriction", "on_trip": "block_entries"}}}) == ["rule_modes.daily_loss"]
    assert rules.loosened_limits({"rule_modes": {"x": {"mode": "notice"}}}, a) == []
    with pytest.raises(ValueError):
        rule_modes.clean_entry({"mode": "notice", "on_trip": "block_entries"})


# ---- migration 0027 and its way back ---------------------------------------------------------------------------------------------

def _at_26(env, tmp):
    """A database at the last version before this one, with desks that have a whitelist put by the system and by hand."""
    folder = env / "upto26"
    folder.mkdir()
    for f in MIGRATIONS_DIR.glob("00*.sql"):
        if int(f.name[:4]) <= 26:
            (folder / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    migrate(migrations_dir=folder, seed=False)
    return sqlite3.connect(str(tmp))


def test_0027_turns_the_list_forced_by_the_system_into_a_notice_keeps_the_ones_by_hand_and_goes_back(env):
    from hokma.db import db_path

    con = _at_26(env, db_path())
    con.execute("PRAGMA foreign_keys = OFF")
    cfgs = {1: {"guards": ["whitelist"]}, 2: {"guards": ["whitelist"], "rule_sources": {"assets_only": "hand"}}, 3: {"guards": []}, 4: {"max_premium_usd": 5},
            5: {"guards": ["whitelist"], "assets_only": True}}                  # the owner's own limit, no record of its source: by hand
    for desk_id, cfg in cfgs.items():
        con.execute("INSERT INTO desk_limits (desk_id, config) VALUES (?, ?)", (desk_id, json.dumps(cfg)))
    con.commit()
    con.close()
    assert 27 in migrate().applied
    con = sqlite3.connect(str(db_path()))
    got = {d: json.loads(c) for d, c in con.execute("SELECT desk_id, config FROM desk_limits")}
    assert got[1]["rule_modes"]["assets_only"]["mode"] == "notice" and got[2]["rule_modes"]["assets_only"]["mode"] == "restriction"
    assert "rule_modes" not in got[3] and "rule_modes" not in got[4]
    log = con.execute("SELECT desk_id, after_mode FROM rule_mode_migrations ORDER BY desk_id").fetchall()
    assert got[5]["rule_modes"]["assets_only"]["mode"] == "restriction"
    assert log == [(1, "notice"), (2, "restriction"), (5, "restriction")]
    assert migrate().applied == []
    con.close()
    take_back_the_migrations_after_0033(db_path())
    assert rollback_0033() == 32 and rollback_0032() == 31 and rollback_0031() == 30 and rollback_0030() == 29 and rollback_0029() == 28 and rollback_0028() == 27 and rollback_0027() == 26
    con = sqlite3.connect(str(db_path()))
    assert "rule_modes" not in json.loads(con.execute("SELECT config FROM desk_limits WHERE desk_id = 1").fetchone()[0])
    assert not con.execute("SELECT name FROM sqlite_master WHERE name IN ('account_rules','account_day_state','account_day_events','rule_mode_migrations')").fetchall()
    assert con.execute("SELECT count(*) FROM schema_version WHERE version = 27").fetchone()[0] == 0


def test_the_way_back_refuses_while_a_rule_of_an_account_exists(env, db):
    desk, acc = _desk_with_account(db)
    account_rules.set_rule(desk, acc, "daily_loss", 100, OWNER, mode="notice", db=db)
    from hokma.db import db_path

    take_back_the_migrations_after_0033(db_path())
    rollback_0033(db_path())
    rollback_0032(db_path())
    rollback_0031(db_path())
    rollback_0030(db_path())
    rollback_0029(db_path())
    rollback_0028(db_path())
    with pytest.raises(AccountRulesExist) as e:
        rollback_0027(db_path())
    assert e.value.count == 1


# ---- Vera's review of 3390a ---------------------------------------------------------------------------------------------

def test_a_size_limit_in_notice_that_cannot_be_measured_still_blocks(desks_db):
    import dataclasses

    cfg = {"guards": ["limits"], "max_premium_usd": 5, "rule_modes": {"size_premium": {"mode": "notice", "timeout_min": 30}}}
    nomark = dataclasses.replace(ctx(prop(SPY, line="SWO"), cfg), marks={})
    out = LimitsGuard().check(nomark)
    assert out.verdict == "block" and out.code == "limit_unpriced"
    out = pipeline.run(_only(cfg), nomark)
    assert out.verdict == "BLOCK" and out.blocked_by.code == "limit_unpriced"
    over = LimitsGuard().check(ctx(prop(SPY, qty="3", line="SWO"), cfg))                 # priced and over the cap: that one is the notice
    assert over.verdict == "pass" and over.code == "needs_owner"


def _fomc(*, unverified=False, word="fomc"):
    from datetime import timedelta

    from hokma.econ_calendar.windows import EventWindow
    from test_desk_rules import NOW

    return EventWindow(word, None if unverified else word.upper(), NOW - timedelta(minutes=10), NOW + timedelta(minutes=10), unverified)


def test_an_event_rule_in_notice_asks_by_its_real_id_and_a_restriction_or_an_unverified_calendar_still_blocks(desks_db):
    import dataclasses

    windows = [{"match": "FOMC", "minutes_before": 30, "minutes_after": 30}]
    cfg = {"guards": ["event_window"], "event_windows": windows, "rule_modes": {"events:fomc": {"mode": "notice", "timeout_min": 30}}}
    base = ctx(prop(), cfg)
    soft = pipeline.run(_only(cfg), dataclasses.replace(base, windows=(_fomc(),)))
    assert soft.verdict == "PASS" and soft.needs_owner is True
    both = {**cfg, "event_windows": windows + [{"match": "CPI", "minutes_before": 30, "minutes_after": 30}]}
    mixed = pipeline.run(_only(both), dataclasses.replace(ctx(prop(), both), windows=(_fomc(), _fomc(word="cpi"))))
    assert mixed.verdict == "BLOCK" and mixed.blocked_by.code == "event_window"            # CPI is a restriction and it is active too
    dark = pipeline.run(_only(cfg), dataclasses.replace(base, windows=(_fomc(unverified=True),)))
    assert dark.verdict == "BLOCK" and dark.blocked_by.code == "unverified_event_risk"
    hard = {**cfg, "rule_modes": {}}
    assert pipeline.run(_only(hard), dataclasses.replace(ctx(prop(), hard), windows=(_fomc(),))).verdict == "BLOCK"


def test_a_restriction_of_a_loss_or_a_drawdown_needs_on_trip_however_it_arrives(desks_db):
    from hokma.broker import rules

    for rule_id in ("daily_loss", "drawdown_pct"):
        with pytest.raises(ValueError, match="on_trip"):
            rules.validate_limits({"rule_modes": {rule_id: {"mode": "restriction"}}})
        with pytest.raises(ValueError, match="on_trip"):
            rule_modes.clean_modes({rule_id: {"mode": "restriction"}})
    ok = rules.validate_limits({"rule_modes": {"daily_loss": {"mode": "restriction", "on_trip": "block_entries"}, "cooldown": {"mode": "restriction"},
                                                "size_premium": {"mode": "notice"}, "daily_loss_pct": {"mode": "notice"}}})
    assert set(ok["rule_modes"]) == {"daily_loss", "cooldown", "size_premium", "daily_loss_pct"}


def test_a_threshold_by_percent_or_by_steps_changes_with_the_equity_without_editing_the_rule(desks_db):
    from hokma.broker import rules
    from hokma.exec.guards.account import AccountGuard

    steps = [{"up_to_equity": 200_000, "usd": 500}, {"up_to_equity": 600_000, "usd": 2000}, {"up_to_equity": 1_000_000, "usd": 5000}]
    cfg = {"daily_loss_steps": steps}
    hit = lambda equity, pnl, limits=cfg: AccountGuard().check(ctx(prop(), limits, book=book(equity=Decimal(equity), peak_equity=Decimal(equity), day_pnl=Decimal(pnl))))
    assert hit(150_000, -600).code == "daily_loss"                        # first step: 500
    assert hit(500_000, -600).verdict == "pass"                           # second step: 2000
    assert hit(500_000, -2100).code == "daily_loss"
    assert hit(5_000_000, -4900).verdict == "pass" and hit(5_000_000, -5100).code == "daily_loss"       # the last step covers what is left
    pct = {"max_daily_loss_pct": 1}
    assert hit(100_000, -1100, pct).code == "daily_loss_pct" and hit(1_000_000, -1100, pct).verdict == "pass"
    dd = {"drawdown_steps": steps}
    low = AccountGuard().check(ctx(prop(), dd, book=book(equity=Decimal(150_000), peak_equity=Decimal(150_700))))
    assert low.code == "trailing_drawdown"
    import dataclasses

    unknown = AccountGuard().check(dataclasses.replace(ctx(prop(), cfg, book=book(equity=None, day_pnl=Decimal(-1))), capital=Decimal(0)))
    assert unknown.verdict == "block"                                     # no equity to work the USD out: it does not pass
    assert rules.validate_limits({"daily_loss_steps": steps})["daily_loss_steps"] == [{"up_to_equity": float(s["up_to_equity"]), "usd": float(s["usd"])} for s in steps]
    for bad in ([], [{"up_to_equity": 5, "usd": 0}], [{"up_to_equity": 5, "usd": 1}, {"up_to_equity": 5, "usd": 2}], [{"up_to_equity": 5}], "x"):
        with pytest.raises(ValueError):
            rules.validate_limits({"daily_loss_steps": bad})
    soft = {**cfg, "guards": ["account"], "rule_modes": {"daily_loss": {"mode": "notice", "timeout_min": 30}}}
    out = pipeline.run(_only(soft), ctx(prop(), soft, book=book(equity=Decimal(150_000), day_pnl=Decimal(-600))))
    assert out.verdict == "PASS" and out.needs_owner is True               # a notice on the loss softens its steps too


# ---- the steps are limits too (3390a, second review of Vera) -------------------------------------------------------------------

STEPS = [{"up_to_equity": 200_000, "usd": 500}, {"up_to_equity": 600_000, "usd": 2000}, {"up_to_equity": 1_000_000, "usd": 5000}]


def test_removing_or_raising_steps_or_going_from_fixed_to_higher_steps_is_a_loosening():
    from hokma.broker import rules

    before = {"daily_loss_steps": STEPS, "drawdown_steps": STEPS}
    assert rules.loosened_limits(before, before) == []
    assert rules.loosened_limits(before, {"drawdown_steps": STEPS}) == ["daily_loss_steps"]                # the steps were taken out
    assert rules.loosened_limits(before, {}) == ["daily_loss_steps", "drawdown_steps"]
    raised = [dict(STEPS[0]), {"up_to_equity": 600_000, "usd": 2500}, dict(STEPS[2])]                      # one step goes up
    assert rules.loosened_limits(before, {**before, "daily_loss_steps": raised}) == ["daily_loss_steps"]
    lower = [{"up_to_equity": 200_000, "usd": 400}, dict(STEPS[1]), dict(STEPS[2])]
    assert rules.loosened_limits(before, {**before, "daily_loss_steps": lower}) == []                      # smaller is stricter
    earlier = [dict(STEPS[0]), {"up_to_equity": 400_000, "usd": 2000}, dict(STEPS[2])]                     # the break comes sooner: 500k now pays 5000
    assert rules.loosened_limits(before, {**before, "daily_loss_steps": earlier}) == ["daily_loss_steps"]
    later = [dict(STEPS[0]), {"up_to_equity": 900_000, "usd": 2000}, dict(STEPS[2])]                       # the break comes later: stricter
    assert rules.loosened_limits(before, {**before, "daily_loss_steps": later}) == []
    fixed = {"funding": {"daily_loss_usd": 300.0}}
    higher = [{"up_to_equity": 100_000, "usd": 250}, {"up_to_equity": 1_000_000, "usd": 900}]
    assert rules.loosened_limits(fixed, {"daily_loss_steps": higher}) == ["daily_loss_steps"]              # fixed to steps that go over it
    assert rules.loosened_limits(fixed, {"daily_loss_steps": [{"up_to_equity": 100_000, "usd": 250}]}) == []
    assert rules.loosened_limits({"daily_loss_steps": higher}, {"funding": {"daily_loss_usd": 900.0}}) == ["daily_loss_steps"]  # steps to a fixed above them
    assert rules.loosened_limits({"daily_loss_steps": higher}, {"funding": {"daily_loss_usd": 250.0}}) == []
    assert rules.loosened_limits(before, {**before, "daily_loss_steps": STEPS, "funding": {"daily_loss_usd": 100.0}}) == []   # a fixed under them tightens


def test_the_steps_are_worked_out_the_same_by_the_guard_the_killswitch_and_the_budget():
    from hokma.desks.trading import budget
    from hokma.exec import killswitch
    from hokma.exec.guards.account import AccountGuard

    limits = {"daily_loss_steps": STEPS, "drawdown_steps": STEPS}
    for equity, cap in ((150_000, 500), (500_000, 2000), (5_000_000, 5000)):
        eq = Decimal(equity)
        th = killswitch.thresholds_of(limits, eq)
        assert th == {"halt_day_loss_usd": float(cap), "halt_drawdown_usd": float(cap)}
        hit = lambda loss: AccountGuard().check(ctx(prop(), limits, book=book(equity=eq, peak_equity=eq, day_pnl=Decimal(-loss))))
        assert hit(cap).code == "daily_loss" and hit(cap - 1).verdict == "pass"
        inp = budget.BudgetInputs.from_book(book(equity=eq, peak_equity=eq, day_pnl=Decimal(-(cap - 1))), limits)
        assert budget.pretrade_check(inp) == ()
        inp = budget.BudgetInputs.from_book(book(equity=eq, peak_equity=eq, day_pnl=Decimal(-cap)), limits)
        assert [b.code for b in budget.pretrade_check(inp)] == ["daily_loss"]
        dd = budget.BudgetInputs.from_book(book(equity=eq, peak_equity=eq + cap), limits)
        assert [b.code for b in budget.pretrade_check(dd)] == ["trailing_drawdown"]
        plan = budget.size_by_plan(budget.BudgetInputs.from_book(book(equity=eq, peak_equity=eq, day_pnl=Decimal(-100)), limits),
                                   risk_per_contract_usd=Decimal(100), premium_per_contract_usd=Decimal(0), cash_per_contract_usd=Decimal(1))
        assert plan.limits["funding_daily_loss"] == (cap - 100) // 100 and plan.limits["funding_trailing"] == cap // 100
    assert killswitch.thresholds_of(limits) == {"halt_day_loss_usd": 500.0, "halt_drawdown_usd": 500.0}        # no equity: the strictest step
    both = killswitch.thresholds_of({**limits, "funding": {"daily_loss_usd": 300}}, Decimal(500_000))
    assert both["halt_day_loss_usd"] == 300.0                                                                  # the fixed and the step: the smaller holds


def test_a_rule_that_is_only_steps_is_present_and_a_notice_takes_it_out():
    only = {"daily_loss_steps": STEPS}
    assert rule_modes._present(only, "daily_loss") is True and rule_modes._present({}, "daily_loss") is False
    out, _ = rule_modes.without_notices({**only, "drawdown_steps": STEPS, "rule_modes": {"daily_loss": {"mode": "notice"}}})
    assert "daily_loss_steps" not in out and "drawdown_steps" in out
    from hokma.broker import rules

    assert rules.loosened_limits(only, {**only, "rule_modes": {"daily_loss": {"mode": "notice"}}}) == ["rule_modes.daily_loss"]   # restriction to notice
    for rule_id, paths in rule_modes.CONTROL_KEYS.items():
        assert paths and all(isinstance(p, tuple) and p for p in paths), rule_id
