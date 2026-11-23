"""The order proposal (SPEC-018: OA-41). One typed shape for what a desk, the owner in the chat or an approved rule wants to
send. It is the unit that is hashed (the fingerprint), approved and sent: what is approved is what is sent.

`Line` is the one of `hokma.desks.core.signal`. The contract of a leg is the canonical id of a sheet (`hokma.contracts`), never a
raw provider symbol. Every amount is a Decimal. `thesis` is read by the owner and is not part of the hash.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from decimal import Decimal
from typing import Any, Literal

from hokma.core.contenthash import content_hash
from hokma.desks.core.signal import LINES, Line
from hokma.exec.money import MoneyError, dec, dec_or_none, text

Side = Literal["buy", "sell"]
Effect = Literal["open", "close"]
ORDER_TYPES = ("market", "limit", "stop", "stop_limit")
ORIGINS = ("desk", "chat", "rule")
TIFS = ("day", "gtc")
MAX_LEGS = 4
NOT_HASHED = ("thesis", "decision_id", "created_by")


class ProposalError(ValueError):
    """The proposal does not fit the schema. `code` is stable (bad_proposal)."""

    code = "bad_proposal"


@dataclass(frozen=True)
class Leg:
    contract_id: str                      # canonical id from hokma.contracts (TA-33); never a raw provider symbol
    side: Side
    effect: Effect
    qty: Decimal                          # contracts or shares, always a Decimal (OA-56)

    def __post_init__(self) -> None:
        if not isinstance(self.contract_id, str) or not 3 <= len(self.contract_id) <= 80 or ":" not in self.contract_id:
            raise ProposalError("a leg needs the canonical contract id of a sheet")
        if self.side not in ("buy", "sell") or self.effect not in ("open", "close"):
            raise ProposalError("a leg needs side buy or sell and effect open or close")
        if not isinstance(self.qty, Decimal) or not self.qty.is_finite() or self.qty <= 0:
            raise ProposalError("a leg quantity is a Decimal above zero")

    def canonical(self) -> dict[str, Any]:
        return {"contract_id": self.contract_id, "effect": self.effect, "qty": text(self.qty), "side": self.side}


@dataclass(frozen=True)
class OrderProposal:
    desk_id: int
    line: Line
    origin: Literal["desk", "chat", "rule"]
    legs: tuple[Leg, ...]
    order_type: Literal["market", "limit", "stop", "stop_limit"]
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: Literal["day", "gtc"] = "day"
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    reduce_only: bool = False
    risk_usd: Decimal | None = None
    thesis: str = ""
    decision_id: int | None = None
    created_by: str = "owner"

    def __post_init__(self) -> None:
        if isinstance(self.desk_id, bool) or not isinstance(self.desk_id, int) or self.desk_id < 1:
            raise ProposalError("desk_id must be a desk id")
        if self.line not in LINES:
            raise ProposalError(f"line must be one of {', '.join(LINES)}")
        if self.origin not in ORIGINS:
            raise ProposalError(f"origin must be one of {', '.join(ORIGINS)}")
        if not isinstance(self.legs, tuple) or not 1 <= len(self.legs) <= MAX_LEGS or not all(isinstance(g, Leg) for g in self.legs):
            raise ProposalError(f"a proposal has 1 to {MAX_LEGS} legs")
        ids = [g.contract_id for g in self.legs]
        if len(set(ids)) != len(ids):
            raise ProposalError("a contract can appear in only one leg of a proposal")
        if self.order_type not in ORDER_TYPES or self.tif not in TIFS:
            raise ProposalError("order_type or tif is not valid")
        for name in ("limit_price", "stop_price", "stop_loss", "take_profit", "risk_usd"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
                raise ProposalError(f"{name} must be a Decimal of zero or more")
        if self.order_type in ("limit", "stop_limit") and self.limit_price is None:
            raise ProposalError(f"a {self.order_type} order needs limit_price")
        if self.order_type in ("stop", "stop_limit") and self.stop_price is None:
            raise ProposalError(f"a {self.order_type} order needs stop_price")
        if not isinstance(self.reduce_only, bool):
            raise ProposalError("reduce_only must be true or false")
        if not isinstance(self.thesis, str) or len(self.thesis) > 4000:
            raise ProposalError("thesis is text of at most 4000 characters")
        if not isinstance(self.created_by, str) or not 1 <= len(self.created_by) <= 80:
            raise ProposalError("created_by is 1 to 80 characters")

    def canonical(self) -> dict[str, Any]:
        """Sorted, Decimal as text, without the thesis, the decision and the author: what is hashed."""
        return {"desk_id": self.desk_id, "line": self.line, "origin": self.origin,
                "legs": [g.canonical() for g in self.legs], "order_type": self.order_type,
                "limit_price": text(self.limit_price), "stop_price": text(self.stop_price), "tif": self.tif,
                "stop_loss": text(self.stop_loss), "take_profit": text(self.take_profit), "reduce_only": self.reduce_only,
                "risk_usd": text(self.risk_usd)}

    def fingerprint(self) -> str:
        """The only fingerprint of an order in Hokma (`desks.core.hashing.content_hash` of `canonical()`)."""
        return content_hash(self.canonical(), ignore=frozenset())

    def to_json(self) -> dict[str, Any]:
        """Everything, to be saved: the canonical form plus the thesis, the decision and the author."""
        return {**self.canonical(), "thesis": self.thesis, "decision_id": self.decision_id, "created_by": self.created_by}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, desk_id: int | None = None, origin: str | None = None,
                  created_by: str | None = None) -> "OrderProposal":
        """The schema of a proposal from JSON. An unknown key is refused. `desk_id`, `origin` and `created_by` can be given
        by the caller (the tool knows them), and then they must not be in the data."""
        if not isinstance(data, Mapping):
            raise ProposalError("a proposal is an object")
        allowed = {f.name for f in fields(cls)}
        extra = sorted(set(data) - allowed)
        if extra:
            raise ProposalError(f"unknown field: {str(extra[0])[:40]}")
        body = dict(data)
        for name, given in (("desk_id", desk_id), ("origin", origin), ("created_by", created_by)):
            if given is not None:
                if name in body and body[name] != given:
                    raise ProposalError(f"{name} is set by the caller and cannot be given")
                body[name] = given
        legs = body.get("legs")
        if not isinstance(legs, (list, tuple)) or not legs:
            raise ProposalError("legs must be a list with at least one leg")
        try:
            parsed = tuple(_leg(x) for x in legs)
            kwargs: dict[str, Any] = {k: body[k] for k in ("desk_id", "line", "origin", "order_type") if k in body}
            for name in ("limit_price", "stop_price", "stop_loss", "take_profit", "risk_usd"):
                if body.get(name) is not None:
                    kwargs[name] = dec(body[name], name)
            for name in ("tif", "reduce_only", "thesis", "decision_id", "created_by"):
                if name in body:
                    kwargs[name] = body[name]
            return cls(legs=parsed, **kwargs)
        except MoneyError as exc:
            raise ProposalError(str(exc)) from exc
        except TypeError as exc:
            raise ProposalError(f"a required field is missing: {exc}") from exc


def _leg(item: Any) -> Leg:
    if isinstance(item, Leg):
        return item
    if not isinstance(item, Mapping):
        raise ProposalError("a leg is an object")
    extra = sorted(set(item) - {"contract_id", "side", "effect", "qty"})
    if extra:
        raise ProposalError(f"unknown leg field: {str(extra[0])[:40]}")
    try:
        return Leg(item["contract_id"], item["side"], item["effect"], dec(item["qty"], "qty"))
    except KeyError as exc:
        raise ProposalError(f"a leg needs {exc.args[0]}") from exc


def fingerprint_of(body: Any) -> str:
    """The hash of any order body (a native order that waits for the owner): the same calculation as `OrderProposal.fingerprint`."""
    return content_hash(body, ignore=frozenset())
