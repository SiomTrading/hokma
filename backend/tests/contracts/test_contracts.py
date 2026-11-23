"""Contract sheets (SPEC-018 B1: TA-33, TA-68, OA-48, AH-41)."""
from datetime import date
from decimal import Decimal

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.broker.types import Fill, Order, Position
from hokma.contracts import discipline, identity, sheet, symbols
from hokma.contracts.identity import ContractSheet, NoContract


@pytest.fixture(autouse=True)
def clean():
    identity.clear_providers()
    yield
    identity.clear_providers()


def test_one_instrument_written_by_three_providers_is_one_id():
    ids = {identity.parse_symbol(x).contract_id for x in ("SPY   261218C00600000", "SPY261218C00600000", "OPT:SPY:2026-12-18:C:600")}
    assert ids == {"OPT:SPY:2026-12-18:C:600"}
    assert {identity.parse_symbol(x).contract_id for x in ("BTC-USD", "BTC/USD", "BINANCE:BTCUSDT")} == {"CRY:BTC-USD"}
    assert {identity.parse_symbol(x).contract_id for x in ("/NQZ26", "NQZ26", "FUT:NQ:2026-12")} == {"FUT:NQ:2026-12"}


def test_the_sheet_carries_multiplier_tick_expiration_strike_and_underlying_and_is_resolved_once():
    calls = []

    def provider(parsed):
        calls.append(parsed.contract_id)
        return ContractSheet(parsed.contract_id, "option", "SPY", "SPY", Decimal(100), Decimal("0.01"), Decimal(1), parsed.expiration,
                             parsed.strike, parsed.put_call, "USD", "fixture", identity.datetime(2026, 11, 1, tzinfo=identity.timezone.utc))

    identity.register_provider("fixture", provider)
    a = identity.resolve("SPY   261218C00600000")
    b = identity.resolve("OPT:SPY:2026-12-18:C:600")
    assert a is b and calls == ["OPT:SPY:2026-12-18:C:600"]
    assert (a.multiplier, a.strike, a.expiration, a.underlying, a.put_call) == (Decimal(100), Decimal(600), date(2026, 12, 18), "SPY", "C")
    identity.forget_run()
    identity.resolve("SPY261218C00600000")
    assert len(calls) == 2                                   # a new run asks again


def test_a_future_takes_its_multiplier_from_a_sheet_and_a_continuous_symbol_needs_the_provider_front_month():
    nq = identity.resolve("NQZ26")
    assert (nq.multiplier, nq.tick_size, nq.tick_value, nq.expiration) == (Decimal(20), Decimal("0.25"), Decimal(5), date(2026, 12, 1))
    with pytest.raises(NoContract):
        identity.resolve("NQ=F")
    identity.register_provider("fixture", lambda p: None, front_month=lambda root: date(2027, 3, 1))
    assert identity.resolve("NQ=F").contract_id == "FUT:NQ:2027-03"
    with pytest.raises(NoContract):
        identity.resolve("ZZZ9=F")


@pytest.mark.parametrize("cid", ["EQ:AAPL", "FUT:NQ:2026-12", "OPT:SPY:2026-12-18:C:600", "CRY:BTC-USD", "EQ:BRK.B", "OPT:SPY:2026-12-18:P:412.5"])
@pytest.mark.parametrize("provider", ["schwab", "alpaca", "ibkr", "finnhub", "yahoo", "crypto"])
def test_symbols_round_trip_for_every_provider_that_carries_the_instrument(cid, provider):
    try:
        text = symbols.to_provider(cid, provider)
    except NoContract:
        return
    back = symbols.from_provider(provider, text)
    if cid.startswith("FUT") and provider == "yahoo":
        assert back == "FUT:NQ:*"
    else:
        assert back == cid, (provider, text)


def test_discipline_refuses_what_has_no_sheet_or_no_expiration_or_strike():
    good = identity.resolve("NQZ26")
    assert discipline.check_sheet(good, good.contract_id) is good
    with pytest.raises(discipline.DisciplineError) as e:
        discipline.check_sheet(None, "EQ:NEWCO")
    assert e.value.code == "no_contract"
    from dataclasses import replace
    with pytest.raises(discipline.DisciplineError) as e:
        discipline.check_sheet(replace(good, expiration=None), good.contract_id)
    assert e.value.code == "missing_expiration"
    with pytest.raises(discipline.DisciplineError) as e:
        discipline.check_sheet(replace(good, multiplier=Decimal(0)), good.contract_id)
    assert e.value.code == "bad_multiplier"
    opt = identity.resolve("OPT:SPY:2026-12-18:C:600")
    with pytest.raises(discipline.DisciplineError) as e:
        discipline.check_sheet(replace(opt, strike=None), opt.contract_id)
    assert e.value.code == "missing_strike"
    loaded = discipline.load_sheets(["EQ:AAPL", "FUT:ZZZ:2026-12"])
    assert loaded["FUT:ZZZ:2026-12"] is None and loaded["EQ:AAPL"] is not None


def test_a_sheet_is_saved_and_read_back(db):
    nq = identity.resolve("NQZ26")
    sheet.save(nq, db=db, aliases={"schwab": "/NQZ26"})
    assert sheet.load("FUT:NQ:2026-12", db=db) == nq
    sheet.save(nq, db=db)                                    # saving again changes nothing
    assert sheet.describe(nq)["multiplier"] == "20"


def test_order_fill_and_position_carry_the_multiplier():
    o = Order(1, "NQZ6", "buy", 1.0, multiplier=Decimal(20), contract_id="FUT:NQ:2026-12")
    f = Fill(1, "NQZ6", "buy", 1.0, 21400.0, 2.0, "2026-11-02 15:00:00", multiplier=Decimal(20))
    p = Position(1, "NQZ6", -2.0, 21400.0, multiplier=20.0, contract_id="FUT:NQ:2026-12")
    assert o.multiplier == f.multiplier == Decimal(20) and p.qty < 0 and f.commission == 2.0


@pytest.mark.parametrize("bad", ["OPT:SPY:2026-12-18:C:123.4567", "OPT:SPY:2026-12-18:C:1e6", "OPT:SPY:2026-12-18:C:NaN", "OPT:SPY:2101-12-18:C:600",
                                 "OPT:SPY:2026-12-18:C:600.0", "OPT:SPY:2026-12-18:C:0600", "FUT:NQ:2101-12", "EQ:aapl", "OPT:SPY:2026-12-18:C:100000"])
def test_a_malformed_canonical_id_is_refused_so_it_cannot_name_another_instrument(bad):
    with pytest.raises(NoContract):
        identity.parse_canonical(bad)


def test_a_non_finite_sheet_value_and_an_unwritable_occ_symbol_are_refused():
    from dataclasses import replace
    nq = identity.resolve("NQZ26")
    with pytest.raises(discipline.DisciplineError):
        discipline.check_sheet(replace(nq, multiplier=Decimal("NaN")), nq.contract_id)
    from hokma.contracts.identity import Parsed
    odd = Parsed("option", underlying="SPY", expiration=date(2026, 12, 18), strike=Decimal("123.4567"), put_call="C")
    with pytest.raises(NoContract):
        symbols._occ(odd, pad=True)
