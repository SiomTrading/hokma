"""The contract sheet in the database and as text for the owner (SPEC-018, SVC-contracts). Sheets are saved when they are
resolved for an order; a saved row is only replaced by a newer sheet of the provider."""
from __future__ import annotations

from typing import Any

from hokma.contracts.identity import ContractSheet, NoContract, resolve
from hokma.db import Database, get_database


def save(sheet: ContractSheet, *, db: Database | None = None, aliases: dict[str, str] | None = None) -> None:
    row = sheet.to_row()
    with (db or get_database()).write() as u:
        s = u.session
        if s.one("SELECT 1 AS x FROM contracts WHERE contract_id = :c", {"c": sheet.contract_id}) is None:
            s.insert_row("contracts", row)
        else:
            s.update("contracts", {"contract_id": sheet.contract_id}, {k: v for k, v in row.items() if k != "contract_id"})
        for provider, alias in (aliases or {}).items():
            if s.one("SELECT 1 AS x FROM contract_aliases WHERE provider = :p AND alias = :a", {"p": provider, "a": alias}) is None:
                s.insert_row("contract_aliases", {"provider": provider, "alias": alias, "contract_id": sheet.contract_id})


def load(contract_id: str, *, db: Database | None = None) -> ContractSheet | None:
    with (db or get_database()).read() as u:
        row = u.session.one("SELECT * FROM contracts WHERE contract_id = :c", {"c": contract_id})
    return None if row is None else ContractSheet.from_row(row)


def get(symbol: str, *, db: Database | None = None) -> ContractSheet:
    """The sheet of a symbol: resolved through the providers and saved, never typed by the caller."""
    sheet = resolve(symbol)
    save(sheet, db=db)
    return sheet


def describe(sheet: ContractSheet) -> dict[str, Any]:
    """The sheet as plain values for a tool answer (Decimal as text)."""
    out = sheet.to_row()
    out["tick_value"] = out["tick_value"] or None
    return out


__all__ = ["NoContract", "describe", "get", "load", "save"]
