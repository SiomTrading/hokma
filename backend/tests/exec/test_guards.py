"""The filter before an order (SPEC-018 B3: OA-43 to OA-47, DTS-06, AH-18)."""
import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from hokma.backoffice.snapshot import AccountSnapshot
from hokma.contracts import identity
from hokma.econ_calendar.windows import EventWindow
from hokma.exec.guards import GuardContext, passed
from hokma.exec.guards import cooldown as cooldown_module
from hokma.exec.guards import pipeline, registry
from hokma.exec.guards.account import AccountGuard
from hokma.exec.guards.cooldown import CooldownGuard
from hokma.exec.guards.event_window import EventWindowGuard
from hokma.exec.guards.max_position import MaxPositionSizeGuard
from hokma.exec.guards.whitelist import SymbolWhitelistGuard
from hokma.exec.limits import apply_limits
from hokma.exec.proposal import Leg, OrderProposal

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
NQ, SPY = "FUT:NQ:2026-12", "OPT:SPY:2026-12-18:C:600"


def prop(contract=NQ, side="buy", qty="1", effect="open", line="DT", **kw):
    return OrderProposal(1, line, "desk", (Leg(contract, side, effect, Decimal(qty)),), kw.pop("order_type", "market"), created_by="x", **kw)


def book(**kw):
    base = dict(account_id=1, as_of="2026-11-20 15:00:00", cash=Decimal(1_000_000), positions_value=Decimal(0), equity=Decimal(1_000_000),
                day_pnl=Decimal(0), peak_equity=Decimal(1_000_000), total_profit=Decimal(0), open_contracts=Decimal(0))
    base.update(kw)
    return AccountSnapshot(**base)


def ctx(p=None, **kw):
    sheets = {c: identity.resolve(c) for c in (NQ, SPY, "EQ:AAPL")}
    base = dict(proposal=p or prop(), contracts=sheets, positions=(), book=book(), limits={}, level=2, windows=(), now=NOW, desk_id=1,
                capital=Decimal(1_000_000), marks={NQ: Decimal(21000), SPY: Decimal("5.5"), "EQ:AAPL": Decimal(200)})
    base.update(kw)
    return GuardContext(**base)


def position(cid, qty, **kw):
    return SimpleNamespace(contract_id=cid, qty=qty, symbol=cid, market_value=kw.get("mv"), multiplier=kw.get("mult"), day_pl=None)


def test_the_context_cannot_be_changed_and_guards_run_in_order_and_stop_at_the_first_block():
    c = ctx(limits={"funding": {"max_contracts": 3}})
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.level = 3
    with pytest.raises(TypeError):
        c.limits["x"] = 1
    with pytest.raises(TypeError):
        c.limits["funding"]["max_contracts"] = 9
    seen = []

    class G:
        applies_to_closing = True

        def __init__(self, name, verdict):
            self.name, self.verdict = name, verdict

        def check(self, context):
            seen.append(self.name)
            return passed(self.name) if self.verdict == "pass" else dataclasses.replace(passed(self.name), verdict="block", code="no")

    out = pipeline.run([G("a", "pass"), G("b", "block"), G("c", "pass")], c)
    assert seen == ["a", "b"] and out.verdict == "BLOCK" and [r.guard for r in out.results] == ["a", "b"] and out.blocked_by.code == "no"


def test_max_position_uses_the_multiplier_of_the_sheet_and_a_new_symbol_has_no_exception():
    limits = {"max_position_pct": 50}
    # 1 NQ at 21000 is 21000 without the multiplier (2.1 percent of 1M) and 420000 with it (42 percent): under the ceiling
    assert MaxPositionSizeGuard().check(ctx(limits=limits)).verdict == "pass"
    two = MaxPositionSizeGuard().check(ctx(prop(qty="2"), limits=limits))          # 2 contracts: 840000 is 84 percent
    assert two.verdict == "block" and two.code == "max_position_pct" and two.numbers["multiplier"] == 20
    held = ctx(prop(qty="1"), limits=limits, positions=(position(NQ, 1),))          # a position that exists counts
    assert MaxPositionSizeGuard().check(held).verdict == "block"
    new_symbol = ctx(prop("EQ:AAPL", qty="3000"), limits={"max_position_pct": 10})
    assert MaxPositionSizeGuard().check(new_symbol).verdict == "block"             # 600000 of 1M: no exception for a symbol with no position
    no_price = ctx(prop("EQ:AAPL"), limits=limits, marks={})
    assert MaxPositionSizeGuard().check(no_price).code == "unpriced"
    assert MaxPositionSizeGuard().check(ctx(limits={})).code == "off"


def test_the_same_facts_give_the_same_answer():
    c = ctx(prop(qty="2"), limits={"max_position_pct": 50})
    assert [MaxPositionSizeGuard().check(c) for _ in range(3)] == [MaxPositionSizeGuard().check(c)] * 3


