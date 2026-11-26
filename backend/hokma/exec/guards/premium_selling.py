"""The premium selling switch at the filter before an order (SPEC-019: ECT-042, spec 4.5).

Selling premium is an opening order that receives a net credit, or that carries a short option leg which no long leg of
the same order covers (same underlying, same type, the quantity, the same expiration or a later one, and a strike that
bounds the loss). Buying calls and puts and debit spreads are not premium selling. An order that only reduces what the
account holds is never premium selling.

The code decides, never the text of an agent (decision 1260). Every fact is derived from real state:

- the effect of each leg is read from the positions of the account (`normalize_effects`), never from the declared effect;
- the underlying, the type, the strike and the expiration come from the contract sheets, never from the declared `line`;
- the credit is the one the market marks of the legs say, never a declared net premium or a limit price;
- a proposal that is the leg of a bigger structure is judged alone: a long leg held in another order covers nothing, so a
  structure split in several proposals, or hidden in the children of a nested order, is refused leg by leg.

The switch is open for one desk and one underlying only while `premium_thresholds` holds a `calibrated` row with a
backtest reference for them. The table is empty in H3. `record_calibration` opens the switch and `retire_calibration` closes it again
(a recalibration that fails): they are the only writers, they have no API route and no MCP tool, and a scan test fails if a module
outside `hokma/backtest/` calls either. There is no manual opening.

Any error reading the table or the contracts closes the switch: the order is refused, never let through.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from hokma.contracts.identity import ContractSheet
from hokma.db import Database, get_database
from hokma.db.timeutil import utcnow
from hokma.exec.guards import GuardContext, GuardResult, blocked, normalize_effects, passed, signed
from hokma.exec.money import ZERO, dec_or_none
from hokma.exec.proposal import Leg, OrderProposal
from hokma.i18n import t

GUARD = "premium_selling"
CODE = "premium_selling_closed"
OPTION_TYPES = ("option", "future_option")
OPTION_PREFIXES = ("OPT", "FOP")
MAX_REF = 128
_METRIC = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_UNDERLYING = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,39}$")
_REF = re.compile(r"^[\x21-\x7e]{1,128}$")

SwitchValue = Literal["closed", "open"]


@dataclass(frozen=True)
class SwitchState:
    """The switch of one desk and one underlying. `open` carries the calibrated row; `closed` says why."""

    state: SwitchValue
    desk_id: int
    underlying: str
    threshold: Decimal | None = None
    metric: str | None = None
    backtest_run_ref: str | None = None
    row_id: int | None = None
    reason: str = "no_calibrated_threshold"

    @property
    def is_open(self) -> bool:
        return self.state == "open"


def _closed(desk_id: int, underlying: str, reason: str) -> SwitchState:
    return SwitchState("closed", desk_id, underlying, reason=reason)


def _clean_underlying(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not _UNDERLYING.match(text):
        raise ValueError("underlying is a plain symbol")
    return text


def switch_state(desk_id: int, underlying: str, *, db: Database | None = None) -> SwitchState:
    """`open` only when a calibrated row with a backtest reference exists for this desk and underlying. Any failure
    (a bad argument, an unreadable table, a threshold that is not a number) is `closed`: it never raises into a send."""
    try:
        if isinstance(desk_id, bool) or not isinstance(desk_id, int) or desk_id < 1:
            raise ValueError("desk_id")
        name = _clean_underlying(underlying)
    except ValueError:
        return _closed(desk_id if isinstance(desk_id, int) and not isinstance(desk_id, bool) else 0, str(underlying)[:40], "bad_argument")
    try:
        with (db or get_database()).read() as u:
            rows = u.session.query(
                "SELECT id, metric, threshold, backtest_run_ref FROM premium_thresholds "
                "WHERE desk_id = :d AND underlying = :u AND status = 'calibrated' AND backtest_run_ref IS NOT NULL "
                "AND length(backtest_run_ref) > 0 ORDER BY id DESC LIMIT 1", {"d": desk_id, "u": name})
        if not rows:
            return _closed(desk_id, name, "no_calibrated_threshold")
        row = rows[0]
        threshold = Decimal(str(row["threshold"]))
        if not threshold.is_finite():
            return _closed(desk_id, name, "bad_threshold")
        return SwitchState("open", desk_id, name, threshold, str(row["metric"]), str(row["backtest_run_ref"]), int(row["id"]), "calibrated")
    except Exception:                                               # noqa: BLE001  (fail closed on any error reading the table)
        return _closed(desk_id, name, "unreadable")


def record_calibration(desk_id: int, underlying: str, *, metric: str, threshold: Decimal, evidence: Any, backtest_run_ref: str,
                       db: Database | None = None) -> int:
    """Write the calibrated threshold of a desk and underlying, retiring the one it replaces. Written by H4 only.

    The evidence is the H1 `EvidenceBundle` of the backtest and must show a leak free study; the reference names the run.
    Reading the desk, retiring the old row and writing the new one happen in one transaction. Returns the id of the row."""
    from hokma.quant.validation.evidence import EvidenceBundle

    if isinstance(desk_id, bool) or not isinstance(desk_id, int) or desk_id < 1:
        raise ValueError("desk_id must be a desk id")
    name = _clean_underlying(underlying)
    if not isinstance(metric, str) or not _METRIC.match(metric):
        raise ValueError("metric is lowercase words joined by underscores, up to 40 characters")
    if isinstance(threshold, bool) or not isinstance(threshold, Decimal) or not threshold.is_finite():
        raise ValueError("threshold must be a finite Decimal")
    if not isinstance(backtest_run_ref, str) or not _REF.match(backtest_run_ref):
        raise ValueError(f"backtest_run_ref is 1 to {MAX_REF} printable characters with no spaces")
    if not isinstance(evidence, EvidenceBundle):
        raise ValueError("evidence must be the EvidenceBundle of the backtest")
    if evidence.n_observations <= 0 or evidence.leakage_free is not True:
        raise ValueError("the evidence must come from a study with observations and no leakage")
    body = json.dumps(evidence.judge_payload(), sort_keys=True, default=str)
    with (db or get_database()).write() as u:
        if u.session.one("SELECT 1 AS x FROM desks WHERE id = :d", {"d": desk_id}) is None:
            raise ValueError("there is no such desk")
        now = utcnow()
        for old in u.session.query("SELECT id FROM premium_thresholds WHERE desk_id = :d AND underlying = :u AND status = 'calibrated'",
                                   {"d": desk_id, "u": name}):
            u.session.update("premium_thresholds", {"id": old["id"]}, {"status": "retired", "retired_at": now})
        return int(u.session.insert("premium_thresholds", {
            "desk_id": desk_id, "underlying": name, "metric": metric, "threshold": str(threshold), "evidence_json": body,
            "backtest_run_ref": backtest_run_ref, "status": "calibrated", "calibrated_at": now}))


_REASON = re.compile(r"^[\x20-\x7e]{1,200}$")


def retire_calibration(threshold_id: int, *, reason: str, backtest_run_ref: str, db: Database | None = None) -> None:
    """Retire a calibrated row, so the switch of its desk and underlying closes. Written by H4 only (a recalibration that fails).

    The reason and the run that proved it are kept inside the evidence of the row, which is never deleted. Reading the row and retiring
    it happen in one transaction; a row that is already retired, or that does not exist, is a ValueError."""
    if isinstance(threshold_id, bool) or not isinstance(threshold_id, int) or threshold_id < 1:
        raise ValueError("threshold_id must be the id of a calibrated row")
    if not isinstance(reason, str) or not _REASON.match(reason):
        raise ValueError("reason is 1 to 200 printable characters")
    if not isinstance(backtest_run_ref, str) or not _REF.match(backtest_run_ref):
        raise ValueError(f"backtest_run_ref is 1 to {MAX_REF} printable characters with no spaces")
    with (db or get_database()).write() as u:
        row = u.session.one("SELECT status, evidence_json FROM premium_thresholds WHERE id = :i", {"i": threshold_id})
        if row is None:
            raise ValueError("there is no such calibration")
        if row["status"] != "calibrated":
            raise ValueError("the calibration is already retired")
        now = utcnow()
        body = json.loads(row["evidence_json"])
        body = {**body, "retired": {"reason": reason, "backtest_run_ref": backtest_run_ref, "at": str(now)}} if isinstance(body, dict) \
            else {"evidence": body, "retired": {"reason": reason, "backtest_run_ref": backtest_run_ref, "at": str(now)}}
        u.session.update("premium_thresholds", {"id": threshold_id}, {"status": "retired", "retired_at": now,
                                                                     "evidence_json": json.dumps(body, sort_keys=True, default=str)})


@dataclass(frozen=True)
class CalibratedRow:
    """A row that keeps the switch open: what the recalibration of H4 reads to know what to measure again."""

    row_id: int
    underlying: str
    metric: str
    threshold: Decimal
    backtest_run_ref: str
    calibrated_at: str


def calibrated_rows(desk_id: int, *, db: Database | None = None) -> list[CalibratedRow]:
    """The calibrated rows of a desk, one per underlying, in the order of the underlyings. Read only."""
    if isinstance(desk_id, bool) or not isinstance(desk_id, int) or desk_id < 1:
        raise ValueError("desk_id must be a desk id")
    with (db or get_database()).read() as u:
        rows = u.session.query("SELECT id, underlying, metric, threshold, backtest_run_ref, calibrated_at FROM premium_thresholds "
                               "WHERE desk_id = :d AND status = 'calibrated' AND backtest_run_ref IS NOT NULL AND length(backtest_run_ref) > 0 "
                               "ORDER BY underlying, id DESC", {"d": desk_id})
    seen: dict[str, CalibratedRow] = {}
    for r in rows:
        seen.setdefault(r["underlying"], CalibratedRow(int(r["id"]), r["underlying"], str(r["metric"]), Decimal(str(r["threshold"])),
                                                       str(r["backtest_run_ref"]), str(r["calibrated_at"])))
    return list(seen.values())


# ---- what is premium selling -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _OptionLeg:
    leg: Leg
    underlying: str | None
    put_call: str | None
    strike: Decimal | None
    expiration: date | None
    multiplier: Decimal | None

    @property
    def known(self) -> bool:
        return None not in (self.underlying, self.put_call, self.strike, self.expiration, self.multiplier)


def _prefix(contract_id: str) -> str:
    return contract_id.split(":", 1)[0].upper()


def _as_option(leg: Leg, sheet: ContractSheet | None) -> _OptionLeg | None:
    """The leg as an option, None when it is not one. A leg with no sheet that is named like an option is an option of which nothing is known."""
    if sheet is None:
        return _OptionLeg(leg, None, None, None, None, None) if _prefix(leg.contract_id) in OPTION_PREFIXES else None
    if sheet.asset_type not in OPTION_TYPES:
        return None
    underlying = (sheet.underlying or "").strip().upper() or None
    return _OptionLeg(leg, underlying, sheet.put_call, sheet.strike, sheet.expiration, sheet.multiplier)


def _covers(long: _OptionLeg, short: _OptionLeg) -> bool:
    """A long leg covers a short one when it bounds the loss: the same underlying and type, an expiration that is not earlier, and
    a strike on the protected side (a lower call or a higher put). A credit spread has its long strike on the wrong side."""
    if not (long.known and short.known) or long.underlying != short.underlying or long.put_call != short.put_call:
        return False
    if long.expiration < short.expiration:       # type: ignore[operator]
        return False
    return long.strike <= short.strike if short.put_call == "C" else long.strike >= short.strike    # type: ignore[operator]


def _uncovered_short(options: Sequence[_OptionLeg]) -> bool:
    shorts = [o for o in options if o.leg.side == "sell"]
    if not shorts:
        return False
    room = {id(o): o.leg.qty for o in options if o.leg.side == "buy"}
    longs = [o for o in options if o.leg.side == "buy"]
    for short in sorted(shorts, key=lambda o: o.leg.qty, reverse=True):
        if not short.known:
            return True
        need = short.leg.qty
        usable = sorted((o for o in longs if _covers(o, short)), key=lambda o: (o.expiration, abs(o.strike - short.strike)))    # type: ignore[type-var,operator]
        for long in usable:
            take = min(need, room[id(long)])
            room[id(long)] -= take
            need -= take
            if need <= 0:
                break
        if need > 0:
            return True
    return False


def _net_credit(options: Sequence[_OptionLeg], marks: Mapping[str, Any]) -> Decimal | None:
    """What the order receives: the sells minus the buys at the market marks, times quantity and multiplier. None when a mark is missing."""
    total = ZERO
    for o in options:
        mark = dec_or_none(marks.get(o.leg.contract_id))
        if mark is None or o.multiplier is None:
            return None
        value = o.leg.qty * mark * o.multiplier
        total += value if o.leg.side == "sell" else -value
    return total


def _opening_options(p: OrderProposal, contracts: Mapping[str, ContractSheet]) -> list[_OptionLeg]:
    out = []
    for leg in p.legs:
        if leg.effect != "open":
            continue
        option = _as_option(leg, contracts.get(leg.contract_id))
        if option is not None:
            out.append(option)
    return out


def is_premium_selling(p: OrderProposal, contracts: Mapping[str, ContractSheet], *, marks: Mapping[str, Any] | None = None,
                       positions: Sequence[Any] | None = None, open_orders: Sequence[Any] = ()) -> bool:
    """True when the proposal opens a position that sells premium. Closing legs never count.

    `positions` (the real ones) makes the effects derived: a leg declared as a close that reduces nothing is an opening. The guard always
    gives them. Without `marks` the credit of a covered structure cannot be known, so it counts as selling (fail closed)."""
    if positions is not None:
        p = normalize_effects(p, positions, open_orders)
    options = _opening_options(p, contracts)
    if not any(o.leg.side == "sell" for o in options):
        return False
    if _uncovered_short(options):
        return True
    credit = _net_credit(options, marks or {})
    return credit is None or credit > ZERO


@dataclass(frozen=True)
class _Held:
    """A position of the account once the orders that wait for the owner are taken as filled."""
    contract_id: str
    qty: Decimal


def with_pending(positions: Sequence[Any], pending: Sequence[OrderProposal]) -> tuple[Any, ...]:
    """The positions of the account plus the net effect of the other orders of the owner that still wait for his answer (bug 1242).
    With none waiting the positions come back as they are."""
    if not pending:
        return tuple(positions)
    net: dict[str, Decimal] = {}
    for x in positions:
        cid = str(getattr(x, "contract_id", "") or "")
        if cid:
            net[cid] = net.get(cid, ZERO) + (dec_or_none(getattr(x, "qty", 0)) or ZERO)
    for q in pending:
        for g in q.legs:
            net[g.contract_id] = net.get(g.contract_id, ZERO) + signed(g)
    return tuple(_Held(cid, qty) for cid, qty in net.items())


def _short_after(p: OrderProposal, positions: Sequence[Any], contract_id: str) -> Decimal:
    """What the account would be short in a contract once this order fills (zero when it ends flat or long)."""
    have = sum((dec_or_none(getattr(x, "qty", 0)) or ZERO for x in positions if getattr(x, "contract_id", None) == contract_id), ZERO)
    after = have + sum((signed(g) for g in p.legs if g.contract_id == contract_id), ZERO)
    return -after if after < 0 else ZERO


def _bounded_loss(options: Sequence[_OptionLeg]) -> Decimal | None:
    """The most a credit spread can lose, in money: the width of each short against the long that bounds it times quantity and multiplier.
    None when some short has no long of the same underlying, type and an expiration that is not earlier (the risk is then not bounded)."""
    room = {id(o): o.leg.qty for o in options if o.leg.side == "buy"}
    longs = [o for o in options if o.leg.side == "buy" and o.known]
    loss = ZERO
    for short in (o for o in options if o.leg.side == "sell"):
        if not short.known:
            return None
        need = short.leg.qty
        for long in sorted((o for o in longs if o.underlying == short.underlying and o.put_call == short.put_call
                            and o.expiration >= short.expiration), key=lambda o: abs(o.strike - short.strike)):    # type: ignore[operator]
            take = min(need, room[id(long)])
            room[id(long)] -= take
            need -= take
            loss += take * abs(long.strike - short.strike) * short.multiplier    # type: ignore[operator]
            if need <= 0:
                break
        if need > 0:
            return None
    return loss


def owner_risk(p: OrderProposal, contracts: Mapping[str, ContractSheet], *, marks: Mapping[str, Any], positions: Sequence[Any],
               open_orders: Sequence[Any] = (), pending: Sequence[OrderProposal] = ()) -> tuple[str, str, Decimal] | None:
    """What an order of the owner leaves open once the closes are netted against the positions, the live orders and the other orders of the
    owner that wait for his answer (`pending`, bug 1242: the exposure is the accumulated one, not the one of each order alone).
    None when it only reduces or when the sold options are covered (a long option that bounds them, or 100 shares per call). Otherwise
    ("short", contract, qty) when it leaves an option or shares sold with no cover (qty is the accumulated short) and ("spread", contract,
    max_loss) for a credit spread, whose loss is bounded but is still a sale of premium. The owner is warned and decides: nothing here blocks."""
    positions = with_pending(positions, pending)
    risk = _option_risk(p, contracts, marks, positions, open_orders)
    return risk if risk is not None else _equity_risk(p, positions, open_orders)


def _option_risk(p: OrderProposal, contracts: Mapping[str, ContractSheet], marks: Mapping[str, Any], positions: Sequence[Any],
                 open_orders: Sequence[Any]) -> tuple[str, str, Decimal] | None:
    if not is_premium_selling(p, contracts, marks=marks, positions=positions, open_orders=open_orders):
        return None
    options = _opening_options(normalize_effects(p, positions, open_orders), contracts)
    if not _uncovered_short(options):
        return None
    sold = [o for o in options if o.leg.side == "sell"]
    if all(o.put_call == "C" and o.known for o in sold):           # covered calls: the shares in the order or in the account are the cover
        shares: dict[str, Decimal] = {}
        for g in p.legs:
            if g.contract_id.upper().startswith("EQ:") and g.side == "buy":
                shares[g.contract_id] = shares.get(g.contract_id, ZERO) + g.qty
        for x in positions:
            cid = str(getattr(x, "contract_id", "") or "")
            if cid.upper().startswith("EQ:"):
                shares[cid] = shares.get(cid, ZERO) + (dec_or_none(getattr(x, "qty", 0)) or ZERO)
        need: dict[str, Decimal] = {}
        for o in sold:
            need["EQ:" + str(o.underlying)] = need.get("EQ:" + str(o.underlying), ZERO) + o.leg.qty * o.multiplier   # type: ignore[operator]
        if all(shares.get(k, ZERO) >= v for k, v in need.items()):
            return None
    loss = _bounded_loss(options)
    if loss is not None:
        return "spread", sold[0].leg.contract_id, loss
    first = sold[0].leg.contract_id
    return "short", first, max(sum((o.leg.qty for o in sold), ZERO), _short_after(p, positions, first))


def _equity_risk(p: OrderProposal, positions: Sequence[Any], open_orders: Sequence[Any]) -> tuple[str, str, Decimal] | None:
    """Shares sold that the account does not hold: a short sale has no ceiling on the loss (Vera 5a)."""
    for g in normalize_effects(p, positions, open_orders).legs:
        if g.contract_id.upper().startswith("EQ:") and g.side == "sell" and g.effect == "open":
            return "short", g.contract_id, max(g.qty, _short_after(p, positions, g.contract_id))
    return None


def _underlyings(p: OrderProposal, contracts: Mapping[str, ContractSheet]) -> list[str | None]:
    names = {o.underlying for o in _opening_options(p, contracts)}
    return sorted(names, key=lambda x: (x is None, x or ""))


class PremiumSellingGuard:
    """Guard 8 of the pipeline, between the limits of the committee and the rules of the broker. Orders of the owner in the chat are not its business."""

    name = GUARD
    applies_to_closing = False

    def check(self, ctx: GuardContext) -> GuardResult:
        try:
            p = normalize_effects(ctx.proposal, ctx.positions, ctx.open_orders)
            if p.origin == "chat":
                return passed(self.name, "chat_exempt")
            if not is_premium_selling(p, ctx.contracts, marks=ctx.marks):
                return passed(self.name, "not_premium_selling")
            for underlying in _underlyings(p, ctx.contracts):
                if underlying is None:
                    return blocked(self.name, CODE, t("exec.why.premium_selling_unreadable"))
                state = switch_state(ctx.desk_id, underlying)
                if not state.is_open:
                    why = t("exec.why.premium_selling_unreadable") if state.reason in ("unreadable", "bad_argument", "bad_threshold") \
                        else t("exec.why.premium_selling_closed", underlying=underlying)
                    return blocked(self.name, CODE, why)
            return passed(self.name, "switch_open")
        except Exception:                                           # noqa: BLE001  (fail closed on any error reading the contracts or the table)
            return blocked(self.name, CODE, t("exec.why.premium_selling_unreadable"))
