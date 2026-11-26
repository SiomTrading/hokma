"""The freeze of the loss of the day and of the drawdown, on what the BROKER reports (SPEC-026 v2, pieces 3390e and 3390c, task 3390).

WHAT IS MEASURED. Hokma does not work out the equity, the loss of the day or the result of a position with prices of its own. The loss of the day of an account is
its equity at the start of ITS session minus its equity now, both as the broker reports them (`hokma.broker.pnl.latest_pnl`); when the broker does not give the start,
the one the owner declared (`account_start_declarations`) stands. The drawdown is the highest equity of the readings of the session minus the equity now: Hokma keeps
only the maximum of what the broker said and values no position. Without a reading, or without a start, the measure is UNKNOWN: it is never read as zero, and a
rule that cannot be measured blocks the entries (`broker_data_missing`).

WHEN IT ACTS. Right at the cap of the rule, no earlier: there are no soft or hard lines (a warning before the cap is a rule of the owner, in notice mode). `LiveMonitor.evaluate`
runs on every new reading (`on_reading`), after a fill and on every tick, so the account is closed in the SAME event that reaches the cap. A rule of the ACCOUNT closes the
account for every desk that uses it; a rule of a DESK stops only that desk, and only where the account is used by that desk alone (the broker does not split the result by
desk: in a shared account the rule of the desk is unmeasured and blocks). The session of the account (hokma.exec.session), not the UTC date, says when the day ends and
the close is lifted.

WHAT IT CLOSES. `close_positions` cancels the live orders and closes each position without inverting it, under the lock of the account, and `resume_closes` brings back
a close that a restart left half done. The book of fills says what the account holds; the broker confirms the result."""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from hokma.backoffice import book as book_module
from hokma.backoffice.book import Book
from hokma.broker import pnl as broker_pnl
from hokma.db import Database, get_database
from hokma.exec import session as sessions
from hokma.exec import thresholds
from hokma.exec.money import ZERO
from hokma.i18n import t

log = logging.getLogger("hokma.exec.live")

FLATTEN_TRIPS = ("block_and_flatten", "block_and_flatten_all")
WATCHED_MODES = ("restriction", "notice")        # the monitor looks at both: a restriction closes, a notice tells the owner
CLOSE_RETRIES = 5                          # passes of the monitor (one per tick) that retry a close that left something open
CONTROLS = ("daily_loss", "daily_loss_pct", "drawdown", "drawdown_pct")
METRIC = {"daily_loss": "daily_loss", "daily_loss_pct": "daily_loss", "drawdown": "drawdown", "drawdown_pct": "drawdown"}
NO_DATA = "no_broker_data"                 # why a measure is unknown: the broker gave no (good) reading
START_UNKNOWN = "start_unknown"            # the broker gave the equity now but not the equity of the start, and the owner did not declare it

# What feeds the loss of the day of each kind of broker (said on the screen of the account). A kind that is not here has no reader of the result of the day.
SOURCES: dict[str, dict[str, Any]] = {
    "sim": {"available": True, "source": "the equity of the simulator, which is the broker of the account"},
    "schwab": {"available": True, "source": "the equity of the Trader API, read every few seconds and when the account stream says something happened"},
    "alpaca": {"available": True, "source": "the equity of the Alpaca account (GET /v2/account), read every few seconds"},
    "internal": {"available": True, "source": "the internal broker takes no orders: it has nothing open to freeze"},
}


def source_of(kind: str) -> dict[str, Any]:
    """Where the loss of the day comes from for a kind of broker. A kind that is not here gives no reading: the freeze is not available (no data from the broker)."""
    return {"kind": kind, **SOURCES.get(kind, {"available": False, "source": "Hokma has no reading of the result of the day for this broker: no data from the broker"})}


def require_available(u, account_ids) -> None:
    """A loss rule in restriction needs the P&L of the broker (SPEC-026 v2, A12). A broker with no reader of it (NinjaTrader today) cannot freeze:
    the rule is refused with a clear sentence and nothing is saved."""
    from hokma.broker import pnl

    pnl.require_reader(u, account_ids)


def require_exclusive(u, control_id: str, account_ids) -> None:
    """A loss or drawdown rule of a DESK needs the account to be that desk's alone: the broker gives the result of the ACCOUNT and does not split it by desk
    (SPEC-026 v2, 4.2, decision 1299). It is refused when the rule is created, with the sentence that says what to do: put the rule on the account. A rule of the
    ACCOUNT never comes here. Rules that already exist in a shared account stay as they are (unmeasured, they block) and the monitor tells the owner to move them."""
    from hokma.broker.service import BrokerError

    if control_id not in CONTROLS:
        return
    for account_id in account_ids:
        if len(u.broker.desks_of_account(account_id)) > 1:
            raise BrokerError("shared_account", 409, text=t("live.shared.rejected"))


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


# ---------------------------------------------------------------------------------------------------------------------- the measure

@dataclass(frozen=True)
class Measure:
    account_id: int
    session_key: str
    session_started_at: str
    session_ends_at: str
    equity_start: Decimal | None
    equity_live: Decimal | None          # the equity now, as the broker reported it (the name is kept: it is "live" in the sense of the last reading)
    loss: Decimal | None                 # of the account today; positive is a loss; None is unknown
    drawdown: Decimal | None             # highest equity of the readings of the session minus the equity now; None is unknown
    reason: str | None                   # why `loss` is unknown: NO_DATA | START_UNKNOWN
    read_at: str | None = None           # when Hokma received the reading this measure is made of (UTC, "YYYY-MM-DD HH:MM:SS")
    source: str | None = None            # schwab | sim | ninjatrader

    @property
    def measured(self) -> bool:
        return self.loss is not None

    @property
    def day_pnl(self) -> Decimal | None:
        return None if self.loss is None else -self.loss


def peak_of(m: Measure) -> Decimal | None:
    """The highest equity the broker reported in the session (the equity now plus the drawdown); None when the drawdown is unknown."""
    return None if m.drawdown is None or m.equity_live is None else m.equity_live + m.drawdown


_peaks: dict[tuple[int, str], Decimal] = {}          # (account, session) -> the highest equity of the readings, a copy of account_live_session.peak_equity
_peaks_lock = threading.Lock()


