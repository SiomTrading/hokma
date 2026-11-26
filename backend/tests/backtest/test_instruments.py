"""Instruments built from the contract sheet (SPEC-020: N-01, N-04). No value is typed in the test: every expectation is read from the sheet."""
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from hokma.backtest import instruments
from hokma.backtest.instruments import InstrumentError, from_sheet
from hokma.contracts import identity
from hokma.contracts.identity import ContractSheet


@pytest.fixture(autouse=True)
def clean():
    identity.clear_providers()
    yield
    identity.clear_providers()


def test_a_future_comes_from_its_sheet():
    sheet = identity.resolve("FUT:NQ:2025-12")
    nq = from_sheet(sheet)
    assert isinstance(nq, instruments.FutureInstrument)
    assert (nq.tick_size, nq.multiplier, nq.expiration, nq.root, nq.contract_id) == (sheet.tick_size, sheet.multiplier, sheet.expiration, "NQ", "FUT:NQ:2025-12")
    assert (nq.tick_size, nq.multiplier) == (D("0.25"), D(20))            # what the exchange table says, as a check of the chain
    assert nq.expiration == date(2025, 12, 1) and nq.source == sheet.source


def test_an_option_a_share_and_a_cfd_come_with_the_fields_of_the_sheet():
    option_sheet, share_sheet = identity.resolve("OPT:SPY:2026-12-18:C:600"), identity.resolve("EQ:AAPL")
    cfd_sheet = ContractSheet("CFD:EURUSD", "cfd", None, "EURUSD", D(1), D("0.00001"), D("0.00001"), None, None, None, "USD", "provider",
                              datetime(2025, 11, 10, tzinfo=timezone.utc))
    option, share, cfd = from_sheet(option_sheet), from_sheet(share_sheet), from_sheet(cfd_sheet)
    assert isinstance(option, instruments.OptionInstrument)
    assert (option.underlying, option.strike, option.put_call, option.expiration, option.multiplier) == (
        "SPY", D(600), "C", date(2026, 12, 18), option_sheet.multiplier)
    assert isinstance(share, instruments.EquityInstrument) and (share.symbol, share.tick_size) == ("AAPL", share_sheet.tick_size)
    assert isinstance(cfd, instruments.CfdInstrument) and (cfd.symbol, cfd.tick_size, cfd.multiplier) == ("EURUSD", D("0.00001"), D(1))


def test_a_future_option_keeps_its_future_root():
    sheet = ContractSheet("FOP:ES:2026-12:2026-12-18:C:6000", "future_option", "ES", "ES", D(50), D("0.05"), D("2.5"), date(2026, 12, 18), D(6000), "C",
                          "USD", "provider", datetime(2025, 11, 10, tzinfo=timezone.utc))
    out = from_sheet(sheet)
    assert isinstance(out, instruments.FutureOptionInstrument) and (out.future_root, out.strike, out.multiplier) == ("ES", D(6000), D(50))


def test_a_sheet_without_what_the_instrument_needs_is_refused():
    no_tick = ContractSheet("CRY:BTC-USD", "crypto", None, "BTC-USD", D(1), None, None, None, None, None, "USD", "x", datetime(2025, 11, 10, tzinfo=timezone.utc))
    with pytest.raises(InstrumentError) as error:
        from_sheet(no_tick)
    assert error.value.code == "unsupported_asset_type"
    future_without_tick = ContractSheet("FUT:NQ:2025-12", "future", "NQ", "NQ", D(20), None, None, date(2025, 12, 1), None, None, "USD", "x",
                                        datetime(2025, 11, 10, tzinfo=timezone.utc))
    with pytest.raises(InstrumentError) as error:
        from_sheet(future_without_tick)
    assert error.value.code == "incomplete_sheet" and "tick_size" in str(error.value)


def test_spreads_of_options_and_of_futures():
    call, put = identity.resolve("OPT:SPY:2026-12-18:C:600"), identity.resolve("OPT:SPY:2026-12-18:C:605")
    vertical = instruments.spread([(call, 1), (put, -1)])
    assert vertical.kind == "option_spread" and [leg.ratio for leg in vertical.legs] == [1, -1]
    calendar = instruments.spread([(identity.resolve("FUT:NQ:2025-12"), 1), (identity.resolve("FUT:NQ:2026-03"), -1)])
    assert calendar.kind == "future_spread" and calendar.legs[1].instrument.expiration == date(2026, 3, 1)


@pytest.mark.parametrize("legs", [
    [("OPT:SPY:2026-12-18:C:600", 1)],                                           # one leg
    [("OPT:SPY:2026-12-18:C:600", 1), ("FUT:NQ:2025-12", -1)],                    # a mix
    [("OPT:SPY:2026-12-18:C:600", 1), ("OPT:QQQ:2026-12-18:C:500", -1)],          # two underlyings
    [("FUT:NQ:2025-12", 1), ("FUT:ES:2025-12", -1)],                              # two roots
    [("OPT:SPY:2026-12-18:C:600", 0), ("OPT:SPY:2026-12-18:C:605", 1)],           # a zero ratio
])
def test_a_bad_spread_is_refused(legs):
    with pytest.raises(InstrumentError) as error:
        instruments.spread([(identity.resolve(cid), ratio) for cid, ratio in legs])
    assert error.value.code == "bad_spread"
