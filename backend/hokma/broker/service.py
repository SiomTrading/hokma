"""Brokers and accounts for the app and the chat (task 3294, C7-5c). One service behind the routes and the tools.
The internal broker ("sim") and Schwab ("schwab", SPEC-015 18.2) are opened by `brokers.kind`. A reset needs `confirmed=True`
here, not only on the screen, and a broker that cannot reset (Schwab) refuses it here too."""
from __future__ import annotations

import datetime as dt
import hmac
import json
import logging
import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from hokma.broker import external, rules
from hokma.broker.contract import Broker, NotSupported
from hokma.broker.schwab import KIND as SCHWAB_KIND
from hokma.broker.schwab import SchwabBroker
from hokma.broker.sim import SimBroker
from hokma.db import Actor, Database, IntegrityViolation, NotFound, RuleViolation, ValidationError, get_database
from hokma.db.repos.broker import BrokerAccount, BrokerRow, NameTaken
from hokma.db.timeutil import from_db_time, to_db_time, utcnow
from hokma.exec import approvals as exec_approvals
from hokma.exec import native as exec_native
from hokma.exec.submit import dispatch as exec_dispatch
from hokma.exec.proposal import fingerprint_of
from hokma.i18n import t
from hokma.schwab import accounts as schwab_accounts
from hokma.schwab import orders as schwab_orders
from hokma.schwab import reader as schwab_reader
from hokma.schwab.http import NOT_FOUND, UNREACHABLE, UPSTREAM, SchwabError

log = logging.getLogger("hokma.broker")

PENDING_MINUTES = 30                 # an intervention that nobody answers expires (SPEC-015 18.3, decision of Cristian)
_ORDER_ID = re.compile(r"[0-9]{1,19}")

_MESSAGES: dict[str, Callable[[], str]] = {      # one literal t(...) per message, for the catalog check
    "broker_not_found": lambda: t("broker.err.broker_not_found"),
    "account_not_found": lambda: t("broker.err.account_not_found"),
    "desk_not_found": lambda: t("broker.err.desk_not_found"),
    "invalid": lambda: t("broker.err.invalid"),
    "name_taken": lambda: t("broker.err.name_taken"),
    "not_confirmed": lambda: t("broker.err.not_confirmed"),
    "account_in_use": lambda: t("broker.err.account_in_use"),
    "desk_has_account": lambda: t("broker.err.desk_has_account"),
    "desk_without_account": lambda: t("broker.err.desk_without_account"),
    "not_supported": lambda: t("broker.err.not_supported"),
    "orders_off": lambda: t("broker.err.orders_off"),
    "desk_inactive": lambda: t("broker.err.desk_inactive"),
    "no_trading_account": lambda: t("broker.err.no_trading_account"),
    "rejected": lambda: t("broker.err.rejected", reason=""),      # always raised with the text that carries Schwab's reason
    "not_editable": lambda: t("broker.err.not_editable"),
    "order_not_found": lambda: t("broker.err.order_not_found"),
    "bad_reason": lambda: t("broker.err.bad_reason"),
    "bad_order_id": lambda: t("broker.err.bad_order_id"),
    "decision_not_found": lambda: t("broker.err.decision_not_found"),
    "decision_closed": lambda: t("broker.err.decision_closed"),
    "decision_expired": lambda: t("broker.err.decision_expired", minutes=str(PENDING_MINUTES)),
    "bad_limits": lambda: t("broker.err.bad_limits", detail=""),
    "owner_required": lambda: t("broker.err.owner_required"),
}


class BrokerError(Exception):
    """`code` is stable for the API; `message` is in the student's language (or `text`, when the caller has the exact one:
    a refusal with the reason of Schwab, an error of the Schwab door)."""

    def __init__(self, code: str, status: int = 400, text: str | None = None) -> None:
        super().__init__(code)
        self.code, self.status, self.text = code, status, text

    @property
    def message(self) -> str:
        return self.text or _MESSAGES[self.code]()


def owner_only(what: str) -> BrokerError:
    """The refusal of a change that only the owner may make (SPEC-015 bug 1220): raising the capital of a desk, loosening or
    turning off a limit, turning the orders of a broker on, assigning or taking off an account. The text is for the agent."""
    return BrokerError("owner_only", 403, text=f"{what} is the owner's to decide: it needs the owner's permission. "
                       "Ask the owner, who changes it in the app or approves desk_money_set.")


def _db(db: Database | None) -> Database:
    return db or get_database()


def _open(u, row: BrokerRow) -> Broker:
    """The broker class of a row, by `brokers.kind`."""
    if row.kind == SCHWAB_KIND:
        return SchwabBroker(u.broker, row)
    if row.kind == "sim":
        return SimBroker(u.broker, row)
    if row.kind in external.CONNECTORS:                          # alpaca, ibkr, crypto: opened on first use (SPEC-018 B5)
        return external.connector_class(row.kind)(u.broker, row)
    raise BrokerError("broker_not_found", 404)


def orders_enabled(row: BrokerRow) -> bool:
    """The switch of 18.2 point 13 (`brokers.config.orders_enabled`): false unless it was turned on."""
    return row.config.get("orders_enabled") is True


def broker_of_desk(u, desk_id: int) -> tuple[Broker, BrokerAccount] | None:
    """The broker and the account of a desk, or None while it has no account (the rules of 18.3 start here)."""
    account = u.broker.account_of_desk(desk_id)
    return None if account is None else (_broker(u, account.broker_id), account)


def _broker(u, id: int) -> Broker:
    try:
        return _open(u, u.broker.get_broker(id))
    except NotFound as exc:
        raise BrokerError("broker_not_found", 404) from exc


def _account(u, id: int) -> tuple[Broker, BrokerAccount]:
    try:
        account = u.broker.get_account(id)
    except NotFound as exc:
        raise BrokerError("account_not_found", 404) from exc
    return _broker(u, account.broker_id), account


