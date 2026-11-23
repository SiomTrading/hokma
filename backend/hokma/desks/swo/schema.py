"""The proposal of a stock options desk (SPEC-019: TA-36).

`SWOProposal` is an `OrderProposal` (SPEC-018) that adds what an options order needs to be read: the structure, the expiration,
the net premium, the most it can lose and the underlying. Nothing the agent declares here decides a control. The structure is
checked against the legs, which are the only thing that is sent: a name that is not what the legs make is refused. The premium
switch, the limits and the effect of each leg are derived by code from the legs and from the real positions.

The fingerprint of a `SWOProposal` is the fingerprint of its order: what is approved is what is sent. The extra fields are
checked here and travel to the owner as the first line of the thesis (`to_order`); they are not stored in `exec_proposals`.
Unknown keys are refused. Every field has a description that instructs the model (`field_descriptions`).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import date
from decimal import Decimal
from typing import Any, Literal, get_args

from hokma.contracts.identity import NoContract, Parsed, parse_canonical
from hokma.exec.money import MoneyError, dec, dec_or_none, text
from hokma.exec.proposal import Leg, OrderProposal, ProposalError

Structure = Literal["long_call", "long_put", "debit_call_spread", "debit_put_spread", "short_put", "short_call", "credit_call_spread",
                    "credit_put_spread", "iron_condor", "short_straddle", "short_strangle", "covered_call", "cash_secured_put"]
STRUCTURES: tuple[str, ...] = get_args(Structure)
DEBIT_STRUCTURES = frozenset({"long_call", "long_put", "debit_call_spread", "debit_put_spread"})
CREDIT_STRUCTURES = frozenset(STRUCTURES) - DEBIT_STRUCTURES
UNBOUNDED_LOSS = frozenset({"short_call", "short_straddle", "short_strangle"})
SHARES_PER_OPTION = 100                  # the shares a standard listed option covers (a covered call holds this many per call)
SWO_KEYS = ("structure", "expiration", "net_premium", "max_loss_usd", "underlying")
BASE_KEYS = frozenset(f.name for f in fields(OrderProposal))

BASE_DESCRIPTIONS: Mapping[str, str] = {
    "desk_id": "The desk this proposal is for. Never write it: the tool sets it from your own desk.",
    "line": "The line of the desk, SWO. Never write it: the code takes it from the profile of the desk.",
    "origin": "Who asks for the order. Never write it: a desk proposal always has origin desk.",
    "legs": "One to four legs, each with the canonical contract id of the option or share, the side buy or sell and a quantity. Take the ids from the chain you were given; never invent a contract.",
    "order_type": "Use limit for an options structure, with limit_price as the net price of the whole structure. Market orders on options are avoided.",
    "limit_price": "The net price of the whole structure per share, from the chain quotes you were given. Write nothing you did not read there.",
    "stop_price": "Only for stop and stop-limit orders. Leave it empty for an entry order.",
    "tif": "Time in force: day for an entry order. Use gtc only when the plan says the order should wait.",
    "stop_loss": "The net premium at which the structure is closed at a loss. Never write it: the exits module computes it from the plan.",
    "take_profit": "The net premium at which the structure is closed at a profit. Never write it: the exits module computes it from the plan.",
    "reduce_only": "True only for an order that closes risk. An entry order is never reduce only.",
    "risk_usd": "The loss in USD if the stop is hit. Never write it: the sizing module computes it.",
    "thesis": "The reason for the trade in the words the owner reads: the view, the evidence and what would prove it wrong. Do not write sizes, stops or objectives in it.",
    "decision_id": "The decision this order follows, when the desk made one. Leave it empty if you have none.",
    "created_by": "Who proposed it. Never write it: the tool sets it from the agent that calls.",
}


@dataclass(frozen=True, kw_only=True)
class SWOProposal(OrderProposal):                 # SPEC-018 4.2
    structure: Structure = field(metadata={"description": "The name of the option structure the legs make: long_call, long_put, debit_call_spread, debit_put_spread, short_put, short_call, credit_call_spread, credit_put_spread, iron_condor, short_straddle, short_strangle, covered_call or cash_secured_put. It must match the legs: a different name is refused."})
    expiration: date = field(metadata={"description": "The expiration date of the structure as YYYY-MM-DD: the nearest expiration among its option legs."})
    net_premium: Decimal = field(metadata={"description": "The net premium per share of one structure, from the chain quotes: positive is a debit you pay, negative is a credit you receive. Buy legs count at the ask and sell legs at the bid."})
    max_loss_usd: Decimal | None = field(metadata={"description": "The most the whole order can lose in USD, from the strikes and the premium. Write null only for a structure whose loss has no bound: short_call, short_straddle and short_strangle."})
    underlying: str = field(metadata={"description": "The symbol of the share the options are on, for example AAPL. Every leg must be on it."})

    def __post_init__(self) -> None:
        OrderProposal.__post_init__(self)
        if self.line != "SWO":
            raise ProposalError("a SWO proposal belongs to a desk of the line SWO")
        if self.structure not in STRUCTURES:
            raise ProposalError(f"structure must be one of {', '.join(STRUCTURES)}")
        if not isinstance(self.expiration, date):
            raise ProposalError("expiration must be a date")
        if not isinstance(self.net_premium, Decimal) or not self.net_premium.is_finite() or self.net_premium == 0:
            raise ProposalError("net_premium must be a Decimal different from zero (positive debit, negative credit)")
        if self.max_loss_usd is not None and (not isinstance(self.max_loss_usd, Decimal) or not self.max_loss_usd.is_finite() or self.max_loss_usd < 0):
            raise ProposalError("max_loss_usd must be a Decimal of zero or more, or empty")
        if not isinstance(self.underlying, str) or not 1 <= len(self.underlying) <= 12:
            raise ProposalError("underlying is the symbol of a share")
        if (self.structure in DEBIT_STRUCTURES) != (self.net_premium > 0):
            raise ProposalError(f"{self.structure} has a net premium {'above' if self.structure in DEBIT_STRUCTURES else 'below'} zero")
        if (self.max_loss_usd is None) != (self.structure in UNBOUNDED_LOSS):
            raise ProposalError("max_loss_usd is empty only for a structure with no bound on its loss, and then it must be empty")
        info = _parse_legs(self.legs)
        derived = derive_structure(self.legs)
        if derived is None or not (derived == self.structure or (derived == "short_put" and self.structure == "cash_secured_put")):
            raise ProposalError(f"the legs do not make a {self.structure}" + ("" if derived is None else f": they make a {derived}"))
        if any(leg.effect != "open" for leg in self.legs):
            raise ProposalError("a SWO structure proposal opens risk: every leg opens (a close is an order without a structure)")
        if {x.underlying for x in info} != {self.underlying}:
            raise ProposalError("every leg must be on the underlying of the proposal")
        option_days = [x.expiration for x in info if x.expiration is not None]
        if option_days and min(option_days) != self.expiration:
            raise ProposalError("expiration is the nearest expiration among the option legs")

    @classmethod
    def field_descriptions(cls) -> dict[str, str]:
        """The description of every field, the ones that instruct the model. A field without one is a defect (the test checks)."""
        out: dict[str, str] = {}
        for f in fields(cls):
            out[f.name] = f.metadata.get("description") or BASE_DESCRIPTIONS.get(f.name, "")
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, desk_id: int | None = None, origin: str | None = None,
                  created_by: str | None = None) -> "SWOProposal":
        """The proposal from JSON. An unknown key is refused; the order fields follow `OrderProposal.from_dict`."""
        if not isinstance(data, Mapping):
            raise ProposalError("a proposal is an object")
        extra = sorted(set(data) - BASE_KEYS - set(SWO_KEYS))
        if extra:
            raise ProposalError(f"unknown field: {str(extra[0])[:40]}")
        missing = [k for k in ("structure", "expiration", "net_premium", "underlying") if data.get(k) in (None, "")]
        if missing:
            raise ProposalError(f"a SWO proposal needs {missing[0]}")
        base = OrderProposal.from_dict({k: v for k, v in data.items() if k in BASE_KEYS}, desk_id=desk_id, origin=origin, created_by=created_by)
        try:
            expiration = data["expiration"] if isinstance(data["expiration"], date) else date.fromisoformat(str(data["expiration"]))
            net = dec(data["net_premium"], "net_premium")
            max_loss = dec_or_none(data.get("max_loss_usd"), "max_loss_usd")
        except (ValueError, MoneyError) as exc:
            raise ProposalError(str(exc)) from exc
        return cls.from_order(base, structure=data["structure"], expiration=expiration, net_premium=net, max_loss_usd=max_loss,
                              underlying=str(data["underlying"]).strip().upper())

    @classmethod
    def from_order(cls, order: OrderProposal, *, structure: str, expiration: date, net_premium: Decimal, max_loss_usd: Decimal | None,
                   underlying: str) -> "SWOProposal":
        values = {f.name: getattr(order, f.name) for f in fields(OrderProposal)}
        return cls(**values, structure=structure, expiration=expiration, net_premium=net_premium, max_loss_usd=max_loss_usd,
                   underlying=underlying)

    def summary(self) -> str:
        """The first line of the thesis for the owner: structure, underlying, expiration, premium and most it can lose."""
        kind = "debit" if self.net_premium > 0 else "credit"
        loss = "no bound" if self.max_loss_usd is None else f"{text(self.max_loss_usd)} USD"
        return (f"{self.structure} on {self.underlying}, expires {self.expiration.isoformat()}, net {kind} "
                f"{text(abs(self.net_premium))} per share, most it can lose: {loss}.")

    def to_order(self) -> OrderProposal:
        """The plain order that goes to `hokma.exec`. Same fingerprint; the thesis starts with the summary so the owner reads it."""
        values = {f.name: getattr(self, f.name) for f in fields(OrderProposal)}
        thesis = f"[{self.summary()}] {self.thesis}".strip()
        values["thesis"] = thesis[:4000]
        return OrderProposal(**values)

    def to_tool_body(self) -> dict[str, Any]:
        """The proposal as the body of `exec_propose`: the order fields (without what the tool sets itself) and the SWO fields."""
        body = {k: v for k, v in self.to_json().items() if k not in ("desk_id", "origin", "created_by", "decision_id")}
        body = {k: v for k, v in body.items() if v is not None}
        body.update(structure=self.structure, expiration=self.expiration.isoformat(), net_premium=text(self.net_premium),
                    underlying=self.underlying, thesis=self.thesis)
        if self.max_loss_usd is not None:
            body["max_loss_usd"] = text(self.max_loss_usd)
        return body


@dataclass(frozen=True)
class _LegInfo:
    kind: Literal["C", "P"] | None        # None for a share
    strike: Decimal | None
    expiration: date | None
    underlying: str
    side: str
    qty: Decimal


def _parse_one(leg: Leg) -> _LegInfo:
    try:
        parsed: Parsed = parse_canonical(leg.contract_id)
    except NoContract as exc:
        raise ProposalError(f"{leg.contract_id[:40]} is not a canonical id") from exc
    if parsed.asset_type == "option":
        return _LegInfo(parsed.put_call, parsed.strike, parsed.expiration, parsed.underlying or "", leg.side, leg.qty)  # type: ignore[arg-type]
    if parsed.asset_type in ("equity", "etf"):
        return _LegInfo(None, None, None, parsed.root or "", leg.side, leg.qty)
    raise ProposalError("a SWO proposal holds options on a share and, for a covered call, the share")


def _parse_legs(legs: Sequence[Leg]) -> list[_LegInfo]:
    return [_parse_one(leg) for leg in legs]


def derive_structure(legs: Sequence[Leg]) -> str | None:
    """The name of the structure the legs make, or None when they make none of the thirteen. Read from the legs only."""
    info = _parse_legs(legs)
    options = [x for x in info if x.kind is not None]
    shares = [x for x in info if x.kind is None]
    if shares:
        if len(shares) == 1 and len(options) == 1 and shares[0].side == "buy" and options[0].kind == "C" and options[0].side == "sell" \
                and shares[0].qty == options[0].qty * SHARES_PER_OPTION and shares[0].underlying == options[0].underlying:
            return "covered_call"
        return None
    if not options or len({x.underlying for x in options}) != 1 or len({x.qty for x in options}) != 1:
        return None
    n = len(options)
    if n == 1:
        x = options[0]
        return {("C", "buy"): "long_call", ("P", "buy"): "long_put", ("C", "sell"): "short_call", ("P", "sell"): "short_put"}[(x.kind, x.side)]  # type: ignore[index]
    if len({x.expiration for x in options}) != 1:
        return None
    if n == 2:
        a, b = options
        if a.kind == b.kind:
            buy = a if a.side == "buy" else b
            sell = b if buy is a else a
            if buy.side != "buy" or sell.side != "sell" or buy.strike == sell.strike:
                return None
            if buy.kind == "C":
                return "debit_call_spread" if buy.strike < sell.strike else "credit_call_spread"      # type: ignore[operator]
            return "debit_put_spread" if buy.strike > sell.strike else "credit_put_spread"            # type: ignore[operator]
        if a.side == "sell" and b.side == "sell":
            put = a if a.kind == "P" else b
            call = b if put is a else a
            if put.strike == call.strike:
                return "short_straddle"
            return "short_strangle" if put.strike < call.strike else None                              # type: ignore[operator]
        return None
    if n == 4:
        puts = sorted((x for x in options if x.kind == "P"), key=lambda x: x.strike)                   # type: ignore[arg-type,return-value]
        calls = sorted((x for x in options if x.kind == "C"), key=lambda x: x.strike)                  # type: ignore[arg-type,return-value]
        if len(puts) == 2 and len(calls) == 2 and puts[0].side == "buy" and puts[1].side == "sell" and calls[0].side == "sell" \
                and calls[1].side == "buy" and puts[1].strike < calls[0].strike:                       # type: ignore[operator]
            return "iron_condor"
    return None