def test_whitelist_blocks_outside_symbols_but_allows_closing_an_existing_position():
    c = ctx(assets=("AAPL", "SPY"))
    assert SymbolWhitelistGuard().check(c).code == "symbol_not_allowed"
    assert SymbolWhitelistGuard().check(ctx(prop("EQ:AAPL"), assets=("AAPL",))).verdict == "pass"
    assert SymbolWhitelistGuard().check(ctx(prop(SPY), assets=("SPY",))).verdict == "pass"       # the underlying of an option
    closing = ctx(prop(side="sell", effect="close"), assets=("AAPL",), positions=(position(NQ, 1),))
    out = pipeline.run([SymbolWhitelistGuard()], closing)
    assert out.verdict == "PASS" and out.results[0].code == "closing_exempt"
    assert SymbolWhitelistGuard().check(ctx(assets=())).code == "no_list"


def test_cooldown_state_survives_a_restart_and_only_a_sent_order_starts_the_clock(ready):
    from exec_support import proposal  # noqa: F401

    limits = {"cooldown_seconds": 60}
    cooldown_module.record_sent(ready.desk_id, [NQ], at=NOW, db=ready.db)               # a sent order
    state = cooldown_module.load_state(ready.desk_id, db=ready.db)                      # read again, as after a restart
    soon = ctx(limits=limits, state=state, now=NOW + timedelta(seconds=10))
    assert CooldownGuard().check(soon).code == "cooldown" and CooldownGuard().check(soon).numbers["wait_seconds"] == 50
    assert CooldownGuard().check(ctx(limits=limits, state=state, now=NOW + timedelta(seconds=61))).verdict == "pass"
    assert CooldownGuard().check(ctx(prop("EQ:AAPL"), limits=limits, state=state, now=NOW)).verdict == "pass"      # another symbol
    # a refused order never wrote state: the clock of a symbol that was never sent is free
    assert cooldown_module.load_state(ready.desk_id, db=ready.db).keys() == {"cooldown:NQ"}


def test_account_guard_daily_loss_drawdown_floor_funding_and_a_closing_order_is_never_blocked():
    g = AccountGuard()
    assert g.check(ctx(limits={"funding": {"daily_loss_usd": 1000}}, book=book(day_pnl=Decimal(-1200)))).code == "daily_loss"
    assert g.check(ctx(limits={"funding": {"daily_loss_usd": 1000}}, book=book(day_pnl=Decimal(-500)))).verdict == "pass"
    assert g.check(ctx(limits={"max_drawdown_pct": 5}, book=book(equity=Decimal(940_000)))).code == "max_drawdown_pct"
    assert g.check(ctx(limits={"funding": {"trailing_drawdown_usd": 2000}}, book=book(equity=Decimal(997_000)))).code == "trailing_drawdown"
    assert g.check(ctx(limits={"capital_floor_usd": 990_000}, book=book(equity=Decimal(980_000)))).code == "capital_floor"
    assert g.check(ctx(limits={"funding": {"max_contracts": 2}}, book=book(open_contracts=Decimal(2)))).code == "funding_max_contracts"
    consistency = ctx(limits={"funding": {"consistency_pct": 30}}, book=book(day_pnl=Decimal(4000), total_profit=Decimal(10000)))
    assert g.check(consistency).code == "consistency"
    unknown = ctx(limits={"funding": {"daily_loss_usd": 1000}}, book=book(day_pnl=None))
    assert g.check(unknown).code == "book_unavailable"                           # never a zero for what is not known
    blocked_context = ctx(prop(side="sell", effect="close"), limits={"funding": {"daily_loss_usd": 1000}}, book=book(day_pnl=Decimal(-5000)),
                          positions=(position(NQ, 1),))
    assert pipeline.run([AccountGuard()], blocked_context).verdict == "PASS"
    assert pipeline.run([AccountGuard()], dataclasses.replace(blocked_context, proposal=prop())).verdict == "BLOCK"


def test_event_window_blocks_openings_and_an_unverified_calendar_is_a_block_not_an_empty_list():
    inside = EventWindow("cpi", "CPI", NOW - timedelta(minutes=5), NOW + timedelta(minutes=30))
    assert EventWindowGuard().check(ctx(windows=(inside,))).code == "event_window"
    unverified = EventWindow("*", None, NOW, NOW, unverified=True)
    assert EventWindowGuard().check(ctx(windows=(unverified,))).code == "unverified_event_risk"
    outside = EventWindow("cpi", "CPI", NOW + timedelta(hours=2), NOW + timedelta(hours=3))
    assert EventWindowGuard().check(ctx(windows=(outside,))).verdict == "pass"


