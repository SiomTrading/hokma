"""Contract discipline at the border of a broker (SPEC-018: OA-48). A futures order needs an expiration and a multiplier, an
option also a strike, a type and an expiration, and a symbol with no sheet is refused: a new symbol gets no exception."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from hokma.contracts.identity import ContractSheet, NoContract


class DisciplineError(ValueError):
    """`code` is stable: no_contract, bad_multiplier, missing_expiration, missing_strike, missing_put_call."""

    def __init__(self, code: str, contract_id: str, why: str) -> None:
        self.code, self.contract_id, self.why = code, contract_id, why
        super().__init__(f"{code}: {why}")


def check_sheet(sheet: ContractSheet | None, contract_id: str) -> ContractSheet:
    if sheet is None:
        raise DisciplineError("no_contract", contract_id, f"{contract_id} has no contract sheet")
    for value in (sheet.multiplier, sheet.strike, sheet.tick_size):
        if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
            raise DisciplineError("bad_multiplier", contract_id, f"{contract_id} has a sheet value that is not a finite number")
    if not isinstance(sheet.multiplier, Decimal) or sheet.multiplier <= 0:
        raise DisciplineError("bad_multiplier", contract_id, f"{contract_id} has no multiplier greater than zero")
    if sheet.asset_type in ("future", "option", "future_option") and sheet.expiration is None:
        raise DisciplineError("missing_expiration", contract_id, f"{contract_id} has no expiration")
    if sheet.asset_type in ("option", "future_option"):
        if sheet.strike is None or sheet.strike <= 0:
            raise DisciplineError("missing_strike", contract_id, f"{contract_id} has no strike")
        if sheet.put_call not in ("C", "P"):
            raise DisciplineError("missing_put_call", contract_id, f"{contract_id} has no call or put type")
    return sheet


def check_border(contract_ids: list[str], sheets: Mapping[str, ContractSheet | None]) -> dict[str, ContractSheet]:
    """Every id must have a valid sheet; the first failure raises. Returns the sheets by id."""
    return {cid: check_sheet(sheets.get(cid), cid) for cid in contract_ids}


def load_sheets(contract_ids: list[str]) -> dict[str, ContractSheet | None]:
    """Resolve each id through the providers; an id with no sheet maps to None (the border then refuses it)."""
    from hokma.contracts.identity import resolve

    out: dict[str, ContractSheet | None] = {}
    for cid in contract_ids:
        try:
            out[cid] = resolve(cid)
        except NoContract:
            out[cid] = None
    return out
