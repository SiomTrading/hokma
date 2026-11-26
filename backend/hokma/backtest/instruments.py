"""The instruments of a backtest, built from the contract sheet (SPEC-020: N-01, N-04).

Every number of an instrument (multiplier, tick, expiration, strike) is read from the `ContractSheet` of the provider and none is typed
here. The values are plain frozen dataclasses: the engine objects are made from them in `backtest/_lib.py`, the only file that may name
the engine.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from hokma.contracts.identity import ContractSheet


class InstrumentError(ValueError):
    """The sheet cannot become an instrument. `code` is stable: unsupported_asset_type, incomplete_sheet, bad_spread."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Instrument:
    contract_id: str
    multiplier: Decimal
    tick_size: Decimal
    currency: str
    source: str                 # where the sheet came from


@dataclass(frozen=True)
class FutureInstrument(Instrument):
    root: str
    expiration: date            # the expiration the sheet gives


@dataclass(frozen=True)
class OptionInstrument(Instrument):
    underlying: str
    strike: Decimal
    put_call: Literal["C", "P"]
    expiration: date


@dataclass(frozen=True)
class FutureOptionInstrument(OptionInstrument):
    future_root: str


@dataclass(frozen=True)
class EquityInstrument(Instrument):
    symbol: str


@dataclass(frozen=True)
class CfdInstrument(Instrument):
    symbol: str


@dataclass(frozen=True)
class Leg:
    instrument: Instrument
    ratio: int                  # signed: positive buys, negative sells


@dataclass(frozen=True)
class SpreadInstrument:
    kind: Literal["option_spread", "future_spread"]
    legs: tuple[Leg, ...]


def _need(sheet: ContractSheet, *names: str) -> None:
    missing = [n for n in names if getattr(sheet, n) is None]
    if missing:
        raise InstrumentError("incomplete_sheet", f"{sheet.contract_id} has no {', '.join(missing)}")


def from_sheet(sheet: ContractSheet) -> Instrument:
    """The instrument that a sheet describes: future, option, future option, share or ETF, or CFD."""
    kind = sheet.asset_type
    base = (sheet.contract_id, sheet.multiplier)
    if kind == "future":
        _need(sheet, "tick_size", "expiration", "root")
        return FutureInstrument(*base, sheet.tick_size, sheet.currency, sheet.source, sheet.root, sheet.expiration)
    if kind in ("option", "future_option"):
        _need(sheet, "tick_size", "expiration", "strike", "put_call", "underlying")
        args = (*base, sheet.tick_size, sheet.currency, sheet.source, sheet.underlying, sheet.strike, sheet.put_call, sheet.expiration)
        return FutureOptionInstrument(*args, sheet.root or sheet.underlying) if kind == "future_option" else OptionInstrument(*args)
    if kind in ("equity", "etf"):
        _need(sheet, "tick_size", "underlying")
        return EquityInstrument(*base, sheet.tick_size, sheet.currency, sheet.source, sheet.underlying)
    if kind == "cfd":
        _need(sheet, "tick_size", "underlying")
        return CfdInstrument(*base, sheet.tick_size, sheet.currency, sheet.source, sheet.underlying)
    raise InstrumentError("unsupported_asset_type", f"{sheet.contract_id} is a {kind}, which the backtest does not simulate")


def spread(legs: Sequence[tuple[ContractSheet, int]]) -> SpreadInstrument:
    """A spread of options or of futures from the sheets of its legs and their ratios (negative sells)."""
    if len(legs) < 2:
        raise InstrumentError("bad_spread", "a spread has at least two legs")
    built = tuple(Leg(from_sheet(sheet), ratio) for sheet, ratio in legs)
    if any(not isinstance(ratio, int) or isinstance(ratio, bool) or ratio == 0 for _, ratio in legs):
        raise InstrumentError("bad_spread", "the ratio of a leg is a non-zero whole number")
    if all(isinstance(leg.instrument, OptionInstrument) for leg in built):
        if len({leg.instrument.underlying for leg in built}) != 1:  # type: ignore[union-attr]
            raise InstrumentError("bad_spread", "the legs of an option spread share one underlying")
        return SpreadInstrument("option_spread", built)
    if all(isinstance(leg.instrument, FutureInstrument) for leg in built):
        if len({leg.instrument.root for leg in built}) != 1:  # type: ignore[union-attr]
            raise InstrumentError("bad_spread", "the legs of a future spread share one root")
        return SpreadInstrument("future_spread", built)
    raise InstrumentError("bad_spread", "a spread is made of options or of futures, not a mix")