def _broker_view(b: Broker) -> dict[str, Any]:
    """`commission` and `slippage_bps` are the internal broker's costs (None in a broker without them); `orders_enabled` is
    the switch of 18.2 point 13, false by default and always false in a broker that cannot take orders."""
    return {"id": b.id, "kind": b.kind, "name": b.row.name, "commission": getattr(b, "commission", None),
            "slippage_bps": getattr(b, "slippage_bps", None),
            "orders_enabled": b.capabilities.orders and orders_enabled(b.row),
            "capabilities": {"accounts": b.capabilities.accounts, "reset": b.capabilities.reset,
                             "orders": b.capabilities.orders, "positions": b.capabilities.positions}}


def _account_view(u, b: Broker, account: BrokerAccount) -> dict[str, Any]:
    """`cash` and `equity` are None when the broker cannot work them out: never a made-up 0."""
    bal = b.balance(account.id)
    link = u.broker.external_of(account.id)
    return {"id": account.id, "broker_id": b.id, "broker": b.row.name, "name": account.name,
            "mask": link.mask if link else None, "can_reset": b.capabilities.reset,
            "initial_capital": account.initial_capital, "currency": account.currency, "status": account.status,
            "desk_id": u.broker.desk_of_account(account.id),
            "cash": bal.cash, "equity": bal.equity, "as_of": bal.as_of}


def desk_account(u, desk_id: int) -> dict[str, Any] | None:
    """What a desk summary carries: `{id, name, broker, initial_capital, balance, as_of}`, or None while it has no account."""
    account = u.broker.account_of_desk(desk_id)
    if account is None:
        return None
    b = _broker(u, account.broker_id)
    bal = b.balance(account.id)
    return {"id": account.id, "name": account.name, "broker": b.row.name, "initial_capital": account.initial_capital,
            "balance": {"cash": bal.cash, "equity": bal.equity, "currency": bal.currency}, "as_of": bal.as_of}


def _guard(fn):
    """A repeated name is 409 `name_taken`; any other refused input is 400 `invalid`."""
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except IntegrityViolation as exc:
            raise BrokerError("name_taken", 409) from exc
        except NameTaken as exc:
            raise BrokerError("name_taken", 409) from exc
        except NotSupported as exc:
            raise BrokerError("not_supported", 409) from exc
        except ValidationError as exc:
            raise BrokerError("invalid") from exc
    return run


def list_brokers(db: Database | None = None) -> list[dict[str, Any]]:
    with _db(db).read() as u:
        return [_broker_view(_open(u, r)) for r in u.broker.list_brokers()]


@_guard
def create_broker(name: Any, commission: Any = None, slippage_bps: Any = None, db: Database | None = None) -> dict[str, Any]:
    with _db(db).write() as u:
        broker = _open(u, u.broker.create_broker("sim", name))
        broker.set_costs(commission, slippage_bps)
        return _broker_view(broker)


@_guard
def update_costs(broker_id: int, commission: Any = None, slippage_bps: Any = None, db: Database | None = None) -> dict[str, Any]:
    """Change the commission and/or the slippage of a broker; what is not given stays."""
    with _db(db).write() as u:
        b = _broker(u, broker_id)
        if not hasattr(b, "set_costs"):
            raise BrokerError("not_supported", 409)
        b.set_costs(commission, slippage_bps)
        return _broker_view(b)


def set_orders_enabled(broker_id: int, enabled: Any, db: Database | None = None) -> dict[str, Any]:
    """Turn the orders of a broker on or off (18.2 point 13). Only a broker that takes orders has the switch: 422 otherwise.
    Off is how every broker starts."""
    if not isinstance(enabled, bool):
        raise BrokerError("invalid", 422)
    with _db(db).write() as u:
        b = _broker(u, broker_id)
        if not b.capabilities.orders:
            raise BrokerError("not_supported", 422)
        b.row = u.broker.set_broker_config(b.id, {**b.row.config, "orders_enabled": enabled})
        return _broker_view(b)


def schwab_orders_enabled(db: Database | None = None) -> bool:
    """The switch of the Schwab broker for the Connections panel; false while Schwab has not been connected."""
    with _db(db).read() as u:
        return any(orders_enabled(r) for r in u.broker.list_brokers() if r.kind == SCHWAB_KIND)


def list_accounts(broker_id: int, db: Database | None = None) -> list[dict[str, Any]]:
    with _db(db).read() as u:
        b = _broker(u, broker_id)
        return [_account_view(u, b, a) for a in b.list_accounts()]


@_guard
def create_account(broker_id: int, name: Any, capital: Any, db: Database | None = None) -> dict[str, Any]:
    with _db(db).write() as u:
        b = _broker(u, broker_id)
        return _account_view(u, b, b.create_account(name, capital))


def get_account(account_id: int, db: Database | None = None) -> dict[str, Any]:
    with _db(db).read() as u:
        b, account = _account(u, account_id)
        return _account_view(u, b, account)


@_guard
def reset_account(account_id: int, confirmed: Any, db: Database | None = None) -> dict[str, Any]:
    if confirmed is not True:
        raise BrokerError("not_confirmed")
    with _db(db).write() as u:
        b, account = _account(u, account_id)
        if not b.capabilities.reset:                             # Schwab: its money is in Schwab, here and not only on screen
            raise BrokerError("not_supported", 409)
        return _account_view(u, b, b.reset(account.id))


def assign_account(desk_id: int, account_id: Any, db: Database | None = None) -> dict[str, Any]:
    """One account serves one desk and a desk has one account: both refusals are 409 with their own code."""
    try:
        with _db(db).write() as u:
            if u.desks.find(desk_id) is None:
                raise BrokerError("desk_not_found", 404)
            _account(u, account_id if isinstance(account_id, int) and not isinstance(account_id, bool) else 0)
            owner = u.broker.desk_of_account(account_id)
            if owner == desk_id:
                return desk_account(u, desk_id)                  # same assignment again: nothing changes
            if owner is not None:
                raise BrokerError("account_in_use", 409)
            if u.broker.account_of_desk(desk_id) is not None:
                raise BrokerError("desk_has_account", 409)
            u.broker.link_desk(desk_id, account_id)
            return desk_account(u, desk_id)
    except IntegrityViolation as exc:                            # two assignments at once: the second loses
        raise BrokerError("account_in_use", 409) from exc


