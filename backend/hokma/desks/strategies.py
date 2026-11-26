"""Data of the Strategies panel of a desk (SPEC-028, delivery 5, piece 3338-A): the cards of a view on the real chain, and one verdict per card from the
options seat of the desk in a single call to the model.

The numbers are made by hokma.quant.options.strategies (strikes of the chain, payoff, breakevens, probability of profit). This module reads the chain
(hokma.market.options: the price of the underlying comes from the stream of the market hub, the chain is the provider's snapshot of this request, with
its time), the premium switch of the desk, the shares of the account of the desk and its token budget, and it asks the model once. It asks again only
when the caller asks again: the panel calls it when the owner changes a field, never on a timer.

A card that sells premium while the switch is closed is returned locked with its reason, never hidden. A card that does not fit the budget says so
with its cost. Without a model (no seat, no engine, budget of the week spent, an answer that cannot be read) every card says "no verdict" with the
reason and the numbers stay. Nothing here writes to the database or sends an order.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import date
from typing import Any

from hokma.db import Database, get_database
from hokma.db.repos.conversations import LEDGER_TITLE  # noqa: F401  (the repo owns the title: it hides and protects that conversation)
from hokma.desks import budget as desk_budget
from hokma.desks.errors import DeskError
from hokma.i18n import language, t
from hokma.quant.options import strategies as quant

log = logging.getLogger(__name__)

SEAT = "A07"                          # the options seat of the desk (Caleb)
CHAIN_STRIKES = 50                    # strikes of the window around the price that the chain read asks for
DEFAULT_MIN_DAYS = 21
MAX_TOKENS = 1800
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SYMBOL = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")
PRIORITY_NAMES = {"balanced": ("balanced", "equilibrio"), "max_return": ("max_return", "maximo_retorno", "max_return", "maximum_return"),
                  "max_probability": ("max_probability", "maxima_probabilidad", "maximum_probability")}
VERDICT_IDS = ("recommended", "possible", "not_worth")
SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["reading", "verdicts"],
    "properties": {"reading": {"type": "string"},
                   "verdicts": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "verdict", "reason"],
                                                           "properties": {"id": {"type": "string"}, "verdict": {"type": "string", "enum": list(VERDICT_IDS)},
                                                                          "reason": {"type": "string"}}}}}}
SYSTEM = (
    "You are the options analyst of a trading desk. You rate option strategies that were already computed from a real option chain. "
    "Use only the figures you are given: never invent a price, a probability or a fact about the company. "
    "For each card in 'to_rate' answer a verdict: 'recommended' when its figures serve the view and the priority of the owner and its probability of profit and "
    "its reward against its risk justify it, 'possible' when it is acceptable but weaker than a better card or its figures are mixed, 'not_worth' when its "
    "reward is small against its risk, or it does not serve the view. At most one or two cards may be 'recommended'. "
    "The reason of each verdict is one sentence of at most 200 characters that cites a figure of the card. "
    "'reading' is one paragraph of at most 600 characters that reads the table for the owner: the view, what the chain says, and which card stands out and why. "
    "Cards in 'not_rated' are closed, over the budget or need shares; you may mention them in the reading but you give them no verdict."
)


def _bad() -> DeskError:
    return DeskError("bad_strategy_args", 422)


def _number(value: Any, *, positive: bool = True) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _bad()
    try:
        number = float(value)
    except ValueError:
        raise _bad() from None
    if number != number or number in (float("inf"), float("-inf")) or number < 0 or (positive and number == 0):
        raise _bad()
    return number


def _priority(value: Any) -> str:
    key = quant._norm(value if value not in (None, "") else "balanced")
    for pid, names in PRIORITY_NAMES.items():
        if key in names:
            return pid
    raise _bad()


def check_arguments(symbol: Any, view: Any, target: Any, budget: Any, expiration: Any, priority: Any) -> dict[str, Any]:
    """The request in its clean form, or DeskError bad_strategy_args (422). The view is accepted by id or by label in Spanish or English."""
    if not isinstance(symbol, str) or not SYMBOL.match(symbol.strip()):
        raise _bad()
    vid = quant.parse_view(view) if isinstance(view, str) else None
    if vid is None:
        raise _bad()
    day = None
    if expiration not in (None, ""):
        if not isinstance(expiration, str) or not ISO_DAY.match(expiration):
            raise _bad()
        try:
            day = date.fromisoformat(expiration)
        except ValueError:
            raise _bad() from None
    return {"symbol": symbol.strip().upper(), "view": vid, "target": _number(target), "budget": _number(budget, positive=False),
            "expiration": day, "priority": _priority(priority)}


# ---- seams: what the tests replace -------------------------------------------------------------------------------------------------------

def _load_chain(desk_id: int, symbol: str, expiration: date | None) -> dict[str, Any]:
    """The chain of one expiration (the nearest one of at least 21 days when none is asked) with its time, or {"ok": False, ...} when the source cannot answer."""
    from hokma.market import options

    try:
        day = expiration
        if day is None:
            listed = [x for x in options.expirations(symbol, desk_id).get("expirations", []) if x.get("days", 0) >= DEFAULT_MIN_DAYS]
            if not listed:
                return {"ok": False, "source": t("desks.strategies.source_chain"), "code": "no_expiration", "reason": t("desks.strategies.no_expiration")}
            day = date.fromisoformat(listed[0]["date"])
        data = options.chain(symbol, [day.isoformat()], CHAIN_STRIKES, desk_id)
        group = (data.get("expirations") or [None])[0]
        spot = data.get("underlying_price")
        if group is None or not group.get("rows") or spot is None:
            return {"ok": False, "source": data.get("source") or t("desks.strategies.source_chain"), "code": "empty", "reason": t("desks.strategies.empty_chain")}
        return {"ok": True, "day": day, "spot": float(spot), "rows": group["rows"], "multiplier": float(group.get("multiplier") or options.DEFAULT_MULTIPLIER),
                "years": options.years_to_expiry(day), "days": group.get("days"), "rate": options.PROB_ITM_RATE, "source": data.get("source"),
                "price_feed": data.get("price_feed"), "greeks": data.get("greeks"), "as_of": data.get("as_of"), "market_open": data.get("market_open"),
                "delayed": data.get("delayed"), "notes": data.get("notes") or []}
    except options.OptionsError as exc:
        return {"ok": False, "source": t("desks.strategies.source_chain"), "code": exc.code, "reason": str(exc)}


def _switch(desk_id: int, symbol: str, db: Database | None) -> tuple[bool, str]:
    """Is premium selling open for the desk and the underlying, and the sentence for the owner when it is closed."""
    from hokma.desks.swo import premium_switch

    return premium_switch.selling_open(desk_id, symbol, db=db), premium_switch.owner_text(symbol)


def _shares_held(desk_id: int, symbol: str, db: Database | None) -> float | None:
    """Shares of the symbol in the account of the desk; None when the desk has no account that can say (then the structures that need shares stay closed)."""
    try:
        from hokma.broker import service as broker

        rows = broker.position_list(desk_id, db).get("positions", [])
    except Exception:                                           # noqa: BLE001  (no account, no session: the shares are not known)
        return None
    total = 0.0
    for p in rows:
        if str(p.get("symbol") or "").upper() == symbol and not p.get("put_call") and "OPTION" not in str(p.get("asset_type") or "").upper():
            total += float(p["long_quantity"]) if p.get("long_quantity") is not None else max(float(p.get("quantity") or 0.0), 0.0)
    return total


def _seat_active(desk_id: int, slug: str, db: Database | None) -> bool:
    """Does the desk carry the agent of the options seat, switched on?"""
    with (db or get_database()).read() as u:
        member = u.desks.find_agent(desk_id, slug)
    return member is not None and bool(getattr(member, "active", True))


def _llm(slug: str, desk_id: int) -> Any:
    from hokma.llm.factory import make_llm

    return make_llm(slug, desk_id=desk_id)


# ---- the verdicts -------------------------------------------------------------------------------------------------------------------------

NO_VERDICT = {
    "budget_exhausted": lambda: t("desks.strategies.reason.budget_exhausted"),
    "no_seat": lambda: t("desks.strategies.reason.no_seat"),
    "engine": lambda: t("desks.strategies.reason.engine"),
    "unreadable": lambda: t("desks.strategies.reason.unreadable"),
    "not_rated": lambda: t("desks.strategies.reason.not_rated"),
}
VERDICT_LABELS = {
    "recommended": lambda: t("desks.strategies.verdict.recommended"), "possible": lambda: t("desks.strategies.verdict.possible"),
    "not_worth": lambda: t("desks.strategies.verdict.not_worth"), "closed": lambda: t("desks.strategies.verdict.closed"),
    "only_if_shares": lambda: t("desks.strategies.verdict.only_if_shares"), "over_budget": lambda: t("desks.strategies.verdict.over_budget"),
}


def _usd(value: float | None) -> str:
    return "-" if value is None else f"{value:,.2f}"


def _none(code: str, detail: str = "") -> dict[str, Any]:
    reason = t("desks.strategies.no_verdict", reason=NO_VERDICT[code]() + (f" ({detail})" if detail else ""))
    return {"verdict": None, "label": None, "reason": reason, "backed_by": None, "code": code}


def _decide_fixed(card: dict, symbol: str, closed_text: str) -> dict[str, Any] | None:
    """The verdicts that the rules decide, not the model: closed by the switch, only with the shares, and over the budget."""
    if card["status"] == "locked":
        return {"verdict": "closed", "label": VERDICT_LABELS["closed"](), "reason": closed_text, "backed_by": {"source": "premium_switch"}}
    if card["status"] == "needs_shares":
        return {"verdict": "only_if_shares", "label": VERDICT_LABELS["only_if_shares"](), "reason": card["status_message"], "backed_by": {"source": "account"}}
    if card["status"] == "over_budget":
        return {"verdict": "over_budget", "label": VERDICT_LABELS["over_budget"](), "reason": card["status_message"], "backed_by": {"source": "budget"}}
    return None


def _status_message(card: dict, symbol: str, closed_text: str) -> str | None:
    if card["status"] == "locked":
        return closed_text
    if card["status"] == "needs_shares":
        return t("desks.strategies.needs_shares", shares=int(card["requires_shares"]), symbol=symbol)
    if card["status"] == "over_budget":
        if card["capital_required_usd"] is None:
            return t("desks.strategies.unbounded")
        return t("desks.strategies.over_budget", cost=_usd(card["capital_required_usd"]), budget=_usd(card["budget"]["budget_usd"]))
    return None


def _brief(card: dict) -> dict[str, Any]:
    """What the model reads of a card: the figures, not the payoff curve."""
    return {"id": card["id"], "structure": card["structure"], "family": card["family"], "class": card["premium_class"],
            "legs": [{"position": x["position"], "kind": x["kind"], "strike": x["strike"], "premium": round(x["premium"], 4)} for x in card["legs"]],
            "net_premium_usd": round(card["net_premium_usd"], 2), "capital_required_usd": card["capital_required_usd"], "fits_budget": card["budget"]["fits"],
            "max_profit_usd": card["max_profit_usd"], "max_loss_usd": card["max_loss_usd"], "unbounded_profit": card["unbounded_profit"],
            "unbounded_loss": card["unbounded_loss"], "breakevens": [round(b, 2) for b in card["breakevens"]],
            "p_profit": None if card["p_profit"] is None else round(card["p_profit"], 4),
            "pnl_at_target_usd": round(card["pnl_at_target_usd"], 2)}




def _record_usage(desk_id: int, slug: str, reply: Any, db: Database | None) -> None:
    """The tokens of the call, where the weekly budget reads them: one message of an archived ledger conversation of the desk project (the same table that
    DeskRepo.usage_total sums for the chat). Archived, so it never shows in the list of conversations. A failure to record never breaks the answer."""
    usage = getattr(reply, "usage", None) or {}
    tin, tout = int(usage.get("tokens_in") or 0), int(usage.get("tokens_out") or 0)
    if tin <= 0 and tout <= 0:
        return
    try:
        from hokma.engine.dbstore import ENGINE_NAME

        with (db or get_database()).write() as u:
            desk = u.desks.get(desk_id)
            ledger = next((c for c in u.conversations.list(project_id=desk.project_id, archived=True, include_ledger=True) if c.title == LEDGER_TITLE and c.agent_slug == slug), None)
            if ledger is None:
                ledger = u.conversations.create(engine=ENGINE_NAME, title=LEDGER_TITLE, project_id=desk.project_id, agent_slug=slug, project_mode="fixed")
                u.conversations.archive(ledger.id, True)
            u.messages.add(ledger.id, "assistant", LEDGER_TITLE, agent_slug=slug, model=str(getattr(reply, "model", "") or "") or None,
                           tokens_in=tin, tokens_out=tout)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("strategies panel usage was not recorded for desk %s: %s", desk_id, type(exc).__name__)


def _ask_model(desk_id: int, symbol: str, request: dict, result: dict, rated: list[dict], not_rated: list[dict], db: Database | None) -> dict[str, Any]:
    """One call to the options seat for all the cards. Returns {"reading", "by_id", "agent", "no_verdict"}: with a code in no_verdict when there is no answer."""
    from hokma.desks.plantel import SEATS
    from hokma.desks.swo.create import desk_agent_slug

    seat = SEATS[SEAT]
    slug = desk_agent_slug(seat, desk_id)
    try:
        if desk_budget.state(desk_id, db=db) == "exhausted":
            return {"no_verdict": ("budget_exhausted", "")}
        if not _seat_active(desk_id, slug, db):
            return {"no_verdict": ("no_seat", "")}
    except DeskError:
        raise
    except Exception as exc:                                    # noqa: BLE001
        return {"no_verdict": ("engine", type(exc).__name__)}
    payload = {"language": "Spanish" if language() == "es" else "English", "symbol": symbol, "view": request["view"], "priority": request["priority"],
               "target": result["target"], "budget_usd": request["budget"], "expiration": result["expiration"], "spot": result["spot"],
               "volatility_view": result["vol_view"], "expected_move": result["expected_move"],
               "to_rate": [_brief(c) for c in rated], "not_rated": [{"id": c["id"], "structure": c["structure"], "status": c["status"]} for c in not_rated]}
    try:
        client = _llm(slug, desk_id)
        reply = client.complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                                schema=SCHEMA, max_tokens=MAX_TOKENS)
        _record_usage(desk_id, slug, reply, db)                 # the answer was paid for even if it cannot be read
        data = reply.json()
    except Exception as exc:                                    # noqa: BLE001  (an engine that fails or an answer that is not JSON is "no verdict", not an error of the panel)
        code = "unreadable" if type(exc).__name__ == "LLMParseError" else "engine"
        return {"no_verdict": (code, str(getattr(exc, "code", "") or type(exc).__name__))}
    by_id: dict[str, dict] = {}
    for item in data.get("verdicts") or []:
        if isinstance(item, dict) and item.get("id") and item.get("verdict") in VERDICT_IDS:
            by_id[str(item["id"])] = {"verdict": item["verdict"], "reason": str(item.get("reason") or "")[:300]}
    try:
        desk_budget.check_alert(desk_id, db=db)
    except Exception:                                           # noqa: BLE001  (the warning of the budget never breaks the answer)
        pass
    reading = str(data.get("reading") or "").strip()[:800] or None
    return {"reading": reading, "by_id": by_id, "agent": {"seat": SEAT, "name": seat.name, "slug": slug, "model": str(getattr(reply, "model", "") or getattr(client, "model", ""))}}


_LAST: dict[int, dict[str, Any]] = {}


def remember(desk_id: int, request: dict[str, Any], result: dict[str, Any]) -> None:
    """The last consultation of each desk, in memory only (no table: the delivery creates none). The panel reads it when it opens, so what the chief
    asked by chat shows in the panel with its fields filled, and a tab switch does not lose the cards. A restart of Hokma forgets it."""
    _LAST[desk_id] = {"form": {"symbol": request["symbol"], "view": request["view"], "target": request["target"], "budget": request["budget"],
                               "expiration": request["expiration"].isoformat() if request["expiration"] else None, "priority": request["priority"]},
                      "result": result}


def last(desk_id: int) -> dict[str, Any]:
    return _LAST.get(desk_id) or {"form": None, "result": None}


def chat_view(result: dict[str, Any]) -> dict[str, Any]:
    """The answer for the chat: the figures of each card without the payoff curve and the chain (the panel has them)."""
    if not result.get("ok"):
        return {k: result.get(k) for k in ("ok", "symbol", "view", "message", "source", "reason")}
    cards = []
    for c in result["cards"]:
        v = c.get("verdict") or {}
        cards.append({"id": c["id"], "structure": c["structure"], "legs": [{"position": x["position"], "kind": x["kind"], "strike": x["strike"]} for x in c["legs"]],
                      "net_premium_usd": round(c["net_premium_usd"], 2), "max_profit_usd": c["max_profit_usd"], "max_loss_usd": c["max_loss_usd"],
                      "unbounded_profit": c["unbounded_profit"], "unbounded_loss": c["unbounded_loss"], "breakevens": [round(b, 2) for b in c["breakevens"]],
                      "p_profit": c["p_profit"], "expected_value_usd": c["expected_value_usd"], "status": c["status"], "status_message": c.get("status_message"),
                      "verdict": v.get("label") or v.get("reason"), "reason": v.get("reason"),
                      "backed_by": (v.get("backed_by") or {}).get("name") or (v.get("backed_by") or {}).get("source")})
    return {"ok": True, "symbol": result["symbol"], "view": result["view_label"], "expiration": result["expiration"], "spot": result["spot"],
            "volatility": t("desks.strategies.vol_uncalibrated") if result["vol_view_source"] == "default_flat_uncalibrated" else result["vol_view"],
            "reading": result["reading"], "cards": cards}


def suggest(desk_id: int, symbol: Any, view: Any, target: Any = None, budget: Any = None, expiration: Any = None, priority: Any = "balanced",
            *, db: Database | None = None) -> dict[str, Any]:
    """The cards of one view for one symbol, with their verdicts. Raises DeskError bad_strategy_args for a request that is not valid; a source that cannot
    answer is {"ok": False, "source", "reason"} ("Sin dato de {source}"), never a zero."""
    req = check_arguments(symbol, view, target, budget, expiration, priority)
    out = _suggest(desk_id, req, db)
    remember(desk_id, req, out)
    return out


def _suggest(desk_id: int, req: dict[str, Any], db: Database | None) -> dict[str, Any]:
    sym = req["symbol"]
    base = {"desk_id": desk_id, "symbol": sym, "view": req["view"], "view_label": quant.VIEW_LABELS[language() if language() in quant.VIEW_LABELS else "en"][req["view"]],
            "priority": req["priority"], "budget_usd": req["budget"], "target": req["target"]}
    chain = _load_chain(desk_id, sym, req["expiration"])
    if not chain["ok"]:
        return {**base, "ok": False, "source": chain["source"], "code": chain["code"], "reason": chain["reason"],
                "message": t("desks.strategies.no_data", source=chain["source"], reason=chain["reason"]), "cards": [], "skipped": [], "reading": None}
    selling_open, closed_text = _switch(desk_id, sym, db)
    def build(held: float | None):
        return quant.strategy_cards(chain["rows"], chain["spot"], req["view"], chain["day"].isoformat(), chain["years"], chain["multiplier"], req["target"],
                                    req["budget"], req["priority"], held, selling_open, chain["rate"], None)

    held = None
    res = build(held)
    if res.value is not None and any(x.get("requires_shares") for x in res.value["cards"]):
        held = _shares_held(desk_id, sym, db)               # the account is read only when a card needs the shares
        res = build(held)
    if res.value is None:
        return {**base, "ok": False, "source": chain["source"], "code": "unusable", "reason": "; ".join(res.warnings),
                "message": t("desks.strategies.no_data", source=chain["source"], reason="; ".join(res.warnings)), "cards": [], "skipped": [], "reading": None}
    value = res.value
    cards = value["cards"]
    for card in cards:
        card["status_message"] = _status_message(card, sym, closed_text)
        card["verdict"] = _decide_fixed(card, sym, closed_text)
    rated = [c for c in cards if card_needs_model(c)]
    not_rated = [c for c in cards if not card_needs_model(c)]
    model = {"used": False, "agent": None, "reason": None}
    reading = None
    if rated:
        asked = _ask_model(desk_id, sym, {**req, "view": req["view"]}, value, rated, not_rated, db)
        if "no_verdict" in asked:
            code, detail = asked["no_verdict"]
            for c in rated:
                c["verdict"] = _none(code, detail)
            model = {"used": False, "agent": None, "reason": code}
        else:
            reading = asked["reading"]
            model = {"used": True, "agent": asked["agent"], "reason": None}
            for c in rated:
                got = asked["by_id"].get(c["id"])
                c["verdict"] = ({"verdict": got["verdict"], "label": VERDICT_LABELS[got["verdict"]](), "reason": got["reason"], "backed_by": asked["agent"]}
                                if got else _none("not_rated"))
    skipped = [{**s, "message": t("desks.strategies.skipped", structure=s["structure"])} for s in value["skipped"]]
    return {**base, "ok": True, "expiration": value["expiration"], "days": chain["days"], "spot": value["spot"], "multiplier": value["multiplier"],
            "source": chain["source"], "price_feed": chain["price_feed"], "greeks": chain["greeks"], "as_of": chain["as_of"], "market_open": chain["market_open"],
            "delayed": chain["delayed"], "notes": chain["notes"], "vol_view": value["vol_view"], "vol_view_source": value["vol_view_source"],
            "expected_move": value["expected_move"], "families": value["families"], "chain": value["chain"],
            "premium_selling": {"open": selling_open, "reason": None if selling_open else closed_text}, "shares_held": held,
            "reading": reading, "model": model, "cards": cards, "skipped": skipped, "warnings": list(res.warnings)}


def card_needs_model(card: dict) -> bool:
    """Closed and needs-shares cards have their verdict from the rules; the model rates the rest."""
    return card["verdict"] is None
