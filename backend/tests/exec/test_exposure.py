"""The exposure guard (SPEC-020: L-50): caps per symbol and per sector, a layer added to the filter of H2 (no guard of H2 changes)."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from exec_support import db, desks_db, env, ready  # noqa: F401  (fixtures)

from hokma.backoffice.snapshot import AccountSnapshot
from hokma.broker import rules, service
from hokma.contracts import identity
from hokma.exec.guards import GuardContext, pipeline, registry
from hokma.exec.guards.exposure import ExposureGuard
from hokma.exec.proposal import Leg, OrderProposal

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
AAPL, MSFT, XOM = "EQ:AAPL", "EQ:MSFT", "EQ:XOM"
SECTORS = {"AAPL": "technology", "MSFT": "technology", "XOM": "energy"}
EQUITY = Decimal(1_000_000)


def prop(contract=AAPL, side="buy", qty="1", effect="open", **kw):
    return OrderProposal(1, "LP", "desk", (Leg(contract, side, effect, Decimal(qty)),), kw.pop("order_type", "market"), created_by="x", **kw)


def book():
    return AccountSnapshot(account_id=1, as_of="2026-11-20 15:00:00", cash=EQUITY, positions_value=Decimal(0), equity=EQUITY, day_pnl=Decimal(0),
                           peak_equity=EQUITY, total_profit=Decimal(0), open_contracts=Decimal(0))


def ctx(p=None, **kw):
    sheets = {c: identity.resolve(c) for c in (AAPL, MSFT, XOM)}
    base = dict(proposal=p or prop(), contracts=sheets, positions=(), book=book(), limits={}, level=2, windows=(), now=NOW, desk_id=1,
                capital=EQUITY, marks={AAPL: Decimal(200), MSFT: Decimal(150), XOM: Decimal(100)})
    base.update(kw)
    return GuardContext(**base)


def held(cid, qty, **kw):
    return SimpleNamespace(contract_id=cid, qty=qty, symbol=cid, market_value=kw.get("mv"), multiplier=kw.get("mult"), day_pl=None)


def test_an_order_that_leaves_the_sector_at_35_percent_with_a_cap_of_30_blocks_with_sector_cap():
    # MSFT 1000 x 150 = 150000 (15 percent) plus AAPL 1000 x 200 = 200000 (20 percent): the sector holds 350000 of 1000000
    limits = {"max_sector_pct": 30, "sectors": SECTORS}
    out = ExposureGuard().check(ctx(prop(qty="1000"), limits=limits, positions=(held(MSFT, 1000),)))
    assert (out.verdict, out.code) == ("block", "sector_cap")
    assert out.numbers["sector_pct"] == 35.0 and out.numbers["cap_pct"] == 30.0
    # the same order with no other position in the sector is 20 percent
    assert ExposureGuard().check(ctx(prop(qty="1000"), limits=limits)).verdict == "pass"


def test_a_sector_at_exactly_the_cap_passes_and_a_position_of_another_sector_does_not_count():
    limits = {"max_sector_pct": 35, "sectors": SECTORS}
    assert ExposureGuard().check(ctx(prop(qty="1000"), limits=limits, positions=(held(MSFT, 1000),))).verdict == "pass"       # 35 is not above 35
    other = ctx(prop(qty="1000"), limits=limits, positions=(held(XOM, 5000),))                                                   # 500000 of energy
    assert ExposureGuard().check(other).verdict == "pass"


def test_the_symbol_cap_counts_what_is_held_plus_the_order_and_blocks_with_symbol_cap():
    held_aapl = (held(AAPL, 500),)                                                    # 500 x 200 = 100000 (10 percent)
    assert ExposureGuard().check(ctx(prop(qty="100"), limits={"max_symbol_pct": 12}, positions=held_aapl)).verdict == "pass"      # 120000 = 12
    out = ExposureGuard().check(ctx(prop(qty="101"), limits={"max_symbol_pct": 12}, positions=held_aapl))                          # 120200 = 12.02
    assert (out.verdict, out.code) == ("block", "symbol_cap") and out.numbers["symbol_pct"] == pytest.approx(12.02)


def test_the_price_of_the_order_cannot_make_the_exposure_smaller_than_the_market():
    far = prop(qty="1000", order_type="limit", limit_price=Decimal(1))               # a limit far under the mark of 200
    assert ExposureGuard().check(ctx(far, limits={"max_symbol_pct": 10})).code == "symbol_cap"      # valued at the mark: 20 percent


def test_what_cannot_be_measured_blocks():
    limits = {"max_sector_pct": 30, "sectors": {"AAPL": "technology"}}
    assert ExposureGuard().check(ctx(prop(MSFT), limits=limits)).code == "no_sector"
    assert ExposureGuard().check(ctx(prop(), limits={"max_symbol_pct": 10}, marks={})).code == "unpriced"
    no_base = ctx(prop(), limits={"max_symbol_pct": 10}, book=AccountSnapshot(1, "x", None, None, None, None), capital=Decimal(0))
    assert ExposureGuard().check(no_base).code == "no_base"
    unknown_held = ctx(prop(), limits={"max_sector_pct": 30, "sectors": SECTORS}, marks={AAPL: Decimal(200)}, positions=(held(MSFT, 10),))
    assert ExposureGuard().check(unknown_held).code == "unpriced"
    assert ExposureGuard().check(ctx(prop(), limits={"max_symbol_pct": 10, "sectors": SECTORS}, positions=(held(MSFT, 10),),
                                     marks={AAPL: Decimal(200)})).verdict == "pass"      # a symbol cap does not need the price of another root


def test_a_position_with_no_mark_is_valued_with_the_value_the_broker_gave_it():
    positions = (held(MSFT, 1000, mv=Decimal(150_000)),)
    limits = {"max_sector_pct": 30, "sectors": SECTORS}
    out = ExposureGuard().check(ctx(prop(qty="1000"), limits=limits, positions=positions, marks={AAPL: Decimal(200)}))
    assert out.code == "sector_cap" and out.numbers["sector_pct"] == 35.0


def test_it_is_off_without_limits_and_never_blocks_a_closing_order():
    assert ExposureGuard().check(ctx()).code == "off"
    closing = ctx(prop(side="sell", effect="close", qty="500"), limits={"max_symbol_pct": 1}, positions=(held(AAPL, 500),))
    out = pipeline.run([ExposureGuard()], closing)
    assert out.verdict == "PASS" and out.results[0].code == "closing_exempt"
    assert pipeline.run([ExposureGuard()], ctx(prop(qty="1000"), limits={"max_symbol_pct": 1})).blocked_by.code == "symbol_cap"


def test_it_is_registered_as_one_more_layer_and_the_guards_of_h2_keep_their_order():
    names = registry.known_types()
    assert "exposure" in names and names.index("exposure") == names.index("max_position") + 1
    h2 = ("contract", "whitelist", "event_window", "cooldown", "max_position", "monitor", "live_loss", "account", "limits", "premium_selling", "stop", "broker_rules")
    assert tuple(n for n in names if n != "exposure") == h2
    assert [g.name for g in registry.build({"guards": ["whitelist"]})] == ["contract", "whitelist", "monitor", "live_loss", "premium_selling", "stop", "broker_rules"]      # a desk list leaves it out
    assert "exposure" in [g.name for g in registry.build({})]


def test_the_limit_keys_are_checked_and_loosening_a_cap_is_the_owners():
    merged = rules.validate_limits({"max_sector_pct": 30, "max_symbol_pct": 10, "sectors": {"aapl": "Technology", "msft": "technology"}})
    assert merged == {"max_sector_pct": 30.0, "max_symbol_pct": 10.0, "sectors": {"AAPL": "technology", "MSFT": "technology"}}
    for bad in ({"max_sector_pct": 0}, {"max_symbol_pct": 101}, {"max_sector_pct": True}, {"sectors": {}}, {"sectors": {"AAPL": "a b"}}, {"sectors": []}):
        with pytest.raises(ValueError):
            rules.validate_limits(bad)
    assert rules.loosened_limits({"max_sector_pct": 30}, {"max_sector_pct": 35}) == ["max_sector_pct"]
    assert rules.loosened_limits({"max_sector_pct": 30}, {}) == ["max_sector_pct"]
    assert rules.loosened_limits({"max_symbol_pct": 10}, {"max_symbol_pct": 12}) == ["max_symbol_pct"]
    assert rules.loosened_limits({"max_sector_pct": 30}, {"max_sector_pct": 25}) == []
    assert rules.loosened_limits({}, {"max_sector_pct": 25}) == []
    assert rules.loosened_limits({"sectors": {"AAPL": "technology"}}, {"sectors": {"AAPL": "energy"}}) == ["sectors"]
    assert rules.loosened_limits({"sectors": {"AAPL": "technology"}}, {"sectors": {"AAPL": "technology", "XOM": "energy"}}) == []


def test_an_agent_cannot_loosen_the_cap_but_the_owner_can(ready):
    service.set_limits(ready.desk_id, {"max_sector_pct": 30, "sectors": SECTORS}, db=ready.db)                                 # the owner's app
    with pytest.raises(service.BrokerError) as refused:
        service.set_limits(ready.desk_id, {"max_sector_pct": 35}, db=ready.db, tighten_only=True)
    assert refused.value.code == "owner_only"
    assert service.get_limits(ready.desk_id, db=ready.db)["max_sector_pct"] == 30.0
    assert service.set_limits(ready.desk_id, {"max_sector_pct": 25}, db=ready.db, tighten_only=True)["max_sector_pct"] == 25.0      # tighter is free
    assert service.set_limits(ready.desk_id, {"max_sector_pct": 35}, db=ready.db)["max_sector_pct"] == 35.0


def test_with_a_sector_cap_active_a_new_root_in_the_sector_map_is_a_loosening_only_the_owner_makes():
    before = {"max_sector_pct": 30, "sectors": {"AAPL": "technology"}}
    assert rules.loosened_limits(before, {**before, "sectors": {"AAPL": "technology", "MSFT": "technology"}}) == ["sectors"]
    assert rules.loosened_limits(before, {**before, "sectors": {"AAPL": "energy"}}) == ["sectors"]
    assert rules.loosened_limits(before, {**before, "sectors": {}}) == ["sectors"]
    assert rules.loosened_limits(before, dict(before)) == []
    # without any sector cap the map is only data
    assert rules.loosened_limits({"sectors": {"AAPL": "technology"}}, {"sectors": {"AAPL": "technology", "MSFT": "technology"}}) == []