def _peak(database: Database, account_id: int, session_key: str, equity_now: Decimal, equity_start: Decimal | None, stored: Any = None) -> Decimal:
    """The highest equity among what the broker reported in this session (its start and every reading seen). Hokma values nothing: it only keeps the maximum.
    The maximum is kept in the database (`account_live_session.peak_equity`, read from `stored`) and written only when it goes up, so a restart of Hokma in the
    middle of the session does not start the drawdown again from the next reading."""
    key = (account_id, session_key)
    with _peaks_lock:
        known = _peaks.get(key)
        saved = None if stored in (None, "") else _d(stored)
        best = max(e for e in (equity_now, equity_start, known, saved) if e is not None)
        _peaks[key] = best
        for other in [k for k in _peaks if k[0] == account_id and k[1] != session_key]:
            del _peaks[other]                        # a session that ended is forgotten
    if saved is None or best > saved:
        try:
            with database.write() as u:
                u.session.execute("UPDATE account_live_session SET peak_equity = :p WHERE account_id = :a AND session_key = :k",
                                  {"p": str(best), "a": account_id, "k": session_key})
        except Exception:                                                  # noqa: BLE001  (the memory still has it; the next higher reading writes it again)
            log.warning("the peak equity of the session could not be saved")
    return best


def _row(u, account_id: int) -> dict[str, Any] | None:
    return u.session.one("SELECT * FROM account_live_session WHERE account_id = :a", {"a": account_id})


def calendar_of(u, account_id: int) -> str:
    row = _row(u, account_id)
    if row is not None and row["calendar"]:
        return row["calendar"]
    from hokma.contracts.identity import NoContract, parse_canonical

    kinds = []
    for r in u.session.query("SELECT DISTINCT contract_id FROM book_entries WHERE account_id = :a AND contract_id IS NOT NULL", {"a": account_id}):
        try:
            kinds.append(parse_canonical(r["contract_id"]).asset_type)
        except (NoContract, ValueError, KeyError):
            continue
    return sessions.calendar_for_assets(kinds)


def _ensure_session(database: Database, account_id: int, now: datetime) -> dict[str, Any]:
    """The session row of the account, rolled to the session of `now` when the stored one ended. A new session lifts the close of the day."""
    stamp = now.astimezone(timezone.utc).strftime(sessions.FORMAT)
    with database.read() as u:
        row = _row(u, account_id)
        if row is not None and stamp < str(row["session_ends_at"]):
            return row
        cal = calendar_of(u, account_id)
    sess = sessions.session_at(cal, now)
    with database.write() as u:
        values = {"k": sess.key, "s": sess.started, "e": sess.ends, "t": stamp, "a": account_id}
        if _row(u, account_id) is None:
            u.session.execute("INSERT INTO account_live_session (account_id, session_key, session_started_at, session_ends_at, updated_at) "
                              "VALUES (:a, :k, :s, :e, :t)", values)
            from hokma.broker import day_close

            closed = day_close.state_of(u, account_id)            # no session row yet, but a close of an earlier session is still there: this session starts open
            if closed["state"] == "day_closed" and closed["closed_at"] < sess.started:
                day_close.restart(u, account_id, "the session of the account restarted")
        else:
            u.session.execute("UPDATE account_live_session SET session_key = :k, session_started_at = :s, session_ends_at = :e, "
                              "equity_start = NULL, peak_equity = NULL, updated_at = :t WHERE account_id = :a", values)
            from hokma.broker import day_close

            day_close.restart(u, account_id, "the session of the account restarted")
        return _row(u, account_id)


def _stamp(as_of: str) -> str | None:
    """The hour of a reading (an ISO stamp of the reader) in the format of the sessions, UTC. None when it cannot be read."""
    try:
        return datetime.fromisoformat(str(as_of).replace("Z", "+00:00")).astimezone(timezone.utc).strftime(sessions.FORMAT)
    except ValueError:
        return None


def measure(account_id: int, *, db: Database | None = None, now: datetime | None = None) -> Measure:
    """The loss of the day and the drawdown of one account, from the last reading of the broker. Unknown (never zero) when the broker gave no reading or no start."""
    database = db or get_database()
    now = now or datetime.now(timezone.utc)
    row = _ensure_session(database, account_id, now)
    key = str(row["session_key"])
    try:
        reading = broker_pnl.latest_pnl(account_id)
    except Exception:                                                  # noqa: BLE001  (a reader that breaks is "no data from the broker": the rules fail closed)
        log.exception("the reading of the broker could not be taken")
        reading = None
    equity_now = None if reading is None or reading.equity_now is None else _d(reading.equity_now)
    start = None if reading is None or reading.equity_start is None else _d(reading.equity_start)
    if start is None and reading is not None:
        start = declared_start(database, account_id, key)              # the broker does not give the start: the owner's declaration stands
    read_at = None if reading is None else _stamp(reading.as_of)
    source = None if reading is None else reading.source
    base = dict(account_id=account_id, session_key=key, session_started_at=str(row["session_started_at"]), session_ends_at=str(row["session_ends_at"]),
                read_at=read_at, source=source)
    if reading is None or equity_now is None:
        return Measure(**base, equity_start=start, equity_live=None, loss=None, drawdown=None, reason=NO_DATA)
    peak = _peak(database, account_id, key, equity_now, start, row.get("peak_equity"))
    drawdown = None if peak is None else max(ZERO, peak - equity_now)
    if start is None:
        return Measure(**base, equity_start=None, equity_live=equity_now, loss=None, drawdown=drawdown, reason=START_UNKNOWN)
    return Measure(**base, equity_start=start, equity_live=equity_now, loss=start - equity_now, drawdown=drawdown, reason=None)


def declared_start(database: Database, account_id: int, session_key: str) -> Decimal | None:
    """The equity the owner declared for the session the account is in (the latest declaration), or None."""
    with database.read() as u:
        row = u.session.one("SELECT equity FROM account_start_declarations WHERE account_id = :a AND session_key = :k ORDER BY id DESC LIMIT 1",
                            {"a": account_id, "k": session_key})
    return None if row is None else _d(row["equity"])


