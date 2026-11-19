"""brokers, broker_accounts, account_ledger, desk_accounts (task 3294, C7-5a). Rows and their limits only: the
contract that brokers obey lives in hokma.broker."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from hokma.db.errors import ValidationError
from hokma.db.models import Record
from hokma.db.repos.base import Repo
from hokma.db.repos.desks import MAX_CAPITAL, MIN_CAPITAL
from hokma.db.timeutil import utcnow


class NameTaken(ValidationError):
    """An account name already used in that broker."""

CURRENCIES = ("USD",)
MAX_NAME = 60


@dataclass(frozen=True, slots=True)
class BrokerRow(Record):
    JSONS = ("config",)
    id: int
    kind: str
    name: str
    config: dict
    created_at: str


@dataclass(frozen=True, slots=True)
class BrokerAccount(Record):
    id: int
    broker_id: int
    name: str
    initial_capital: int
    currency: str
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class LedgerEntry(Record):
    id: int
    account_id: int
    kind: str
    amount: float
    created_at: str


class BrokerRepo(Repo):
    def create_broker(self, kind: str, name: str, config: dict[str, Any] | None = None) -> BrokerRow:
        name = (name or "").strip()
        if not 1 <= len(name) <= MAX_NAME or not 1 <= len(kind or "") <= 40:
            raise ValidationError(f"broker name must have 1 to {MAX_NAME} characters and a kind")
        new_id = self.s.insert("brokers", {"kind": kind, "name": name, "config": json.dumps(config or {}),
                                           "created_at": utcnow()})
        return self.get_broker(new_id)

    def get_broker(self, id: int) -> BrokerRow:
        return self._get("brokers", "id", id, BrokerRow)

    def find_broker(self, name: str) -> BrokerRow | None:
        return self._find("brokers", "name", name, BrokerRow)

    def list_brokers(self) -> list[BrokerRow]:
        return [BrokerRow.from_row(r) for r in self.s.query("SELECT * FROM brokers ORDER BY id")]

    def set_broker_config(self, id: int, config: dict[str, Any]) -> BrokerRow:
        self.get_broker(id)
        self.s.update("brokers", {"id": id}, {"config": json.dumps(config)})
        return self.get_broker(id)

    def create_account(self, broker_id: int, name: str, capital: int, currency: str = "USD") -> BrokerAccount:
        """The account and its 'open' entry, in one go. The limits are the ones of `desks.capital`."""
        name = (name or "").strip()
        if not 1 <= len(name) <= MAX_NAME:
            raise ValidationError(f"name must have 1 to {MAX_NAME} characters")
        if currency not in CURRENCIES:
            raise ValidationError(f"currency must be one of {', '.join(CURRENCIES)}; got {currency!r}")
        if not isinstance(capital, int) or isinstance(capital, bool) or not MIN_CAPITAL <= capital <= MAX_CAPITAL:
            raise ValidationError(f"capital must be a whole number from {MIN_CAPITAL} to {MAX_CAPITAL}")
        self.get_broker(broker_id)
        if self.find_account_by_name(broker_id, name) is not None:
            raise NameTaken(f"an account named {name!r} already exists in this broker")
        new_id = self.s.insert("broker_accounts", {"broker_id": broker_id, "name": name, "initial_capital": capital,
                                                   "currency": currency, "created_at": utcnow()})
        self._entry(new_id, "open", capital)
        return self.get_account(new_id)

    def get_account(self, id: int) -> BrokerAccount:
        return self._get("broker_accounts", "id", id, BrokerAccount)

    def find_account_by_name(self, broker_id: int, name: str) -> BrokerAccount | None:
        row = self.s.one("SELECT * FROM broker_accounts WHERE broker_id = :b AND name = :n", {"b": broker_id, "n": name})
        return BrokerAccount.from_row(row) if row else None

    def list_accounts(self, broker_id: int) -> list[BrokerAccount]:
        rows = self.s.query("SELECT * FROM broker_accounts WHERE broker_id = :b AND status = 'active' ORDER BY id",
                            {"b": broker_id})
        return [BrokerAccount.from_row(r) for r in rows]

    def ledger(self, account_id: int) -> list[LedgerEntry]:
        rows = self.s.query("SELECT * FROM account_ledger WHERE account_id = :a ORDER BY id", {"a": account_id})
        return [LedgerEntry.from_row(r) for r in rows]

    def last_entry(self, account_id: int) -> LedgerEntry | None:
        row = self.s.one("SELECT * FROM account_ledger WHERE account_id = :a ORDER BY id DESC LIMIT 1", {"a": account_id})
        return LedgerEntry.from_row(row) if row else None

    def reset_account(self, id: int) -> BrokerAccount:
        """Drop every entry after the opening one and write a 'reset' at the initial capital. Other accounts are not touched."""
        account = self.get_account(id)
        opening = self.s.scalar("SELECT MIN(id) FROM account_ledger WHERE account_id = :a AND kind = 'open'", {"a": id})
        self.s.execute("DELETE FROM account_ledger WHERE account_id = :a AND id > :o", {"a": id, "o": opening or 0})
        self._entry(id, "reset", account.initial_capital)
        return account

    def _entry(self, account_id: int, kind: str, amount: float) -> None:
        self.s.insert("account_ledger", {"account_id": account_id, "kind": kind, "amount": amount,
                                         "created_at": utcnow()})

    def link_desk(self, desk_id: int, account_id: int) -> None:
        self.get_account(account_id)
        self.s.insert_row("desk_accounts", {"desk_id": desk_id, "account_id": account_id})

    def account_of_desk(self, desk_id: int) -> BrokerAccount | None:
        row = self.s.one("SELECT a.* FROM broker_accounts a JOIN desk_accounts d ON d.account_id = a.id "
                         "WHERE d.desk_id = :d", {"d": desk_id})
        return BrokerAccount.from_row(row) if row else None

    def desk_of_account(self, account_id: int) -> int | None:
        return self.s.scalar("SELECT desk_id FROM desk_accounts WHERE account_id = :a", {"a": account_id})

    def unlink_desk(self, desk_id: int) -> None:
        self.s.execute("DELETE FROM desk_accounts WHERE desk_id = :d", {"d": desk_id})
