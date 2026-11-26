"""brokers, broker_accounts, account_ledger, desk_account_links and desk_strategy_accounts (task 3294, C7-5a; 0024, SPEC-027). Rows and their limits only: the
contract that brokers obey lives in hokma.broker."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from hokma.db.errors import IntegrityViolation, ValidationError
from hokma.db.models import Record, loads_json
from hokma.db.repos.base import Repo, require_in
from hokma.db.repos.desks import MAX_CAPITAL, MIN_CAPITAL
from hokma.db.timeutil import utcnow


class NameTaken(ValidationError):
    """An account name already used in that broker."""


class AlreadyLinked(ValidationError):
    """The account is already in that desk."""


class NotLinked(ValidationError):
    """The account is not in that desk, so a strategy cannot trade through it."""


class AccountInUseByStrategy(IntegrityViolation):
    """A strategy of the desk trades through the account: it cannot be taken out (409 account_in_use_by_strategy)."""
    code = "account_in_use_by_strategy"

CURRENCIES = ("USD",)
PRINCIPAL = "principal"                    # the strategy every desk has (SPEC-027 3.2)
MAX_STRATEGY_KEY = 80
LINK_ORIGINS = ("owner", "chat")
MAX_NAME = 60
ACCOUNT_STATUSES = ("active", "archived")
ORDER_ACTIONS = ("preview", "place", "replace", "cancel")
RULE_RESULTS = ("auto", "needs_owner", "refused")
APPROVERS = ("auto", "owner")
MAX_REASON = 2000
MAX_WHY = 4000
# Keys that carry an account identifier: a row of broker_orders never holds them (SPEC-015 18.6, 5.9).
_FORBIDDEN_KEYS = ("accountNumber", "hashValue")


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


@dataclass(frozen=True, slots=True)
class ExternalAccount(Record):
    """The link of a local account with an account of an outside broker: HMAC fingerprint and mask, never the identifier."""
    account_id: int
    fingerprint: str
    mask: str


@dataclass(frozen=True, slots=True)
class BrokerOrderRow(Record):
    JSONS = ("request_json", "response_json")
    id: int
    desk_id: int | None
    account_id: int | None
    broker_kind: str
    action: str
    agent_slug: str | None
    conversation_id: int | None
    task_id: int | None
    reason: str | None
    request_json: Any
    rule_result: str | None
    rule_why: str | None
    decision_id: int | None
    approved_by: str | None
    http_status: int | None
    broker_order_id: str | None
    replaces_order_id: str | None
    response_json: Any
    correl_id: str | None
    created_at: str
    strategy_key: str | None = None


def _carries_identifier(value: Any) -> bool:
    if isinstance(value, dict):
        return any(k in _FORBIDDEN_KEYS or _carries_identifier(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_carries_identifier(v) for v in value)
    return False


def _masked_json(name: str, value: Any) -> str | None:
    """JSON text of a request or response that has already been through the account mask."""
    if value is None:
        return None
    if _carries_identifier(value):
        raise ValidationError(f"{name} still carries an account identifier: mask it before saving")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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

    def rename_broker(self, id: int, name: str) -> BrokerRow:
        name = (name or "").strip()
        if not 1 <= len(name) <= MAX_NAME:
            raise ValidationError(f"broker name must have 1 to {MAX_NAME} characters")
        self.get_broker(id)
        self.s.update("brokers", {"id": id}, {"name": name})
        return self.get_broker(id)

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

    def list_all_accounts(self, broker_id: int) -> list[BrokerAccount]:
        """Every account of a broker, archived ones included (the sync with an outside broker needs them)."""
        rows = self.s.query("SELECT * FROM broker_accounts WHERE broker_id = :b ORDER BY id", {"b": broker_id})
        return [BrokerAccount.from_row(r) for r in rows]

    def set_account_status(self, id: int, status: str) -> BrokerAccount:
        """'archived' hides an account without deleting anything; 'active' brings it back."""
        require_in("status", status, ACCOUNT_STATUSES)
        self.get_account(id)
        self.s.update("broker_accounts", {"id": id}, {"status": status})
        return self.get_account(id)

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

    # ---- accounts of a desk (0024, SPEC-027): many to many; each strategy of a desk trades through one of them ----

    def link(self, desk_id: int, account_id: int, added_by: str = "owner") -> None:
        """Put an account in a desk. The same account may be in other desks. Raises AlreadyLinked when the pair exists."""
        require_in("added_by", added_by, LINK_ORIGINS)
        self.get_account(account_id)
        if self.s.scalar("SELECT 1 FROM desk_account_links WHERE desk_id = :d AND account_id = :a",
                         {"d": desk_id, "a": account_id}) is not None:
            raise AlreadyLinked(f"account {account_id} is already in desk {desk_id}")
        self.s.insert_row("desk_account_links", {"desk_id": desk_id, "account_id": account_id,
                                                 "added_at": utcnow(), "added_by": added_by})

    def accounts_of_desk(self, desk_id: int) -> list[BrokerAccount]:
        """The accounts of a desk, the oldest link first."""
        rows = self.s.query("SELECT a.* FROM broker_accounts a JOIN desk_account_links l ON l.account_id = a.id "
                            "WHERE l.desk_id = :d ORDER BY l.added_at, a.id", {"d": desk_id})
        return [BrokerAccount.from_row(r) for r in rows]

    def desks_of_account(self, account_id: int) -> list[int]:
        """The ids of the desks that hold an account, in id order."""
        rows = self.s.query("SELECT desk_id FROM desk_account_links WHERE account_id = :a ORDER BY desk_id", {"a": account_id})
        return [r["desk_id"] for r in rows]

    def unlink(self, desk_id: int, account_id: int) -> bool:
        """Take an account out of a desk. Refused (AccountInUseByStrategy) while a strategy of the desk trades through it.
        Returns False when the pair did not exist."""
        users = self.strategies_of_account(desk_id, account_id)
        if users:
            raise AccountInUseByStrategy(f"account {account_id} is used by the strategies {', '.join(users)} of desk {desk_id}")
        return self.s.execute("DELETE FROM desk_account_links WHERE desk_id = :d AND account_id = :a",
                              {"d": desk_id, "a": account_id}) > 0

    def bind_strategy(self, desk_id: int, strategy_key: str, account_id: int) -> None:
        """Say that a strategy of a desk trades through an account of that desk (an upsert: it moves the strategy)."""
        if not isinstance(strategy_key, str) or not 1 <= len(strategy_key) <= MAX_STRATEGY_KEY:
            raise ValidationError(f"strategy_key must have 1 to {MAX_STRATEGY_KEY} characters")
        if self.s.scalar("SELECT 1 FROM desk_account_links WHERE desk_id = :d AND account_id = :a",
                         {"d": desk_id, "a": account_id}) is None:
            raise NotLinked(f"account {account_id} is not in desk {desk_id}")
        if self.s.scalar("SELECT 1 FROM desk_strategy_accounts WHERE desk_id = :d AND strategy_key = :k",
                         {"d": desk_id, "k": strategy_key}) is None:
            self.s.insert_row("desk_strategy_accounts", {"desk_id": desk_id, "strategy_key": strategy_key, "account_id": account_id})
        else:
            self.s.update("desk_strategy_accounts", {"desk_id": desk_id, "strategy_key": strategy_key}, {"account_id": account_id})

    def unbind_strategy(self, desk_id: int, strategy_key: str) -> bool:
        return self.s.execute("DELETE FROM desk_strategy_accounts WHERE desk_id = :d AND strategy_key = :k",
                              {"d": desk_id, "k": strategy_key}) > 0

    def account_of_strategy(self, desk_id: int, strategy_key: str = PRINCIPAL) -> BrokerAccount | None:
        """The account a strategy of a desk trades through; None when it has none yet."""
        row = self.s.one("SELECT a.* FROM broker_accounts a JOIN desk_strategy_accounts s ON s.account_id = a.id "
                         "WHERE s.desk_id = :d AND s.strategy_key = :k", {"d": desk_id, "k": strategy_key})
        return BrokerAccount.from_row(row) if row else None

    def strategy_bindings(self, desk_id: int) -> dict[str, int]:
        """strategy_key -> account id for every strategy of the desk that has an account."""
        rows = self.s.query("SELECT strategy_key, account_id FROM desk_strategy_accounts WHERE desk_id = :d ORDER BY strategy_key",
                            {"d": desk_id})
        return {r["strategy_key"]: r["account_id"] for r in rows}

    def strategies_of_account(self, desk_id: int, account_id: int) -> list[str]:
        rows = self.s.query("SELECT strategy_key FROM desk_strategy_accounts WHERE desk_id = :d AND account_id = :a ORDER BY strategy_key",
                            {"d": desk_id, "a": account_id})
        return [r["strategy_key"] for r in rows]

    # ---- accounts of an outside broker (0020): the fingerprint is all that is kept of the identifier ----

    def add_external(self, account_id: int, fingerprint: str, mask: str) -> ExternalAccount:
        self.get_account(account_id)
        if not isinstance(fingerprint, str) or not 16 <= len(fingerprint) <= 128:
            raise ValidationError("fingerprint must have 16 to 128 characters")
        if not isinstance(mask, str) or not 1 <= len(mask) <= 16:
            raise ValidationError("mask must have 1 to 16 characters")
        self.s.insert_row("broker_external_accounts", {"account_id": account_id, "fingerprint": fingerprint, "mask": mask})
        return self.external_of(account_id)  # type: ignore[return-value]

    def external_of(self, account_id: int) -> ExternalAccount | None:
        return self._find("broker_external_accounts", "account_id", account_id, ExternalAccount)

    def find_by_fingerprint(self, fingerprint: str) -> BrokerAccount | None:
        row = self.s.one("SELECT a.* FROM broker_accounts a JOIN broker_external_accounts e ON e.account_id = a.id "
                         "WHERE e.fingerprint = :f", {"f": fingerprint})
        return BrokerAccount.from_row(row) if row else None

    def archive_account(self, id: int) -> BrokerAccount:
        return self.set_account_status(id, "archived")

    def archive_broker_accounts(self, broker_id: int) -> int:
        """Archive every active account of a broker (it was removed). Nothing is deleted. Returns how many changed."""
        return self.s.execute("UPDATE broker_accounts SET status = 'archived' WHERE broker_id = :b AND status = 'active'",
                              {"b": broker_id})

    def update_external_mask(self, account_id: int, mask: str) -> None:
        self.s.update("broker_external_accounts", {"account_id": account_id}, {"mask": mask})

    # ---- the record of every order attempt (0020, SPEC-015 18.6) ----

    def record_order(self, *, broker_kind: str, action: str, desk_id: int | None = None, account_id: int | None = None,
                     agent_slug: str | None = None, conversation_id: int | None = None, task_id: int | None = None,
                     reason: str | None = None, request: Any = None, rule_result: str | None = None,
                     rule_why: str | None = None, decision_id: int | None = None, approved_by: str | None = None,
                     http_status: int | None = None, broker_order_id: str | None = None,
                     replaces_order_id: str | None = None, response: Any = None,
                     correl_id: str | None = None, strategy_key: str | None = None) -> BrokerOrderRow:
        """One row per attempt. `request` and `response` must already be masked (account_ref and mask): a value that still
        carries 'accountNumber' or 'hashValue' is refused."""
        require_in("action", action, ORDER_ACTIONS)
        if rule_result is not None:
            require_in("rule_result", rule_result, RULE_RESULTS)
        if approved_by is not None:
            require_in("approved_by", approved_by, APPROVERS)
        if not 1 <= len(broker_kind or "") <= 40:
            raise ValidationError("broker_kind must have 1 to 40 characters")
        if reason is not None and len(reason) > MAX_REASON:
            raise ValidationError(f"reason must have at most {MAX_REASON} characters")
        if strategy_key is not None and not 1 <= len(strategy_key) <= MAX_STRATEGY_KEY:
            raise ValidationError(f"strategy_key must have 1 to {MAX_STRATEGY_KEY} characters")
        if rule_why is not None and len(rule_why) > MAX_WHY:
            rule_why = rule_why[:MAX_WHY]
        new_id = self.s.insert("broker_orders", {
            "desk_id": desk_id, "account_id": account_id, "broker_kind": broker_kind, "action": action,
            "agent_slug": agent_slug, "conversation_id": conversation_id, "task_id": task_id, "reason": reason,
            "request_json": _masked_json("request", request), "rule_result": rule_result, "rule_why": rule_why,
            "decision_id": decision_id, "approved_by": approved_by, "http_status": http_status,
            "broker_order_id": None if broker_order_id is None else str(broker_order_id),
            "replaces_order_id": None if replaces_order_id is None else str(replaces_order_id),
            "response_json": _masked_json("response", response), "correl_id": correl_id, "created_at": utcnow(),
            "strategy_key": strategy_key})
        return self.get_order_record(new_id)

    def get_order_record(self, id: int) -> BrokerOrderRow:
        return self._get("broker_orders", "id", id, BrokerOrderRow)

    def list_order_records(self, desk_id: int, since: str | None = None, limit: int = 50) -> list[BrokerOrderRow]:
        """The newest first. `since` is an ISO UTC instant as written by utcnow()."""
        limit = max(1, min(int(limit), 500))
        sql, params = "SELECT * FROM broker_orders WHERE desk_id = :d", {"d": desk_id}
        if since:
            sql, params["s"] = sql + " AND created_at >= :s", since
        rows = self.s.query(sql + f" ORDER BY id DESC LIMIT {limit}", params)
        return [BrokerOrderRow.from_row(r) for r in rows]

    def count_order_records(self, desk_id: int, action: str, since: str) -> int:
        """How many attempts of an action a desk made since an instant (the daily order limit counts with it)."""
        require_in("action", action, ORDER_ACTIONS)
        return int(self.s.scalar("SELECT count(*) FROM broker_orders WHERE desk_id = :d AND action = :a AND created_at >= :s "
                                 "AND (rule_result IS NULL OR rule_result <> 'refused')",
                                 {"d": desk_id, "a": action, "s": since}) or 0)

    def count_sent_orders(self, desk_id: int, since: str) -> int:
        """Orders the broker accepted for a desk since an instant: place and replace attempts that got a 2xx answer (the daily
        order limit of 18.5 counts with it; an attempt that was refused or is waiting for the owner does not count)."""
        return int(self.s.scalar("SELECT count(*) FROM broker_orders WHERE desk_id = :d AND action IN ('place', 'replace') "
                                 "AND http_status BETWEEN 200 AND 299 AND created_at >= :s", {"d": desk_id, "s": since}) or 0)

    def placed_order_refs(self, desk_id: int, account_id: int) -> list[str]:
        """The ids the broker gave to the orders a desk sent through an account (place and replace answered 2xx)."""
        rows = self.s.query("SELECT DISTINCT broker_order_id FROM broker_orders WHERE desk_id = :d AND account_id = :a "
                            "AND action IN ('place', 'replace') AND http_status BETWEEN 200 AND 299 AND broker_order_id IS NOT NULL",
                            {"d": desk_id, "a": account_id})
        return [r["broker_order_id"] for r in rows]

    def pending_order_records(self, desk_id: int | None = None) -> list[BrokerOrderRow]:
        """The attempts that wait for the owner: a 'needs_owner' row whose decision is still 'proposed', oldest first."""
        sql = ("SELECT o.* FROM broker_orders o JOIN decisions d ON d.id = o.decision_id "
               "WHERE o.rule_result = 'needs_owner' AND d.status = 'proposed'")
        params: dict[str, Any] = {}
        if desk_id is not None:
            sql, params = sql + " AND o.desk_id = :d", {"d": desk_id}
        return [BrokerOrderRow.from_row(r) for r in self.s.query(sql + " ORDER BY o.id", params)]

    def order_record_for_decision(self, decision_id: int) -> BrokerOrderRow | None:
        """The 'needs_owner' attempt that created a decision (it carries the whole order)."""
        row = self.s.one("SELECT * FROM broker_orders WHERE decision_id = :d AND rule_result = 'needs_owner' ORDER BY id LIMIT 1",
                         {"d": decision_id})
        return BrokerOrderRow.from_row(row) if row else None

    # ---- the optional limits of a desk (0020, SPEC-015 18.5): no row means no limit ----

    def get_limits(self, desk_id: int) -> dict[str, Any]:
        raw = self.s.scalar("SELECT config FROM desk_limits WHERE desk_id = :d", {"d": desk_id})
        parsed = loads_json(raw)
        return parsed if isinstance(parsed, dict) else {}

    def set_limits(self, desk_id: int, config: dict[str, Any]) -> dict[str, Any]:
        """Replace the limits of a desk (an upsert). The keys and values are checked by the desks service, not here."""
        if not isinstance(config, dict):
            raise ValidationError("limits must be an object")
        text = json.dumps(config, separators=(",", ":"))
        if self.s.scalar("SELECT 1 FROM desk_limits WHERE desk_id = :d", {"d": desk_id}) is None:
            self.s.insert_row("desk_limits", {"desk_id": desk_id, "config": text})
        else:
            self.s.update("desk_limits", {"desk_id": desk_id}, {"config": text})
        return self.get_limits(desk_id)