def unassign_account(desk_id: int, db: Database | None = None) -> dict[str, Any]:
    with _db(db).write() as u:
        if u.desks.find(desk_id) is None:
            raise BrokerError("desk_not_found", 404)
        if u.broker.account_of_desk(desk_id) is None:
            raise BrokerError("desk_without_account", 404)
        u.broker.unlink_desk(desk_id)
        return {"desk_id": desk_id, "account": None}


# ---------------------------------------------------------------------------------------------------------------------
# Orders of a desk (SPEC-015 18.2 to 18.6). The tools of the chat and the routes of the app call these functions. They block
# (network): call them from a thread. The rules are in hokma.broker.rules; the four calls to Schwab are in hokma.schwab.orders
# and reach it only through the broker object (`broker/schwab.py`). No account number, hash or token is ever in a record, a
# journal entry, a decision, a log line or an answer: an account is its `account_ref` and its mask.
# ---------------------------------------------------------------------------------------------------------------------

_REFUSAL_STATUS = {"orders_off": 409, "desk_inactive": 409, "no_trading_account": 409, "rejected": 422,
                   "not_editable": 409, "order_not_found": 404}
_JOURNAL: dict[str, Callable[..., str]] = {         # one literal t(...) per message, for the catalog check
    "sent": lambda **p: t("broker.journal.sent", **p),
    "replaced": lambda **p: t("broker.journal.replaced", **p),
    "canceled": lambda **p: t("broker.journal.canceled", **p),
    "refused": lambda **p: t("broker.journal.refused", **p),
    "needs_owner": lambda **p: t("broker.journal.needs_owner", **p),
    "failed": lambda **p: t("broker.journal.failed", **p),
    "not_sent": lambda **p: t("broker.journal.not_sent", **p),
    "expired": lambda **p: t("broker.journal.expired", **p),
    "rejected": lambda **p: t("broker.journal.rejected", **p),
}
_OUTCOME = {"sent": "success", "replaced": "success", "canceled": "success", "refused": "failure", "failed": "failure",
            "not_sent": "failure", "needs_owner": "info", "expired": "info", "rejected": "info"}


# The start secret of the owner (SPEC-015 bug 1221). `Who(owner=True)` skips the question to the owner, so only the code of the
# app may build it: it holds a secret that is generated once, in memory, the first time the app asks for it (the engine does at
# start, when it loads the routes of the owner). It is never written to disk, to the environment of any process, to a log or to
# the arguments of a child process, and no tool, route or agent can read it. A process that never asks for it (the MCP server of
# the tools, a terminal, a child of the engine) has none, so it cannot build an owner at all.
_OWNER_KEY: str | None = None
_OWNER_LOCK = threading.Lock()


def _owner_key() -> str:
    global _OWNER_KEY
    with _OWNER_LOCK:
        if _OWNER_KEY is None:
            _OWNER_KEY = secrets.token_urlsafe(32)
        return _OWNER_KEY


def _is_owner_key(candidate: Any) -> bool:
    key = _OWNER_KEY
    if key is None or not isinstance(candidate, str):
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), key.encode("utf-8"))      # constant time


@dataclass(frozen=True, slots=True)
class Who:
    """Who asks: an agent (from the environment of the engine) or the owner in the app (`owner=True`: the owner is the one
    who approves, so a soft condition does not ask again). `owner=True` holds only with the start secret in `proof` (use
    `owner_who()`); without it, or with a wrong one, the caller is treated as an agent. The secret is not kept in the object."""
    agent: str | None = None
    conversation_id: int | None = None
    task_id: int | None = None
    owner: bool = False
    proof: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.owner and not _is_owner_key(self.proof):
            log.warning("owner=True without the start secret of the app: treated as an agent")
            object.__setattr__(self, "owner", False)
        object.__setattr__(self, "proof", None)


def owner_who() -> Who:
    """The owner of the app. Only the routes of the app (`broker/api.py`) call it; no tool does."""
    return Who(owner=True, proof=_owner_key())


@dataclass(slots=True)
class _Out:
    """One attempt, from the request to the answer. `status`: previewed, sent, replaced, canceled, refused, needs_owner, failed
    (the broker answered with an error) or not_sent (the owner approved but the new check did not pass)."""
    action: str
    desk_id: int
    who: Who
    reason: str | None = None
    order: Any = None
    order_id: Any = None                       # the order that a replace changes or a cancel removes
    decision_id: int | None = None
    approval: set | None = None                # the soft conditions the owner approved (an approved decision being sent)
    approved_value: float | None = None        # what the order was worth when the owner approved it (C2: 5% more asks again)
    twin: int | None = None                    # decision of an identical order that is still waiting (C4b)
    status: str = "refused"
    request: Any = None                        # the order as it can be saved (None while it is not valid)
    rule: dict | None = None
    preview: Any = None
    sent: dict | None = None
    http_status: int | None = None
    correl_id: str | None = None
    approved_by: str | None = None
    error: BrokerError | None = None
    why: str | None = None
    desk_name: str = ""
    project_id: int | None = None
    account_id: int | None = None
    broker_kind: str = "none"
    notes: list = field(default_factory=list)


@dataclass(slots=True)
class _Ctx:
    desk: Any
    account: BrokerAccount | None
    broker: Broker | None
    trading: bool                              # an active account of a broker that takes orders
    enabled: bool                              # ... and its switch is on
    limits: dict
    assets: list


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _day_start() -> str:
    return _now().strftime("%Y-%m-%d 00:00:00")