def start_state(account_id: int, *, db: Database | None = None, now: datetime | None = None) -> dict[str, Any]:
    """What the owner sees about the start of the session of an account: whether the loss of the day is measured, why not, and what was declared."""
    database = db or get_database()
    m = measure(account_id, db=database, now=now)
    with database.read() as u:
        rows = u.session.query("SELECT equity, declared_by, declared_at FROM account_start_declarations WHERE account_id = :a AND session_key = :k ORDER BY id DESC",
                               {"a": account_id, "k": m.session_key})
    return {"account_id": account_id, "session_key": m.session_key, "measured": m.measured, "reason": m.reason,
            "needs_declaration": (not m.measured) and m.reason == "start_unknown",
            "equity_start": None if m.equity_start is None else str(m.equity_start),
            "declarations": [{"equity": r["equity"], "by": r["declared_by"], "at": str(r["declared_at"])} for r in rows]}


def declare_start(account_id: int, equity: Any, who: Any, *, confirm: bool = False, db: Database | None = None,
                  now: datetime | None = None) -> dict[str, Any]:
    """The owner says the equity the account started its session with, when a position carried over left that start unmeasured. Only the owner; only when
    the start really is unknown (a start that was measured is never overwritten); recorded with who, when and what. Without `confirm` it asks first."""
    from hokma.broker import service
    from hokma.broker.service import BrokerError

    if who is None or not getattr(who, "owner", False):
        raise service.owner_only("declaring the equity an account started its session with")
    try:
        value = Decimal(str(equity).strip())
    except Exception:                                                      # noqa: BLE001
        raise BrokerError("bad_equity", 400) from None
    if not value.is_finite() or value <= 0:
        raise BrokerError("bad_equity", 400)
    database = db or get_database()
    with database.read() as u:
        if u.session.one("SELECT id FROM broker_accounts WHERE id = :a", {"a": account_id}) is None:
            raise BrokerError("account_not_found", 404)
    state = start_state(account_id, db=database, now=now)
    if not state["needs_declaration"]:
        raise BrokerError("start_known", 409)
    if confirm is not True:
        raise BrokerError("equity_confirm", 409)
    with database.write() as u:
        u.session.execute("INSERT INTO account_start_declarations (account_id, session_key, equity, declared_by, declared_at) VALUES (:a, :k, :e, :w, :t)",
                          {"a": account_id, "k": state["session_key"], "e": str(value), "w": getattr(who, "name", None) or "owner", "t": utcnow_str()})
    after = start_state(account_id, db=database, now=now)
    monitor_ = monitor()
    if monitor_ is not None:
        monitor_.after_fill(account_id)                                    # measured now: the freeze looks at it at once
    return after


def utcnow_str() -> str:
    from hokma.db.timeutil import utcnow

    return utcnow()


# ------------------------------------------------------------------------------------------------------------------------ the rules

@dataclass(frozen=True)
class Rule:
    rule_id: str
    control: str
    metric: str                           # daily_loss | drawdown
    scope: str                            # account | desk_account | desk
    account_id: int | None
    desk_id: int | None
    fixed: Decimal | None = None
    steps: tuple = ()
    pct: Decimal | None = None
    on_trip: str = "block_entries"
    mode: str = "restriction"             # restriction | notice (a notice never closes or blocks: it tells the owner when the broker's number reaches the cap)
    timeout_min: int | None = None        # of a notice: how long the owner has to answer it (the wait of the rule)


def _values(limits: Mapping[str, Any], control: str) -> tuple[Decimal | None, tuple, Decimal | None]:
    funding = limits.get("funding") or {}
    if control == "daily_loss":
        fixed, steps, pct = funding.get("daily_loss_usd"), limits.get("daily_loss_steps"), None
    elif control == "drawdown":
        fixed, steps, pct = funding.get("trailing_drawdown_usd"), limits.get("drawdown_steps"), None
    else:
        fixed, steps = None, None
        pct = limits.get("max_daily_loss_pct" if control == "daily_loss_pct" else "max_drawdown_pct")
    return (None if fixed is None else _d(fixed)), tuple(steps or ()), (None if pct is None else _d(pct))


def rules_of_account(u, account_id: int, with_modes: tuple[str, ...] = ("restriction",)) -> list[Rule]:
    """Every rule that a loss or a drawdown of this account can trip: the rules of the account, of one desk in it, and the rules in the limits of each desk that
    uses it. By default only the ones in restriction (what freezes: the door and the close). A rule that is a notice never freezes (it asks), so it is here only when
    `with_modes` names it: the monitor asks for both to tell the owner when a notice reaches its cap."""
    from hokma.broker import account_rules, desk_rules, rules as broker_rules

    out: list[Rule] = []
    for row in u.session.query("SELECT * FROM account_rules WHERE account_id = :a ORDER BY id", {"a": account_id}):
        if row["mode"] not in with_modes:
            continue
        if row["control_id"] not in CONTROLS:
            continue
        control = desk_rules.BY_ID[row["control_id"]]
        cfg = broker_rules.validate_limits(desk_rules._stored_changes({}, control, json.loads(row["value"])), {})
        fixed, steps, pct = _values(cfg, control.id)
        out.append(Rule(account_rules.rule_id_of(account_id, control.id, row["desk_id"]), control.id, METRIC[control.id],
                        "desk_account" if row["desk_id"] else "account", account_id, row["desk_id"], fixed, steps, pct,
                        row["on_trip"] or "block_entries", row["mode"], row["timeout_min"]))
    for desk_id in u.broker.desks_of_account(account_id):
        limits = u.broker.get_limits(desk_id)
        modes = limits.get("rule_modes") or {}
        for control in CONTROLS:
            fixed, steps, pct = _values(limits, control)
            if fixed is None and not steps and pct is None:
                continue
            entry = modes.get(control) if isinstance(modes.get(control), Mapping) else {}
            mode = entry.get("mode", "restriction")
            if mode not in with_modes:
                continue
            out.append(Rule(f"d{desk_id}:{control}", control, METRIC[control], "desk", None, desk_id, fixed, steps, pct,
                            entry.get("on_trip") or "block_entries", mode, entry.get("timeout_min")))
    return out