def test_limits_clamp_by_line_and_refuse_what_does_not_fit_one_unit():
    pos = ()
    dt = apply_limits(prop(qty="5"), limits={"funding": {"max_contracts": 3}}, contracts=ctx().contracts, positions=pos, equity=Decimal(1_000_000), marks={})
    assert dt.proposal.legs[0].qty == 3 and dt.clamps[0]["limit"] == "funding.max_contracts" and dt.refused is None
    full = apply_limits(prop(qty="1"), limits={"funding": {"max_contracts": 3}}, contracts=ctx().contracts, positions=(position(NQ, 3),),
                        equity=Decimal(1_000_000), marks={})
    assert full.refused == "limit_max_contracts"
    swo = apply_limits(prop(SPY, qty="10", line="SWO", order_type="limit", limit_price=Decimal("5.5")), limits={"max_premium_usd": 2000}, contracts=ctx().contracts,
                       positions=pos, equity=Decimal(1_000_000), marks={SPY: Decimal("5.5")})
    assert swo.proposal.legs[0].qty == 3                  # 3 x 5.5 x 100 = 1650; 4 would be 2200
    swc = apply_limits(prop("EQ:AAPL", qty="500", line="SWC"), limits={"max_margin_pct": 5}, contracts=ctx().contracts, positions=pos,
                       equity=Decimal(1_000_000), marks={"EQ:AAPL": Decimal(200)})
    assert swc.proposal.legs[0].qty == 250                # 5 percent of 1M is 50000
    lp = apply_limits(prop("EQ:AAPL", qty="1000", line="LP"), limits={"max_weight_pct": 10}, contracts=ctx().contracts, positions=pos,
                      equity=Decimal(1_000_000), marks={"EQ:AAPL": Decimal(200)})
    assert lp.proposal.legs[0].qty == 500
    tiny = apply_limits(prop("EQ:AAPL", qty="1", line="LP"), limits={"max_weight_pct": 0.01}, contracts=ctx().contracts, positions=pos,
                        equity=Decimal(1_000_000), marks={"EQ:AAPL": Decimal(200)})
    assert tiny.refused == "limit_max_weight"
    untouched = apply_limits(prop(qty="2"), limits={}, contracts=ctx().contracts, positions=pos, equity=None, marks={})
    assert untouched.proposal.legs[0].qty == 2 and not untouched.clamps


def test_registry_resolves_by_type_and_an_unknown_type_fails_when_the_configuration_is_saved(ready):
    from hokma.broker import service

    assert [g.name for g in registry.build({})] == [n for n, _ in registry.ORDER]
    chosen = [g.name for g in registry.build({"guards": ["cooldown"]})]
    assert chosen == ["contract", "cooldown", "monitor", "live_loss", "premium_selling", "stop", "broker_rules"]   # the contract guard, the monitor, the live freeze, the premium switch, the stop of an entry and the rules of the broker always run
    with pytest.raises(service.BrokerError) as e:
        service.set_limits(ready.desk_id, {"guards": ["nope"]}, db=ready.db)
    assert e.value.code == "bad_limits"
    out = service.set_limits(ready.desk_id, {"guards": ["cooldown"], "max_position_pct": 40, "funding": {"max_contracts": 2}}, db=ready.db)
    assert out["guards"] == ["cooldown"]
    with pytest.raises(service.BrokerError) as e:
        service.set_limits(ready.desk_id, {"max_position_pct": 60}, db=ready.db, tighten_only=True)       # an agent cannot loosen
    assert e.value.code == "owner_only"
    service.set_limits(ready.desk_id, {"max_position_pct": 30}, db=ready.db, tighten_only=True)


def test_reduce_only_never_opens_the_opposite_position(ready):
    from hokma.exec import prefilter

    held = (position(NQ, 2),)
    sell_five = prop(side="sell", qty="5", effect="close", reduce_only=True)
    reduced, clamps = prefilter.clip_reduce_only(sell_five, held)
    assert reduced.legs[0].qty == 2 and clamps[0]["limit"] == "reduce_only" and clamps[0]["from_qty"] == "5"
    nothing, _ = prefilter.clip_reduce_only(prop(side="buy", qty="1", effect="close", reduce_only=True), held)
    assert nothing is None


from exec_support import db, desks_db, env, ready  # noqa: E402,F401  (fixtures)


def test_the_broker_rules_guard_measures_percent_limits_of_a_desk_without_capital_on_the_equity_and_fails_closed_without_it():
    """The same answer as the service path (SPEC-027 027c): capital 0 means the desk has none; the base is the equity of the account."""
    from hokma.exec.guards.broker_rules import BrokerRulesGuard

    limits = {"max_order_pct": 0.0001}
    sized = BrokerRulesGuard().check(ctx(capital=Decimal(0), limits=limits, book=book(equity=Decimal(1000)), orders_enabled=True, account_ok=True,
                                         preview={"orderBalance": {"orderValue": 5000.0}}))
    assert sized.verdict == "pass" and "max_order_pct" in sized.flags and "pct_base" not in sized.flags
    blind = BrokerRulesGuard().check(ctx(capital=Decimal(0), limits=limits, book=book(equity=None), orders_enabled=True, account_ok=True,
                                         preview={"orderBalance": {"orderValue": 5000.0}}))
    assert "pct_base" in blind.flags