def _load(u, desk_id: Any) -> _Ctx:
    desk = u.desks.find(desk_id) if isinstance(desk_id, int) and not isinstance(desk_id, bool) else None
    if desk is None:
        raise BrokerError("desk_not_found", 404)
    account = u.broker.account_of_desk(desk.id)
    broker = _broker(u, account.broker_id) if account is not None else None
    trading = bool(account is not None and account.status == "active" and broker is not None and broker.capabilities.orders)
    enabled = bool(trading and broker is not None and orders_enabled(broker.row))
    return _Ctx(desk, account, broker, trading, enabled, u.broker.get_limits(desk.id),
                [a.symbol for a in u.desks.list_assets(desk.id)])


def _need_trading(ctx: _Ctx) -> tuple[Broker, BrokerAccount]:
    if not ctx.trading or ctx.broker is None or ctx.account is None:
        raise BrokerError("no_trading_account", 409)
    return ctx.broker, ctx.account


def _reason(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2000:
        raise BrokerError("bad_reason", 422)
    return value.strip()


def _order_id(value: Any) -> str:
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(text, str) or not _ORDER_ID.fullmatch(text):
        raise BrokerError("bad_order_id", 422)
    return text


def _schwab_error(error: SchwabError) -> BrokerError:
    """An error of the Schwab door as the catalog text of the student, never with an identifier."""
    text = schwab_reader.message_of(error)
    if error.code in (UNREACHABLE, UPSTREAM) and error.detail:    # "the outcome is unknown: read the orders before sending again"
        text = f"{text} {error.detail}"
    return BrokerError(error.code, schwab_reader.status_of(error), text=text)


def _refuse(out: _Out, rule: dict) -> _Out:
    out.status, out.rule = "refused", rule
    out.error = BrokerError(rule["code"], _REFUSAL_STATUS.get(rule["code"], 409), text=rule["why"])
    return out


def _fail(out: _Out, error: SchwabError, *, sending: bool = False) -> _Out:
    out.error = _schwab_error(error)
    out.status = "failed" if sending else "refused"
    out.why = out.error.message
    out.http_status = error.status if 100 <= (error.status or 0) <= 599 else None
    out.correl_id = error.correl_id
    return out


def _run(db: Database, out: _Out) -> _Out:
    """Everything up to the call to the broker: the cheap refusals, the preview, the facts of the account, the rules and, when
    the rules allow it, the send. Nothing is written to the database here (`_record` does that)."""
    invalid: SchwabError | None = None
    if out.action == "cancel":
        out.request = None
    else:
        try:
            schwab_orders.validate_order(out.order)
            out.request = out.order
        except SchwabError as error:
            invalid = error
    with db.read() as u:
        ctx = _load(u, out.desk_id)
        out.desk_name, out.project_id = ctx.desk.name, ctx.desk.project_id
        out.account_id = ctx.account.id if ctx.account is not None else None
        out.broker_kind = ctx.broker.kind if ctx.broker is not None else "none"
        refusal = rules.gate(ctx.enabled, ctx.desk.status, ctx.trading)
        if refusal is not None and (out.action != "preview" or not ctx.trading):
            return _refuse(out, refusal)
        if invalid is not None:
            return _fail(out, invalid)
        broker, account = _need_trading(ctx)
        try:
            if out.action in ("replace", "cancel"):
                try:
                    current = broker.get_order(account.id, out.order_id)
                except SchwabError as error:
                    if error.code != NOT_FOUND:
                        raise
                    current = None                                  # Schwab answered 404: there is no such order
                if current is None:
                    return _refuse(out, {"result": "refused", "code": "order_not_found", "flags": [], "numbers": {},
                                         "why": t("broker.err.order_not_found")})
                flag = current.editable if out.action == "replace" else current.cancelable
                if flag is not True:
                    return _refuse(out, {"result": "refused", "code": "not_editable", "flags": [], "numbers": {},
                                         "why": t("broker.err.not_editable")})
            if out.action == "cancel":
                out.rule = rules.evaluate(action="cancel", orders_enabled=True, desk_status=ctx.desk.status, account_ok=True)
            else:
                out.preview = schwab_accounts.public(broker.preview_order(account.id, out.order))
                positions = broker.positions(account.id)
                open_orders = broker.orders(account.id)
                today = u.broker.count_sent_orders(ctx.desk.id, _day_start()) if "max_orders_per_day" in ctx.limits else 0
                out.rule = rules.evaluate(
                    action=out.action, orders_enabled=ctx.enabled, desk_status=ctx.desk.status, account_ok=ctx.trading,
                    order=out.order, preview=out.preview, capital=float(ctx.desk.capital), positions=positions,
                    open_orders=open_orders, limits=ctx.limits, assets=ctx.assets, orders_today=today,
                    replaces_order_id=out.order_id if out.action == "replace" else None)
        except SchwabError as error:
            return _fail(out, error)
        if out.action == "preview":
            out.status = "previewed"
            return out
        if out.action in ("place", "replace") and out.rule["result"] != "refused":
            filtered = exec_native.check(ctx.desk.id, out.order, preview=out.preview, who=out.who, db=db)       # the same guards as any proposal
            if filtered is not None:
                out.rule = filtered
        rule = out.rule
        if rule["result"] == "refused":
            return _refuse(out, rule)
        out.approved_by = "owner" if out.who.owner else "auto"        # the owner placing it in the app is its own approval
        if out.approval is not None:                                # a decision the owner approved, checked again
            if rule["result"] == "needs_owner" and not set(rule["flags"]) <= out.approval:
                out.status, out.why = "not_sent", rule["why"]
                out.approved_by = None
                return out
            rose = _value_rose(out.approved_value, rule)
            if rose:                                                # worth more than 5% above what the owner saw: ask again
                out.rule = {**rule, "result": "needs_owner", "code": rule["code"] if rule["flags"] else "value_rose",
                            "flags": [*rule["flags"], "value_rose"], "why": "; ".join(x for x in (rule["why"], rose) if x)}
                out.status, out.approved_by = "needs_owner", None
                return out
            out.approved_by = "owner"
        elif rule["result"] == "needs_owner":
            if not out.who.owner:
                out.status, out.approved_by = "needs_owner", None
                return out
            out.approved_by = "owner"                                # the owner placed it in the app: nothing to ask
        try:
            out.sent, out.http_status, out.status = exec_dispatch(broker, account.id, out.action, out.order, out.order_id)
            out.correl_id = (out.sent or {}).get("correl_id")
        except SchwabError as error:
            return _fail(out, error, sending=True)
    return out


VALUE_RISE = 1.05                                              # an approved order may be worth up to 5% more when it is sent


def _value_rose(approved: float | None, rule: dict) -> str:
    """The text that asks again when the order is now worth more than 5% above what the owner approved, else ''."""
    now = rule.get("numbers", {}).get("order_value")
    if approved is None or not isinstance(now, (int, float)) or isinstance(now, bool):
        return ""
    if now > approved * VALUE_RISE:
        return t("broker.why.value_rose", now=f"{now:,.2f}", approved=f"{approved:,.2f}")
    return ""


def _response(out: _Out) -> dict[str, Any] | None:
    body: dict[str, Any] = {}
    if out.sent is not None:
        body.update(order_id=out.sent.get("order_id"), correl_id=out.sent.get("correl_id"))
    if out.rule is not None:
        body["rule"] = out.rule
    if out.preview is not None:
        body["preview"] = out.preview
    if out.error is not None:
        body["error"] = {"code": out.error.code, "status": out.error.status}
    return body or None


def _rule_fields(out: _Out) -> tuple[str, str | None]:
    if out.status == "not_sent":
        return "refused", out.why
    if out.rule is None:                                           # failed before the rules: the broker's own error
        return "refused", out.why or (out.error.message if out.error else None)
    if out.status == "failed" and out.rule["result"] == "refused":
        return "refused", out.rule["why"]
    return out.rule["result"], out.rule["why"] or None


def _summary(out: _Out) -> str:
    return f"order {out.order_id}" if out.action == "cancel" else rules.summary(out.order)


def _actor(u, who: Who) -> Actor:
    if who.agent and u.agents.find(who.agent) is not None:
        return Actor.agent(who.agent)
    return Actor.human("owner") if who.owner else Actor.system("hokma")


def _journal(u, kind: str, *, desk_name: str, summary: str, project_id: int | None, who: Who, body: str | None) -> None:
    """One 'action' entry in the journal of the project of the desk, with the outcome of the attempt."""
    task_id = who.task_id if who.task_id is not None and u.tasks.find(who.task_id) is not None else None
    title = _JOURNAL[kind](desk=desk_name, summary=summary)[:200]
    u.journal.add("action", title, actor=_actor(u, who), outcome=_OUTCOME[kind], task_id=task_id, project_id=project_id,
                  body=(body or None) and body[:8000])


def _propose(u, out: _Out) -> int:
    """The pending decision of an order that needs the owner: the whole order stays in the record of the attempt; the decision
    carries what the owner reads (what, who, why) and the order as JSON when it fits."""
    import json

    summary = rules.summary(out.order)
    topic = (t("broker.decision.title_replace", desk=out.desk_name, order=summary) if out.action == "replace"
             else t("broker.decision.title_place", desk=out.desk_name, order=summary))[:200]
    proposal = t("broker.decision.body", agent=out.who.agent or "-", desk=out.desk_name, order=summary,
                 reason=out.reason or "-", why=out.rule["why"], minutes=str(PENDING_MINUTES))
    if out.action == "replace":
        proposal += " " + t("broker.decision.replaces", order_id=str(out.order_id))
    text = json.dumps(out.order, ensure_ascii=False, separators=(",", ":"))
    proposer = next((s for s in (out.who.agent, "desk-chief") if s and u.agents.find(s) is not None), None)
    if proposer is None:
        known = u.agents.list()
        if not known:
            raise BrokerError("invalid")
        proposer = known[0].slug
    decision = u.decisions.propose(topic, proposal, proposed_by=proposer, context=text if len(text) <= 8000 else None,
                                   project_id=out.project_id)
    return decision.id


def _notify(u, out: _Out) -> None:
    """Tell the owner (Telegram outbox, when linked) that an order waits: same channel as the `notify_owner` tool."""
    from hokma.kit import hokma_tools

    try:
        hokma_tools.notify_owner(u, None, {"text": t("broker.needs_owner", desk=out.desk_name, why=out.rule["why"])})
    except Exception:                                              # a notice that cannot be queued never blocks the record
        log.warning("the owner notice of a pending order could not be queued")


def _record(db: Database, out: _Out, *, approval_run: bool = False) -> dict[str, Any]:
    """Write the attempt (`broker_orders`, the journal, the decision and the notice), then return the answer of the tool or raise
    its error. An order that already left is never lost to a failed record: it is answered as sent and the failure is logged."""
    sent_out = out.status in ("sent", "replaced", "canceled")
    decision_id = out.decision_id
    try:
        with db.write() as u:
            if out.status == "needs_owner":
                out.twin = _open_twin(u, out)
                decision_id = out.twin if out.twin is not None else _propose(u, out)
            rule_result, rule_why = _rule_fields(out)
            sent = out.sent or {}
            row = u.broker.record_order(
                broker_kind=out.broker_kind, action=out.action, desk_id=out.desk_id, account_id=out.account_id,
                agent_slug=out.who.agent, conversation_id=out.who.conversation_id, task_id=out.who.task_id,
                reason=out.reason, request=out.request, rule_result=rule_result, rule_why=rule_why,
                decision_id=None if out.twin is not None else decision_id,      # the pending list shows one row per decision
                approved_by=out.approved_by if sent_out or out.status == "failed" else None,
                http_status=out.http_status, broker_order_id=sent.get("order_id"),
                replaces_order_id=out.order_id if out.action == "replace" else None, response=_response(out),
                correl_id=out.correl_id)
            if out.action != "preview" and out.twin is None:
                kind = "refused" if out.status == "refused" else out.status
                _journal(u, kind, desk_name=out.desk_name, summary=_summary(out), project_id=out.project_id, who=out.who,
                         body="\n".join(x for x in (out.reason, rule_why, f"order_id {sent.get('order_id')}" if sent.get("order_id") else None,
                                                    f"correl_id {out.correl_id}" if out.correl_id else None) if x))
            if out.status == "needs_owner" and out.twin is None:
                _notify(u, out)
            record_id = row.id
    except Exception:
        if not sent_out:
            raise
        log.error("an order was sent but its record could not be written (%s)", out.action)
        record_id = None
    return _answer(out, record_id, decision_id, approval_run)


def _canonical(action: str, desk_id: Any, order: Any, replaces: Any) -> str:
    """The identity of an order that waits for the owner: the hash of its JSON with the keys in order, without the reason of the
    agent (the same order with another reason is the same order). Desk, action and the order it replaces are part of it."""
    body = {"desk": desk_id, "action": action, "replaces": None if replaces is None else str(replaces),
            "order": json.loads(json.dumps(order, default=str))}
    return fingerprint_of(body)


def _open_twin(u, out: _Out) -> int | None:
    """C4b: the id of the decision that still waits for this same order of this same desk, else None. An answer that expired
    does not count (the sweep rejects it); an approved or rejected decision is closed, so the order asks again."""
    mine = _canonical(out.action, out.desk_id, out.order, out.order_id if out.action == "replace" else None)
    for row in u.broker.pending_order_records(out.desk_id):
        if _canonical(row.action, row.desk_id, row.request_json, row.replaces_order_id) != mine:
            continue
        if not _expired(u.decisions.get(row.decision_id)):
            return row.decision_id
    return None


def _answer(out: _Out, record_id: int | None, decision_id: int | None, approval_run: bool) -> dict[str, Any]:
    if out.status == "previewed":
        return {"status": "previewed", "preview": out.preview, "rule": out.rule, "log_id": record_id}
    if out.status in ("sent", "replaced", "canceled"):
        sent = out.sent or {}
        answer = {"status": out.status, "order_id": sent.get("order_id"), "correl_id": sent.get("correl_id"),
                  "approved_by": out.approved_by, "log_id": record_id}
        if out.action != "cancel":
            answer["rule"] = out.rule
            answer["preview"] = out.preview
        if out.action == "replace":
            answer["replaces_order_id"] = str(out.order_id)
        if sent.get("order_id") is None and out.action != "cancel":
            answer["note"] = "the order was accepted but Schwab gave no id: read the orders of the account"
        return answer
    if out.status == "needs_owner":
        answer = {"status": "needs_owner", "decision_id": decision_id, "why": out.rule["why"],
                  "expires_in_minutes": PENDING_MINUTES, "log_id": record_id}
        if out.twin is not None:                                   # the same order is already waiting: no new decision, no notice
            answer["already_pending"] = True
        return answer
    if approval_run:                                               # approved by the owner, but nothing went out
        return {"status": "not_sent", "decision_id": decision_id,
                "why": out.why or (out.error.message if out.error else ""), "log_id": record_id}
    assert out.error is not None
    raise out.error


def _attempt(desk_id: Any, action: str, who: Who, *, order: Any = None, order_id: Any = None, reason: Any = None,
             db: Database | None = None) -> dict[str, Any]:
    database = _db(db)
    out = _Out(action=action, desk_id=desk_id, who=who, order=order,
               order_id=None if order_id is None else _order_id(order_id),
               reason=_reason(reason) if action != "preview" or reason is not None else None)
    return _record(database, _run(database, out))


def preview(desk_id: int, order: Any, who: Who = Who(), db: Database | None = None) -> dict[str, Any]:
    """`previewOrder` with the whole request, and what the rules of 18.3 would say. Nothing is sent. Raises 409
    `no_trading_account` while the desk has no account that trades."""
    return _attempt(desk_id, "preview", who, order=order, db=db)


def place(desk_id: int, order: Any, reason: Any, who: Who = Who(), db: Database | None = None) -> dict[str, Any]:
    """Preview, rules and send (18.2 points 9 and 11). Answers `sent` with the order id, `needs_owner` with the pending
    decision, or raises the refusal (`orders_off`, `desk_inactive`, `no_trading_account`, `rejected`, the Schwab error)."""
    return _attempt(desk_id, "place", who, order=order, reason=reason, db=db)


def replace(desk_id: int, order_id: Any, order: Any, reason: Any, who: Who = Who(),
            db: Database | None = None) -> dict[str, Any]:
    """Reads the order, requires `editable`, previews the new one, applies the rules and replaces it (18.2 point 10)."""
    return _attempt(desk_id, "replace", who, order=order, order_id=order_id, reason=reason, db=db)


def cancel(desk_id: int, order_id: Any, reason: Any, who: Who = Who(), db: Database | None = None) -> dict[str, Any]:
    """Reads the order, requires `cancelable` and cancels it. A cancellation needs no preview and goes out on its own."""
    return _attempt(desk_id, "cancel", who, order_id=order_id, reason=reason, db=db)


# ---------------------------------------------------------------------------------------------------- pending decisions

def _expired(decision) -> bool:
    return _now() - from_db_time(decision.created_at) >= dt.timedelta(minutes=PENDING_MINUTES)


def _expire(db: Database, decision, row) -> bool:
    """The owner did not answer in time: the decision is rejected by Hokma, nothing is sent, and the log says so."""
    with db.write() as u:
        try:
            u.decisions.decide(decision.id, "rejected", actor=Actor.agent("hokma"))
        except RuleViolation:
            return False                                           # answered a moment ago
        why = t("broker.decision.expired", minutes=str(PENDING_MINUTES))
        _end_record(u, row, why, "expired", decision.id)
    return True


def _end_record(u, row, why: str, kind: str, decision_id: int) -> None:
    desk = u.desks.find(row.desk_id) if row.desk_id is not None else None
    u.broker.record_order(
        broker_kind=row.broker_kind, action=row.action, desk_id=row.desk_id, account_id=row.account_id,
        agent_slug=row.agent_slug, conversation_id=row.conversation_id, task_id=row.task_id, reason=row.reason,
        request=row.request_json, rule_result="refused", rule_why=why, decision_id=decision_id,
        replaces_order_id=row.replaces_order_id)
    who = Who(agent=row.agent_slug, task_id=row.task_id)
    _journal(u, kind, desk_name=desk.name if desk else "", summary=rules.summary(row.request_json) if row.replaces_order_id is None
             else f"order {row.replaces_order_id}", project_id=desk.project_id if desk else None, who=who, body=why)


def expire_pending(db: Database | None = None) -> int:
    """The sweep: every pending order decision older than 30 minutes is rejected. Called when decisions or the log are read and
    before an answer; there is no new scheduler. Returns how many expired."""
    database = _db(db)
    with database.read() as u:
        waiting = [(u.decisions.get(r.decision_id), r) for r in u.broker.pending_order_records()]
    return (sum(1 for decision, row in waiting if _expired(decision) and _expire(database, decision, row))
            + exec_approvals.expire_pending(database))


def _pending_of(db: Database, decision_id: Any, desk_id: int | None = None):
    with db.read() as u:
        try:
            decision = u.decisions.get(decision_id) if isinstance(decision_id, int) and not isinstance(decision_id, bool) else None
        except NotFound:
            decision = None
        row = u.broker.order_record_for_decision(decision.id) if decision is not None else None
    if decision is None or row is None or (desk_id is not None and row.desk_id != desk_id):
        raise BrokerError("decision_not_found", 404)
    if decision.status != "proposed":
        raise BrokerError("decision_closed", 409)
    return decision, row


def _exec_proposal(database: Database, decision_id: Any, desk_id: int | None) -> dict | None:
    """The proposal of `hokma.exec` that a decision belongs to, or None when it is a native order of SPEC-015."""
    if not isinstance(decision_id, int) or isinstance(decision_id, bool):
        return None
    row = exec_approvals.proposal_of_decision(decision_id, db=database)
    if row is None or (desk_id is not None and row["desk_id"] != desk_id):
        return None
    return row


def _exec_answer(result: Any, decision_id: int) -> dict[str, Any]:
    status = "rejected" if result.status == "canceled" else ("sent" if result.status in ("sent", "filled", "partially_filled") else "not_sent")
    return {"status": status, "decision_id": decision_id, "proposal_id": result.proposal_id, "execution": result.status, "why": result.why,
            "code": result.code, "log_id": result.broker_order_record_id}


def _need_owner(who: Who | None) -> None:
    if who is None or not who.owner:
        raise BrokerError("owner_required", 403)


def approve_pending(decision_id: int, decided_by: str = "owner", db: Database | None = None,
                    desk_id: int | None = None, who: Who | None = None) -> dict[str, Any]:
    """The owner approved an order decision in the app (`who` must be `owner_who()`: an agent cannot approve its own order).
    Past 30 minutes it expires instead (nothing is sent). Otherwise the order is previewed again and the rules are applied
    again with the prices of this moment: it is sent when it passes (with `approved_by = 'owner'`); when it does not, the
    decision stays approved without an order and the answer says why. When the order is now worth more than 5% above what the
    owner saw (the `order_value` kept in the record of the decision), it is not sent and a new decision asks again (C2)."""
    _need_owner(who)
    database = _db(db)
    if (proposal := _exec_proposal(database, decision_id, desk_id)) is not None:        # a proposal of a desk: hokma.exec sends it
        return _exec_answer(exec_approvals.approve(proposal["id"], who=who, db=database), decision_id)
    decision, row = _pending_of(database, decision_id, desk_id)
    if _expired(decision):
        _expire(database, decision, row)
        raise BrokerError("decision_expired", 409)
    with database.write() as u:
        approval = u.approvals.record("decision", decision.id, "approved", decided_by=decided_by, channel="dashboard")
        try:
            u.decisions.decide(decision.id, "approved", actor=Actor.human(decided_by), approval_id=approval.id)
        except RuleViolation as exc:
            raise BrokerError("decision_closed", 409) from exc
    rule_then = (row.response_json or {}).get("rule") or {}
    flags = rule_then.get("flags") or []
    seen = (rule_then.get("numbers") or {}).get("order_value")
    out = _Out(action=row.action, desk_id=row.desk_id, who=Who(agent=row.agent_slug, conversation_id=row.conversation_id,
                                                              task_id=row.task_id),
               reason=row.reason, order=row.request_json, order_id=row.replaces_order_id, decision_id=decision.id,
               approval=set(flags), approved_value=float(seen) if isinstance(seen, (int, float)) and not isinstance(seen, bool) else None)
    return _record(database, _run(database, out), approval_run=True)


def reject_pending(decision_id: int, decided_by: str = "owner", db: Database | None = None,
                   desk_id: int | None = None, who: Who | None = None) -> dict[str, Any]:
    """The owner rejected an order decision: nothing goes out and the log and the journal say so."""
    _need_owner(who)
    database = _db(db)
    if (proposal := _exec_proposal(database, decision_id, desk_id)) is not None:
        return _exec_answer(exec_approvals.reject(proposal["id"], who=who, db=database), decision_id)
    decision, row = _pending_of(database, decision_id, desk_id)
    with database.write() as u:
        approval = u.approvals.record("decision", decision.id, "rejected", decided_by=decided_by, channel="dashboard")
        try:
            u.decisions.decide(decision.id, "rejected", actor=Actor.human(decided_by), approval_id=approval.id)
        except RuleViolation as exc:
            raise BrokerError("decision_closed", 409) from exc
        _end_record(u, row, t("broker.decision.rejected"), "rejected", decision.id)
    return {"status": "rejected", "decision_id": decision.id}


def list_pending(desk_id: int, db: Database | None = None) -> list[dict[str, Any]]:
    """The orders of a desk that wait for the owner (oldest first), with the order and why it was held."""
    database = _db(db)
    expire_pending(database)
    with database.read() as u:
        _load(u, desk_id)
        out = []
        for row in u.broker.pending_order_records(desk_id):
            decision = u.decisions.get(row.decision_id)
            expires = from_db_time(decision.created_at) + dt.timedelta(minutes=PENDING_MINUTES)
            out.append({"decision_id": decision.id, "action": row.action, "agent": row.agent_slug, "reason": row.reason,
                        "why": row.rule_why, "order": row.request_json, "replaces_order_id": row.replaces_order_id,
                        "created_at": decision.created_at, "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S")})
    out += exec_approvals.list_pending(desk_id, db=database)          # the proposals of the desk that wait for the owner
    return sorted(out, key=lambda item: item["created_at"])