def cap_of(rule: Rule, base: Decimal | None, live_equity: Decimal | None, peak: Decimal | None) -> Decimal | None:
    """The cap in USD right now: it moves with the equity (SPEC-026, 3.2). None when it cannot be worked out (the rule is then unmeasured)."""
    caps: list[Decimal] = []
    if rule.metric == "daily_loss":
        if rule.fixed is not None or rule.steps:
            caps.append(thresholds.effective_usd(rule.fixed, rule.steps, base))
        if rule.pct is not None:
            if base is None or base <= 0:
                return None
            caps.append(base * rule.pct / 100)
    else:
        if rule.fixed is not None or rule.steps:
            caps.append(thresholds.effective_usd(rule.fixed, rule.steps, live_equity))
        if rule.pct is not None:
            if peak is None or peak <= 0:
                return None
            caps.append(peak * rule.pct / 100)
    return min(caps) if caps else None


@dataclass(frozen=True)
class Eval:
    rule: Rule
    status: str                           # ok | reached | unmeasured
    value: Decimal | None                 # the loss or the drawdown the broker data gives
    cap: Decimal | None
    why: str = ""                         # why it is unmeasured: NO_DATA | START_UNKNOWN | shared_account | the base of the threshold is unknown
    at: str | None = None                 # when the broker reading was received


def _eval(rule: Rule, value: Decimal | None, cap: Decimal | None, why: str, at: str | None) -> Eval:
    if value is None or cap is None:
        return Eval(rule, "unmeasured", value, cap, why or "the base of the threshold is unknown", at)
    if value >= cap:
        return Eval(rule, "reached", value, cap, "", at)
    return Eval(rule, "ok", value, cap, "", at)


SHARED = "shared_account"                 # a rule of a desk in an account that other desks use: the broker does not split the result by desk


def evaluate(rule: Rule, m: Measure, others: Mapping[int, Measure], accounts_of_desk: list[int], shared: frozenset[int] = frozenset()) -> Eval:
    """One rule against the measures. `m` is the account being looked at; `others` are the measures of the other accounts of a desk rule; `shared` are the accounts that
    more than one desk uses. A rule of an ACCOUNT is the account's number; a rule of a DESK is measured only where the account is that desk's alone (spec 4.2)."""
    drawdown = rule.metric == "drawdown"
    if rule.scope == "account":
        value, why = (m.drawdown if drawdown else m.loss), (m.reason or "")
        return _eval(rule, value, cap_of(rule, m.equity_start, m.equity_live, peak_of(m)), why, m.read_at)
    accounts = [m.account_id] if rule.scope == "desk_account" else accounts_of_desk
    total: Decimal | None = ZERO
    bases: Decimal | None = ZERO
    live: Decimal | None = ZERO
    why = ""
    at = m.read_at
    for account in accounts:
        mm = m if account == m.account_id else others.get(account)
        piece = None
        if account in shared:
            why = why or SHARED
        elif mm is not None:
            piece = mm.drawdown if drawdown else mm.loss
            why = why or (mm.reason or "")
        total = None if total is None or piece is None else total + piece
        bases = None if bases is None or mm is None or mm.equity_start is None else bases + mm.equity_start
        live = None if live is None or mm is None or mm.equity_live is None else live + mm.equity_live
    return _eval(rule, total, cap_of(rule, bases, live, peak_of(m) if len(accounts) == 1 else None), why, at)


def evaluations(database: Database, account_id: int, desk_id: int | None = None, *, now: datetime | None = None,
                modes: tuple[str, ...] = ("restriction",)) -> tuple[Measure, list[Eval]]:
    """The measure of the account and the evaluation of its rules. With `desk_id` only the rules that reach that desk (an order of it). `modes` says which modes
    are looked at: the door asks for restrictions only; the monitor asks for notices too."""
    with database.read() as u:
        rules = rules_of_account(u, account_id, modes)
        desks = {r.desk_id: u.broker.accounts_of_desk(r.desk_id) for r in rules if r.scope == "desk"}
        shared = frozenset(a for a in {account_id, *(x.id for accounts in desks.values() for x in accounts)} if len(u.broker.desks_of_account(a)) > 1)
    if desk_id is not None:
        rules = [r for r in rules if r.scope == "account" or r.desk_id == desk_id]
    m = measure(account_id, db=database, now=now)
    others: dict[int, Measure] = {}
    for rule in rules:
        if rule.scope == "desk":
            for account in desks[rule.desk_id]:
                if account.id != account_id and account.id not in others:
                    others[account.id] = measure(account.id, db=database, now=now)
    return m, [evaluate(r, m, others, [a.id for a in desks[r.desk_id]] if r.scope == "desk" else [], shared) for r in rules]


