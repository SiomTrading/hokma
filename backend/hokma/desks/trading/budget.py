"""Size by plan: the minimum of the limits that apply, and a refusal before any stop is narrowed (SPEC-019: DTS-07a).

The contracts of an order are the smallest of these limits, each one a whole number of contracts rounded down:
1. risk per trade: the share of equity the mandate allows to lose, over the risk of one contract;
2. maximum premium of the desk: the cap of `max_premium_usd` over the premium bought by one contract;
3. the committee: its own cap on contracts, the position and order caps (`max_position_pct`, `max_order_pct`);
4. cash: the cash available over the cash one contract needs.
Completed (the complement of the order filter): the drawdown from the peak of the book, the capital floor and the rules of a funded
account (daily loss, trailing drawdown, contracts held). A limit that is set and whose input is unknown gives zero: it fails closed.

If the minimum is below one the answer is `NO_TRADE` and names the limit that binds. The stop of the plan is not an input of this
module and the module returns no stop: a size that does not fit is refused, it is never made to fit by narrowing the stop.
`pretrade_check` is the small base of the filter that runs before an order: it reads the same facts and the same limits, and the
filter of `hokma.exec` still decides at the moment of sending.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any

from hokma.exec import thresholds
from hokma.exec.money import dec_or_none

ZERO = Decimal(0)
HUNDRED = Decimal(100)
HARD_MAX_CONTRACTS = 10_000


@dataclass(frozen=True)
class BudgetInputs:
    """Facts and limits a size reads. Money is Decimal; a fact that is not known is None."""

    equity: Decimal | None
    cash: Decimal | None
    capital: Decimal = ZERO                       # capital of the desk: the base when the book has no equity
    peak_equity: Decimal | None = None
    day_pnl: Decimal | None = None
    open_contracts: Decimal | None = None
    risk_pct: Decimal = Decimal(1)                # percent of equity risked per trade (mandate)
    max_premium_usd: Decimal | None = None
    committee_max_contracts: int | None = None
    max_position_pct: Decimal | None = None
    max_order_pct: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    capital_floor_usd: Decimal | None = None
    funding: Mapping[str, Any] = field(default_factory=dict)
    daily_loss_steps: tuple[Mapping[str, Any], ...] | None = None        # SPEC-026 (A8): a USD that scales with the equity
    drawdown_steps: tuple[Mapping[str, Any], ...] | None = None
    desk_status: str = "active"
    orders_enabled: bool = True
    account_ok: bool = True

    @classmethod
    def from_book(cls, book: Any, limits: Mapping[str, Any], *, capital: Any = 0, risk_pct: Any = 1, committee_max_contracts: int | None = None,
                  desk_status: str = "active", orders_enabled: bool = True, account_ok: bool = True) -> "BudgetInputs":
        """The inputs from the back office snapshot of the account and the `desk_limits` of the desk."""
        def d(name: str) -> Decimal | None:
            return dec_or_none(limits.get(name), name)

        funding = dict(limits.get("funding") or {})
        return cls(equity=dec_or_none(getattr(book, "equity", None)), cash=dec_or_none(getattr(book, "cash", None)),
                   capital=dec_or_none(capital) or ZERO, peak_equity=dec_or_none(getattr(book, "peak_equity", None)),
                   day_pnl=dec_or_none(getattr(book, "day_pnl", None)), open_contracts=dec_or_none(getattr(book, "open_contracts", None)),
                   risk_pct=dec_or_none(risk_pct) or Decimal(1), max_premium_usd=d("max_premium_usd"),
                   committee_max_contracts=committee_max_contracts, max_position_pct=d("max_position_pct"), max_order_pct=d("max_order_pct"),
                   max_drawdown_pct=d("max_drawdown_pct"), capital_floor_usd=d("capital_floor_usd"), funding=funding,
                   daily_loss_steps=tuple(limits.get("daily_loss_steps") or ()) or None,
                   drawdown_steps=tuple(limits.get("drawdown_steps") or ()) or None,
                   desk_status=desk_status, orders_enabled=orders_enabled, account_ok=account_ok)

    @classmethod
    def from_context(cls, ctx: Any, *, risk_pct: Any = 1, committee_max_contracts: int | None = None) -> "BudgetInputs":
        """The inputs from the `GuardContext` of the filter: the same facts, read once."""
        return cls.from_book(ctx.book, ctx.limits, capital=ctx.capital, risk_pct=risk_pct, committee_max_contracts=committee_max_contracts,
                             desk_status=ctx.desk_status, orders_enabled=ctx.orders_enabled, account_ok=ctx.account_ok)

    @property
    def base(self) -> Decimal | None:
        """What a percentage is a percentage of: the equity of the book, else the capital of the desk."""
        if self.equity is not None and self.equity > 0:
            return self.equity
        return self.capital if self.capital > 0 else None


@dataclass(frozen=True)
class BudgetResult:
    status: str                                    # "OK" or "NO_TRADE"
    contracts: int
    binding: str                                   # the limit that gave the minimum
    limits: Mapping[str, int | None]               # contracts each limit allows; None when the limit does not apply
    code: str = "ok"
    reason: str = ""
    risk_budget_usd: Decimal = ZERO                # the risk budget of the first limit, for the record


@dataclass(frozen=True)
class Blocker:
    code: str
    why: str


def _floor(numerator: Decimal, denominator: Decimal) -> int:
    """Exact floor of a ratio of Decimals, never below zero."""
    if denominator <= 0:
        raise ValueError("the unit amount must be above zero")
    if numerator <= 0:
        return 0
    return (Fraction(numerator) / Fraction(denominator)).__floor__()


def _loss_cap(inp: BudgetInputs) -> Decimal | None:
    """The USD the account may lose in a day: the fixed one and the step of the base (as the account guard), the smaller holds."""
    return thresholds.effective_usd(dec_or_none(inp.funding.get("daily_loss_usd")), inp.daily_loss_steps, inp.base)


def _trailing_cap(inp: BudgetInputs) -> Decimal | None:
    """The USD the account may fall from its peak: the fixed one and the step of the equity (as the account guard), the smaller holds."""
    return thresholds.effective_usd(dec_or_none(inp.funding.get("trailing_drawdown_usd")), inp.drawdown_steps,
                                    inp.equity if inp.equity is not None and inp.equity > 0 else None)


def _drawdown_room(inp: BudgetInputs) -> tuple[Decimal | None, str]:
    if inp.max_drawdown_pct is None:
        return None, ""
    if inp.equity is None or inp.peak_equity is None or inp.peak_equity <= 0:
        return None, "max_drawdown_pct is set and the equity or its peak is unknown"
    return inp.peak_equity * inp.max_drawdown_pct / HUNDRED - (inp.peak_equity - inp.equity), ""


def pretrade_check(inp: BudgetInputs) -> tuple[Blocker, ...]:
    """The base of the filter before an order: what stops any new risk whatever its size. An empty tuple is a clear road."""
    out: list[Blocker] = []
    if inp.desk_status != "active":
        out.append(Blocker("desk_inactive", f"the desk is {inp.desk_status}"))
    if not inp.orders_enabled:
        out.append(Blocker("orders_off", "orders are switched off for this desk"))
    if not inp.account_ok:
        out.append(Blocker("no_account", "the order has no trading account"))      # the account the order resolved to; a desk with none never gets here (SPEC-027 4.3)
    room, unknown = _drawdown_room(inp)
    if unknown:
        out.append(Blocker("drawdown_unknown", unknown))
    elif room is not None and room <= 0:
        out.append(Blocker("max_drawdown_pct", "the drawdown from the peak of the book already reached its limit"))
    if inp.capital_floor_usd is not None:
        if inp.equity is None:
            out.append(Blocker("capital_floor_unknown", "capital_floor_usd is set and the equity is unknown"))
        elif inp.equity <= inp.capital_floor_usd:
            out.append(Blocker("capital_floor", f"the equity {inp.equity} is at or under the floor of {inp.capital_floor_usd}"))
    loss = _loss_cap(inp)
    if loss is not None:
        if inp.day_pnl is None:
            out.append(Blocker("daily_loss_unknown", "the funded daily loss is set and the result of the day is unknown"))
        elif inp.day_pnl + loss <= 0:
            out.append(Blocker("daily_loss", "the funded account already lost its daily allowance"))
    trailing = _trailing_cap(inp)
    if trailing is not None:
        if inp.equity is None or inp.peak_equity is None:
            out.append(Blocker("trailing_drawdown_unknown", "the trailing drawdown is set and the equity or its peak is unknown"))
        elif trailing - (inp.peak_equity - inp.equity) <= 0:
            out.append(Blocker("trailing_drawdown", "the funded account reached its trailing drawdown"))
    return tuple(out)


def size_by_plan(inp: BudgetInputs, *, risk_per_contract_usd: Decimal, premium_per_contract_usd: Decimal, cash_per_contract_usd: Decimal,
                 max_contracts: int = HARD_MAX_CONTRACTS) -> BudgetResult:
    """The contracts of one order: the smallest of the limits above.

    `risk_per_contract_usd` is what one contract loses at the stop; `premium_per_contract_usd` the premium it buys (the sum of its
    bought legs, which is what the premium cap of the desk counts); `cash_per_contract_usd` the cash it needs, commission and slippage
    included. The stop is not an argument: a size that does not fit is refused, the stop stays where it is."""
    for name, value in (("risk_per_contract_usd", risk_per_contract_usd), ("cash_per_contract_usd", cash_per_contract_usd)):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(f"{name} must be a Decimal above zero")
    if not isinstance(premium_per_contract_usd, Decimal) or not premium_per_contract_usd.is_finite() or premium_per_contract_usd < 0:
        raise ValueError("premium_per_contract_usd must be a Decimal of zero or more")
    limits: dict[str, int | None] = {}
    base = inp.base
    budget = ZERO
    if base is None:
        limits["risk_per_trade"] = 0
    else:
        budget = base * inp.risk_pct / HUNDRED
        limits["risk_per_trade"] = _floor(budget, risk_per_contract_usd)
    if inp.max_premium_usd is not None and premium_per_contract_usd > 0:
        limits["max_premium"] = _floor(inp.max_premium_usd, premium_per_contract_usd)
    committee: list[int] = []
    if inp.committee_max_contracts is not None:
        committee.append(max(int(inp.committee_max_contracts), 0))
    for pct in (inp.max_position_pct, inp.max_order_pct):
        if pct is not None:
            committee.append(0 if base is None else _floor(base * pct / HUNDRED, cash_per_contract_usd))
    limits["committee"] = min(committee) if committee else None
    limits["cash"] = 0 if inp.cash is None else _floor(min(inp.cash, inp.equity) if inp.equity is not None and inp.equity > 0 else inp.cash,
                                                       cash_per_contract_usd)
    room, unknown = _drawdown_room(inp)
    if inp.max_drawdown_pct is not None:
        limits["drawdown"] = 0 if unknown else _floor(room or ZERO, risk_per_contract_usd)
    if inp.capital_floor_usd is not None:
        limits["capital_floor"] = 0 if inp.equity is None else _floor(inp.equity - inp.capital_floor_usd, risk_per_contract_usd)
    funding = inp.funding
    loss = _loss_cap(inp)
    if loss is not None:
        limits["funding_daily_loss"] = 0 if inp.day_pnl is None else _floor(loss + inp.day_pnl, risk_per_contract_usd)
    trailing = _trailing_cap(inp)
    if trailing is not None:
        limits["funding_trailing"] = (0 if inp.equity is None or inp.peak_equity is None
                                      else _floor(trailing - (inp.peak_equity - inp.equity), risk_per_contract_usd))
    cap = dec_or_none(funding.get("max_contracts"))
    if cap is not None:
        held = inp.open_contracts
        limits["funding_max_contracts"] = 0 if held is None else max(int((cap - held).to_integral_value(rounding="ROUND_FLOOR")), 0)
    applicable = {k: v for k, v in limits.items() if v is not None}
    binding = min(applicable, key=lambda k: (applicable[k], k))
    count = min(applicable[binding], max_contracts)
    if count < 1:
        return BudgetResult("NO_TRADE", 0, binding, limits, f"no_room_{binding}",
                            f"the limit {binding} leaves room for no contract; the order is refused and the stop is not narrowed", budget)
    return BudgetResult("OK", count, binding if applicable[binding] <= max_contracts else "max_contracts", limits, "ok", "", budget)