# --------------------------------------------------------------------------------------------------------------- reads

def order_log(desk_id: int, since: Any = None, limit: Any = 50, db: Database | None = None) -> dict[str, Any]:
    """Every attempt of the desk, newest first (`broker_orders`): agent, reason, order, rule result, answer."""
    database = _db(db)
    expire_pending(database)
    try:
        start = to_db_time(since) if since else None
        size = 50 if limit is None else int(limit)
    except (TypeError, ValueError) as exc:
        raise BrokerError("invalid", 422) from exc
    with database.read() as u:
        _load(u, desk_id)
        return {"desk_id": desk_id, "orders": [r.to_dict() for r in u.broker.list_order_records(desk_id, start, size)]}


def _identity(u, broker: Broker, account: BrokerAccount) -> dict[str, Any]:
    ref = broker.account_ref(account.id) if hasattr(broker, "account_ref") else None
    link = u.broker.external_of(account.id)
    return {"account_ref": ref, "mask": link.mask if link else None}


def _read(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except SchwabError as error:
        raise _schwab_error(error) from None


def order_list(desk_id: int, status: Any = None, fromEnteredTime: Any = None, toEnteredTime: Any = None,  # noqa: N803
               maxResults: Any = None, db: Database | None = None) -> dict[str, Any]:  # noqa: N803
    """The orders of the account of the desk with the filters of REF 3.2, every field, the account as account_ref and mask."""
    with _db(db).read() as u:
        broker, account = _need_trading(_load(u, desk_id))
        found = _read(broker.orders, account.id, status, fromEnteredTime, toEnteredTime, maxResults)
        return {"desk_id": desk_id, **_read(_identity, u, broker, account), "orders": [o.data for o in found]}


def order_status(desk_id: int, order_id: Any, db: Database | None = None) -> dict[str, Any]:
    """One order of the account of the desk with its executions."""
    number = _order_id(order_id)
    with _db(db).read() as u:
        broker, account = _need_trading(_load(u, desk_id))
        found = _read(broker.get_order, account.id, number)
        if found is None:
            raise BrokerError("order_not_found", 404)
        return {"desk_id": desk_id, **_read(_identity, u, broker, account), "order": found.data}


def position_list(desk_id: int, db: Database | None = None) -> dict[str, Any]:
    """The positions of the account of the desk: value, day result, open result, and the greeks of 16.16 (None, with the
    reason, when they cannot be read). Hokma keeps no copy: Schwab answers each time."""
    with _db(db).read() as u:
        broker, account = _need_trading(_load(u, desk_id))
        who = _read(_identity, u, broker, account)
        found = _read(broker.positions, account.id)
        rows = [{"symbol": p.symbol, "asset_type": p.asset_type, "quantity": p.qty, "long_quantity": p.long_qty,
                 "short_quantity": p.short_qty, "average_price": p.avg_price, "market_value": p.market_value,
                 "day_pnl": p.day_pl, "day_pnl_pct": p.day_pl_pct, "open_pnl": p.open_pl, "multiplier": p.multiplier,
                 "put_call": p.put_call, "underlying": p.underlying} for p in found]
        total = {"market_value": sum(abs(p.market_value or 0.0) for p in found),
                 "day_pnl": sum(p.day_pl or 0.0 for p in found), "open_pnl": sum(p.open_pl or 0.0 for p in found)}
        greeks, greeks_error = None, None
        if found and who["account_ref"]:
            try:
                greeks = schwab_reader.portfolio_greeks(who["account_ref"])
            except SchwabError as error:
                greeks_error = _schwab_error(error).message
        return {"desk_id": desk_id, **who, "positions": rows, "total": total, "greeks": greeks,
                "greeks_error": greeks_error, "as_of": utcnow()}


# ------------------------------------------------------------------------------------------------------- desk limits (18.5)

def get_limits(desk_id: int, db: Database | None = None) -> dict[str, Any]:
    """The optional limits of a desk; empty means none (all off, the default)."""
    with _db(db).read() as u:
        return _load(u, desk_id).limits


def set_limits(desk_id: int, changes: Any, db: Database | None = None, *, tighten_only: bool = False) -> dict[str, Any]:
    """Change the limits of a desk: only the given keys change, a `None` turns that limit off. Checked before saving.

    `tighten_only` is what the tools of the agents use (bug 1220): a change that loosens or turns off any limit, compared with
    the limits saved INSIDE this transaction, raises `owner_only` and saves nothing. The app of the owner does not set it."""
    with _db(db).write() as u:
        ctx = _load(u, desk_id)
        try:
            merged = rules.validate_limits(changes, ctx.limits)
        except ValueError as exc:
            raise BrokerError("bad_limits", 400, text=t("broker.err.bad_limits", detail=str(exc))) from exc
        if tighten_only and (loosened := rules.loosened_limits(ctx.limits, merged)):
            raise owner_only("loosening or turning off the limit " + ", ".join(loosened))
        return u.broker.set_limits(desk_id, merged)