def active_stop(u, desk_id: int, account_id: int, now: datetime | None = None) -> dict[str, Any] | None:
    """The stop of a desk by a rule of its own that is in force: for this account or for all of them."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(sessions.FORMAT)
    return u.session.one("SELECT rule_id, loss, on_trip, closed_at FROM desk_day_stops WHERE desk_id = :d AND account_id IN (0, :a) AND lifted_at IS NULL "
                         "AND expires_at > :n ORDER BY id DESC LIMIT 1", {"d": desk_id, "a": account_id, "n": stamp})


# ------------------------------------------------------------------------------------------------------------------------ the freeze

def _notify(database: Database, text: str) -> None:
    from hokma.exec import telegram_approval

    try:
        with database.write() as u:
            telegram_approval.notice(u, text)
    except Exception:                                                      # noqa: BLE001  (the stop is already recorded; the notice is queued or lost, never blocks)
        log.warning("the notice of the live freeze could not be queued")


def notify_reached(database: Database, text: str) -> bool | None:
    """Like `_notify`, but says what happened: True when the text was queued, None when there is no Telegram link (nothing to queue to: this channel does not count), False when the
    queue failed. The notice of a cap counts its channels with this; `_notify` keeps swallowing the failure for everybody else."""
    from hokma.exec import telegram_approval

    try:
        with database.write() as u:
            return telegram_approval.notice_queued(u, text)
    except Exception:                                                      # noqa: BLE001
        log.warning("the notice of a cap could not be queued")
        return False


def trip(database: Database, m: Measure, ev: Eval) -> bool:
    """Close what the rule closes, in the event that reached the cap. Idempotent. Returns True when it was this call that closed."""
    from hokma.broker import day_close

    rule = ev.rule
    if rule.scope in ("account",):
        with database.read() as u:
            was = day_close.is_closed(u, m.account_id)
        if was:
            return False
        day_close.close_day(m.account_id, rule.rule_id, ev.value, rule.on_trip, database)
        scope_text = t("live.freeze.scope.account", account=m.account_id)
        desk_id = None
        accounts = [m.account_id]
    else:
        account = 0 if rule.scope == "desk" else m.account_id
        with database.write() as u:
            if active_stop(u, rule.desk_id, account) is not None:
                return False
            u.session.execute("INSERT INTO desk_day_stops (desk_id, account_id, rule_id, loss, on_trip, closed_at, expires_at) VALUES (:d, :a, :r, :l, :o, :c, :e)",
                              {"d": rule.desk_id, "a": account, "r": rule.rule_id, "l": str(ev.value), "o": rule.on_trip, "c": utcnow_str(), "e": m.session_ends_at})
        scope_text = (t("live.freeze.scope.desk_all", desk=rule.desk_id) if rule.scope == "desk"
                      else t("live.freeze.scope.desk_account", desk=rule.desk_id, account=m.account_id))
        desk_id = rule.desk_id
        if rule.scope == "desk":
            with database.read() as u:
                accounts = [a.id for a in u.broker.accounts_of_desk(rule.desk_id)] or [m.account_id]      # a desk rule stops the desk in EVERY account it uses
        else:
            accounts = [m.account_id]
    done = _on_trip(database, accounts, desk_id, rule.on_trip)
    what = t("live.freeze.what.drawdown") if rule.metric == "drawdown" else t("live.freeze.what.loss")
    _notify(database, t("live.freeze.notice", scope=scope_text, what=what, value=f"{ev.value:,.2f}", cap=f"{ev.cap:,.2f}", at=ev.at or "?",
                        rule=rule.rule_id, done=done))
    return True


def _on_trip(database: Database, accounts: list[int], desk_id: int | None, on_trip: str) -> str:
    """What happens to what is open, in every account the rule closes (all the accounts of a desk when the rule is the desk's). Never raises: a failure is
    told to the owner in the notice, exactly, and what is left open is retried by the monitor."""
    if on_trip == "block_entries":
        return t("live.freeze.done.block")
    parts = []
    for account_id in accounts:
        try:
            text, remaining = close_positions(database, account_id, desk_id, include_long_shares=on_trip == "block_and_flatten_all")
        except Exception as exc:                                           # noqa: BLE001
            log.exception("the closing of the live freeze failed")
            text, remaining = t("live.freeze.done.failed", error=type(exc).__name__), ["?"]
        if remaining and _monitor is not None:
            _monitor.retry_close(account_id, desk_id, on_trip == "block_and_flatten_all")
        parts.append(text if len(accounts) == 1 else f"[{account_id}] {text}")
    return " ".join(parts)


def _is_long_holding(contract_id: str, qty: Decimal) -> bool:
    from hokma.contracts.identity import parse_canonical

    try:
        return qty > 0 and parse_canonical(contract_id).asset_type in ("equity", "etf")
    except Exception:                                                      # noqa: BLE001  (an id that cannot be read is closed like anything else)
        return False


def _open_of(database: Database, account_id: int, desk_id: int | None) -> dict[str, Decimal]:
    book = Book.load(account_id, db=database) if desk_id is None else Book(book_module.entries(account_id, db=database, desk_id=desk_id))
    return {cid: p.qty for cid, p in book.cost_positions().items() if p.qty != 0}


def closing(database: Database, account_id: int, desk_id: int | None, *, include_long_shares: bool) -> str:
    return close_positions(database, account_id, desk_id, include_long_shares=include_long_shares)[0]


def _broker_room(broker: Any, account_id: int) -> dict[str, Decimal] | None:
    """What the BROKER says is still open to close right now: its positions net of what its live orders already take out of them (a sell against
    a long, a buy against a short). None when the broker does not report positions. A live order that cannot be read whole leaves nothing to close."""
    from hokma.exec import prefilter, submit
    from hokma.exec.guards import _held

    if not broker.capabilities.positions:
        return None
    positions = prefilter._normalize_positions(broker.positions(account_id))
    return dict(_held(positions, submit._live_orders(broker, account_id)))


_reported: dict[int, dict[str, Decimal]] = {}          # account -> what the broker reported open at the end of the last close pass (to tell the owner exactly)


def _broker_open(broker: Any, account_id: int) -> dict[str, Decimal] | None:
    """What the broker reports as open in the account right now ({contract: signed qty}), or None when it does not report positions or cannot be read."""
    from hokma.exec import prefilter

    if not broker.capabilities.positions:
        return None
    try:
        out: dict[str, Decimal] = {}
        for p in prefilter._normalize_positions(broker.positions(account_id)):
            cid, qty = getattr(p, "contract_id", None), _d(getattr(p, "qty", 0))
            if cid and qty != 0:
                out[cid] = out.get(cid, ZERO) + qty
        return out
    except Exception:                                                      # noqa: BLE001  (the book decides, and the next pass reads the broker again)
        log.warning("the positions of the broker could not be read after the close")
        return None


def _still_open(database: Database, account_id: int, desk_id: int | None, wanted: Mapping[str, Decimal], at_broker: dict[str, Decimal] | None) -> list[str]:
    """What a close pass still has open. A position is closed only when the broker's own report no longer carries it (SPEC-026 v2, 4.2): an order sent, or a fill the
    book saw, is not enough. The close of ONE desk in a shared account cannot ask the broker (the broker's number is the whole account's): there the book of that desk,
    fed by the fills the broker gave, says."""
    if desk_id is None and at_broker is not None:
        _reported[account_id] = at_broker
        return sorted(cid for cid in wanted if at_broker.get(cid, ZERO) != 0)
    return sorted(set(_open_of(database, account_id, desk_id)) & set(wanted))


def _reported_text(account_id: int, contracts: list[str]) -> str:
    """The positions that did not close, with the quantity the broker reports (or just the contract when the broker was not asked)."""
    held = _reported.get(account_id) or {}
    return ", ".join(f"{cid} {format(held[cid].normalize(), 'f')}" if cid in held else cid for cid in contracts)


def _pull_quietly(database: Database, account_id: int) -> None:
    try:
        pull_fills(database, account_id)
    except Exception:                                                      # noqa: BLE001
        log.warning("the fills of the close could not be pulled")


def close_positions(database: Database, account_id: int, desk_id: int | None, *, include_long_shares: bool) -> tuple[str, list[str]]:
    """Cancel the live orders that open and close the positions of the account (or of one desk in it) and close each position with a reduce-only market
    order straight to the broker (a close is never stopped by a guard). Shares and ETFs held long are sold only with `include_long_shares`.
    ONE contract that fails never stops the others. Returns (the exact text: what closed, what did NOT, what was kept) and the contracts still open
    after the pass, which the monitor retries.

    A close never repeats one that already happened. The fills are pulled BEFORE deciding what is open. Once the live orders are canceled and the broker
    confirms that none is live, each close is cut to what the broker holds at that moment: never more than is open, never on the other side. `reduce_only`
    is not honored by every broker (Schwab ignores it), so the cut is made here and not left to the flag. A contract with a live closing order of its
    own is not sent again (the cancel above leaves none; the cut to the net room is the second belt)."""
    from hokma.broker import service
    from hokma.exec import submit
    from hokma.exec.money import clip_reduce_only
    from hokma.exec.proposal import Leg, OrderProposal

    with database.read() as u:
        desks = u.broker.desks_of_account(account_id)
    anchor = desk_id if desk_id is not None else (desks[0] if desks else None)
    if anchor is None:
        return t("live.close.no_desk"), []
    _pull_quietly(database, account_id)                                   # what is open is decided on the fills the broker already gave, not on yesterday's book
    mine = _open_of(database, account_id, desk_id)
    wanted = {cid: q for cid, q in mine.items() if include_long_shares or not _is_long_holding(cid, q)}
    kept = sorted(set(mine) - set(wanted))
    try:
        submit.cancel_live_orders(database, anchor, reason="live freeze", who=None, account_id=account_id)
    except service.BrokerError as exc:
        return t("live.close.cancel_failed", code=exc.code), sorted(wanted)
    closed: list[str] = []
    failed: list[str] = []
    line = submit._line_of(database, anchor)
    with submit.open_broker(database, anchor, account_id) as live_broker:
        broker, account = live_broker
        todo = dict(wanted)
        # The account lock is held from the read of the broker (positions and live orders) to the last send: another route over the same account
        # (flatten, another desk, the killswitch, the retries) cannot take the position to 0 between the read and the send, and a broker that ignores
        # reduce_only would invert it. `dispatch` takes the same re-entrant lock. Order of locks: desk, then account; none of the desk is taken here.
        # The limit that remains: `positions()` of a broker may lag its own fills; the cut is as fresh as the broker reports.
        with submit.account_lock(account.id):
            try:
                room = _broker_room(broker, account.id)                       # read AFTER the cancels: a fill while they went out changes the quantity
            except Exception:                                                  # noqa: BLE001  (what the broker holds cannot be read: nothing is sent blind)
                log.exception("the positions of the broker could not be read before the close")
                room, failed, todo = {}, sorted(wanted), {}
            for cid, qty in sorted(todo.items()):
                try:
                    side = "sell" if qty > 0 else "buy"
                    send = abs(qty)
                    if room is not None:
                        send, _cut = clip_reduce_only(side, send, room.get(cid, ZERO))
                        if send <= 0:                                          # closed already, or a closing order is still alive: nothing to send now
                            continue
                    leg = Leg(cid, side, "close", send)
                    proposal = OrderProposal(anchor, line, "rule", (leg,), "market", reduce_only=True, thesis="live freeze", created_by="live-freeze")
                    native = submit.to_native(proposal, submit.resolve_contracts(proposal), broker.kind)
                    answer, status, outcome = submit.dispatch(broker, account.id, "place", native, owner=(anchor, None))
                    with database.write() as w:
                        w.broker.record_order(broker_kind=broker.kind, action="place", desk_id=anchor, account_id=account.id, request=proposal.to_json(),
                                              rule_result="auto", reason="live freeze: the loss reached the line of the rule", http_status=status)
                    refused = isinstance(answer, Mapping) and str(answer.get("status", "")).upper() in ("REJECTED", "REFUSED", "CANCELED", "CANCELLED")
                    (failed if status >= 400 or refused else closed).append(cid)
                except Exception:                                              # noqa: BLE001  (the next contract is still closed)
                    log.exception("the close of %s failed", cid)
                    failed.append(cid)
    if closed:                                                             # a broker without a stream: the fill that closes it comes by the pull (a failure elsewhere does not skip it)
        _pull_quietly(database, account_id)
    at_broker = None
    try:
        with submit.open_broker(database, anchor, account_id) as again:        # what the BROKER reports now, a read only (nothing is sent here): it, and not the book, says what is still open
            at_broker = _broker_open(again[0], again[1].id)
    except Exception:                                                      # noqa: BLE001  (the book decides this pass; the next pass asks the broker again)
        log.warning("the broker could not be asked what is still open after the close")
    remaining = _still_open(database, account_id, desk_id, wanted, at_broker)
    text = t("live.close.closed", list=", ".join(closed) or t("live.close.nothing"))
    if failed:
        text += t("live.close.failed", list=", ".join(sorted(failed)))
    if kept:
        text += t("live.close.kept", list=", ".join(kept))
    return text, remaining


# ------------------------------------------------------------------------------------------------------------------------ the monitor

class LiveMonitor:
    """One monitor for all the accounts with a rule of loss or drawdown. It acts on the LAST reading of the broker (`hokma.broker.pnl.latest_pnl`): `evaluate`
    runs when a new reading arrives (`on_reading`), after a fill and on every `tick`, and it never values a position with prices of its own."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db
        self._accounts: set[int] = set()                  # the accounts that have a rule in restriction (the ones that are evaluated)
        self._lock = threading.Lock()
        self._busy: set[int] = set()
        self._dirty: set[int] = set()
        self._start_told: set[tuple[int, str]] = set()
        self._notice_told: dict[tuple[int, str, str], Decimal] = {}   # (account, rule, session) -> the cap of a notice that reached it and was raised; raised again after it fell below, or when the cap is a higher one
        self._shared_told: set[tuple[str, str]] = set()               # (rule, session) of a desk rule in a shared account that the owner was told to move
        self._retries: dict[tuple[int, int | None], list] = {}        # (account, desk) -> [include_long, attempts]: closes with something still open
        self._pulling: set[int] = set()
        self._pull_dirty: set[int] = set()
        self.history: list[dict[str, Any]] = []          # what each close measured: {account, rule, value, cap} (the overshoot of the tests: value minus cap)

    @property
    def database(self) -> Database:
        return self._db or get_database()

    def attach(self) -> "LiveMonitor":
        self.refresh()
        try:
            self.resume_closes()
        except Exception:                                                  # noqa: BLE001  (the monitor starts anyway; the log says what was not resumed)
            log.exception("the closes of the day could not be resumed")
        return self

    def resume_closes(self) -> list[tuple[int, int | None]]:
        """After a restart the retries of a close (kept in memory) are gone. A day that is closed by a rule that flattens, with what it had to close still open,
        is closed again from the state saved in the database: the account state and the stops of the desks. Returns the (account, desk) that were resumed."""
        database = self.database
        stamp = datetime.now(timezone.utc).strftime(sessions.FORMAT)
        now = datetime.now(timezone.utc)
        with database.read() as u:
            accounts = []
            for r in u.session.query("SELECT account_id, on_trip, closed_at FROM account_day_state WHERE state = 'day_closed'"):
                started = sessions.session_at(calendar_of(u, r["account_id"]), now).started
                if str(r["closed_at"]) < started:          # closed in a session that already ended: the tick reopens the account by its rule; a close of yesterday never flattens today
                    continue
                accounts.append(r)
            stops = u.session.query("SELECT desk_id, account_id, on_trip FROM desk_day_stops WHERE lifted_at IS NULL AND expires_at > :n", {"n": stamp})
            desk_accounts = {r["desk_id"]: [a.id for a in u.broker.accounts_of_desk(r["desk_id"])] for r in stops}
        todo: list[tuple[int, int | None, bool]] = []
        for r in accounts:
            if r["on_trip"] in FLATTEN_TRIPS:
                todo.append((r["account_id"], None, r["on_trip"] == "block_and_flatten_all"))
        for r in stops:
            if r["on_trip"] in FLATTEN_TRIPS:
                for a in ([r["account_id"]] if r["account_id"] else desk_accounts.get(r["desk_id"], [])):
                    todo.append((a, r["desk_id"], r["on_trip"] == "block_and_flatten_all"))
        resumed = []
        for account_id, desk_id, include_long in todo:
            held = _open_of(database, account_id, desk_id)
            if any(include_long or not _is_long_holding(cid, q) for cid, q in held.items()):
                self.retry_close(account_id, desk_id, include_long)
                resumed.append((account_id, desk_id))
        return resumed

    def detach(self) -> None:
        with self._lock:
            self._accounts = set()

    def refresh(self, account_id: int | None = None) -> None:
        """Which accounts have a rule in restriction (the ones the monitor evaluates). A closed account is evaluated too: a reopened one trips again."""
        database = self.database
        with database.read() as u:
            ids = [account_id] if account_id is not None else [r["id"] for r in u.session.query("SELECT id FROM broker_accounts")]
            watched = {a for a in ids if rules_of_account(u, a, WATCHED_MODES)}
        with self._lock:
            self._accounts = ({a for a in self._accounts if a != account_id} if account_id is not None else set()) | watched

    def retry_close(self, account_id: int, desk_id: int | None, include_long: bool) -> None:
        self._retries.setdefault((account_id, desk_id), [include_long, 1])

    def _retry_closes(self) -> None:
        """What a close left open is closed again on each tick, up to CLOSE_RETRIES passes; then the owner is told it must be done by hand."""
        for key, state in list(self._retries.items()):
            account_id, desk_id = key
            try:
                text, remaining = close_positions(self.database, account_id, desk_id, include_long_shares=state[0])
            except Exception:                                              # noqa: BLE001
                log.exception("the retry of the close failed")
                text, remaining = "", ["?"]
            state[1] += 1
            if not remaining:
                self._retries.pop(key)
                _notify(self.database, t("live.close.retry_done", account=account_id, text=text))
            elif state[1] > CLOSE_RETRIES:
                self._retries.pop(key)
                _notify(self.database, t("live.close.retry_exhausted", account=account_id, list=_reported_text(account_id, remaining), n=CLOSE_RETRIES))

    def _tell_start(self, m: Measure) -> None:
        """An account with a rule whose start of session cannot be measured (a carried position): the owner is told once per session how to unblock it."""
        key = (m.account_id, m.session_key)
        if m.measured or m.reason != "start_unknown" or key in self._start_told:
            return
        self._start_told.add(key)
        _notify(self.database, t("live.start.unknown", account=m.account_id))

    def _tell_notice(self, m: Measure, ev: Eval) -> None:
        """A rule in NOTICE reached its cap in the reading of the broker: the owner gets ONE notice with no order in the way, with three buttons (keep trading today, block
        entries today, close positions) in the chat and in Telegram (`hokma.exec.cap_notices`). It closes nothing and blocks nothing by itself. It is raised again if the number
        fell below the cap and reached it again, or if the cap is a higher one, or if it could not be raised or reached the owner (the key is kept only when it was raised or deliberately suppressed); after the owner answered it is not raised again in that session."""
        from hokma.exec import cap_notices

        key = (m.account_id, ev.rule.rule_id, m.session_key)
        if ev.status != "reached":
            if ev.status == "ok":
                self._notice_told.pop(key, None)
            return
        told = self._notice_told.get(key)
        if told is not None and ev.cap is not None and ev.cap <= told:
            return
        if cap_notices.raise_notice(self.database, m, ev) != cap_notices.FAILED:         # a failure is not told: the next reading raises it again
            self._notice_told[key] = ev.cap

    def _tell_shared(self, m: Measure, ev: Eval) -> None:
        """A rule of a DESK in an account that more than one desk uses cannot be measured (the broker does not split the result by desk): it keeps blocking the
        openings, and the owner is told ONCE per session to move it to the account."""
        if ev.status != "unmeasured" or ev.why != SHARED or ev.rule.scope == "account":
            return
        key = (ev.rule.rule_id, m.session_key)
        if key in self._shared_told:
            return
        self._shared_told.add(key)
        _notify(self.database, t("live.shared.move", rule=ev.rule.rule_id, account=m.account_id))

    def wake_fills(self, kind: str, *, again_after: float = 2.0) -> None:
        """The account stream of a broker said that something happened in its accounts: the fills of every account of that kind with a rule are pulled NOW, and
        once more a moment later (the order may not show as filled the instant the message arrives). The message itself carries no fill that is read."""
        database = self.database
        with database.read() as u:
            kinds = {b.id: b.kind for b in u.broker.list_brokers()}
            mine = [r["id"] for r in u.session.query("SELECT id, broker_id FROM broker_accounts") if kinds.get(r["broker_id"]) == kind]
            mine = [a for a in mine if rules_of_account(u, a)]
        for account_id in mine:
            self._pull(account_id)
            if again_after:
                timer = threading.Timer(again_after, self._pull, args=(account_id,))
                timer.daemon = True
                timer.start()

    def _pull(self, account_id: int) -> None:
        with self._lock:
            if account_id in self._pulling:
                self._pull_dirty.add(account_id)
                return
            self._pulling.add(account_id)
        threading.Thread(target=self._pull_run, args=(account_id,), daemon=True, name="live-fills").start()

    def _pull_run(self, account_id: int) -> None:
        while True:
            try:
                pull_fills(self.database, account_id)
            except Exception:                                              # noqa: BLE001  (the sync is the backup; the next signal pulls again)
                log.exception("the fills of account %s could not be pulled", account_id)
            with self._lock:
                if account_id in self._pull_dirty:
                    self._pull_dirty.discard(account_id)
                    continue
                self._pulling.discard(account_id)
                return

    def after_fill(self, account_id: int) -> None:
        self.refresh(account_id)
        self.evaluate(account_id)

    def tick(self) -> None:
        self.refresh()
        self._retry_closes()
        try:
            from hokma.exec import cap_notices

            cap_notices.expire_due(self.database)                          # a notice of a cap nobody answered ends here at the latest (it also has its own timer)
        except Exception:                                                  # noqa: BLE001
            log.warning("the notices of a cap could not be swept")
        with self._lock:
            accounts = sorted(self._accounts)
        for account_id in accounts:
            self.evaluate(account_id)

    def evaluate(self, account_id: int, *, now: datetime | None = None) -> list[Eval]:
        """Read the last data of the broker for the account and close what reached the cap, in this same call. Calls that arrive while one runs are folded into a re-run."""
        with self._lock:
            if account_id in self._busy:
                self._dirty.add(account_id)
                return []
            self._busy.add(account_id)
        out: list[Eval] = []
        try:
            while True:
                try:
                    m, out = evaluations(self.database, account_id, now=now, modes=WATCHED_MODES)
                    self._tell_start(m)
                    for ev in out:
                        self._tell_shared(m, ev)
                        if ev.rule.mode == "notice":
                            self._tell_notice(m, ev)
                        elif ev.status == "reached" and trip(self.database, m, ev):
                            self.history.append({"account_id": account_id, "rule_id": ev.rule.rule_id, "value": ev.value, "cap": ev.cap})
                except Exception:                                      # noqa: BLE001  (a failed pass is seen in the log and the next event measures again)
                    log.exception("the live freeze failed on account %s", account_id)
                with self._lock:
                    if account_id not in self._dirty:
                        self._busy.discard(account_id)
                        return out
                    self._dirty.discard(account_id)
        finally:
            with self._lock:
                self._busy.discard(account_id)


_monitor: LiveMonitor | None = None


def monitor() -> LiveMonitor | None:
    return _monitor


def start(db: Database | None = None) -> LiveMonitor:
    """Create and attach the monitor of the process (the scheduler and the tests call it)."""
    global _monitor
    if _monitor is not None:
        _monitor.detach()
    _monitor = LiveMonitor(db).attach()
    return _monitor


def stop() -> None:
    global _monitor
    if _monitor is not None:
        _monitor.detach()
    _monitor = None


def pull_fills(database: Database, account_id: int) -> None:
    """Pull the fills of the account from its broker into the book (the same sync of the backoffice). The book is what a close reads to know what the account holds."""
    from hokma.backoffice import sync

    with database.read() as u:
        desks = u.broker.desks_of_account(account_id)
    if desks:
        sync.sync_desk(desks[0], account_id=account_id, db=database)


def on_account_activity(kind: str, message_type: str) -> None:
    """The account stream of a broker of this `kind` said something happened. An ERROR message is not a signal. A process with no monitor ignores it."""
    if _monitor is not None and message_type.upper() != "ERROR":
        _monitor.wake_fills(kind)


def after_fill(account_id: int) -> None:
    """A fill was booked: the simulated broker is read again at once and the monitor looks at the account. A process with no monitor ignores it."""
    from hokma.broker import pnl

    pnl.on_fill()                                                          # the simulated broker is read again at once (SPEC-026 4.1.2)
    if _monitor is not None:
        _monitor.after_fill(account_id)


def on_reading(account_id: int) -> None:
    """A new reading of the broker arrived for this account (the readers of 3390e1 call it): it is evaluated in this event. A process with no monitor ignores it."""
    if _monitor is not None:
        _monitor.evaluate(account_id)


async def keep_measuring(app: Any, interval: float = 5.0) -> None:
    """The task of the app: it starts the monitor and evaluates the accounts every few seconds on the last data of the broker. The account stream is followed by
    the reader of the P&L (hokma.broker.pnl), its only owner. The readings and the fills do the evaluating as they arrive; this only catches the open of a session and a reading that
    went stale."""
    import asyncio

    mon = start(app.state.db)
    while True:
        try:
            await asyncio.to_thread(mon.tick)
        except Exception:                                              # noqa: BLE001
            log.exception("a pass of the live freeze failed")
        await asyncio.sleep(interval)
