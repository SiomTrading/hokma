"""HTTP surface of brokers and accounts (task 3294, C7-5c). Every route needs the per-boot session token. Errors carry a
stable `code` and a message in the student's language. A reset needs {"confirmed": true}."""
from typing import Any, Callable

from fastapi import APIRouter, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel

from hokma.broker import account_rules, day_close, desk_accounts, desk_rules, service
from hokma.core.origin import token_matches
from hokma.db import DbError
from hokma.desks.api import _error
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN


class TokenRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            candidate = request.headers.get("x-hokma-token") or request.query_params.get("token") or ""
            if not token_matches(candidate, SESSION_TOKEN):
                return _error(401, "bad_token", t("desks.err.bad_token"))
            try:
                return await original(request)
            except service.BrokerError as exc:
                return _error(exc.status, exc.code, exc.message)
            except DbError:
                return _error(400, "db_error", t("desks.err.db_failed"))

        return handler


router = APIRouter(route_class=TokenRoute)


class NewBroker(BaseModel):
    name: Any
    commission: Any = None
    slippage_bps: Any = None


class Costs(BaseModel):
    commission: Any = None
    slippage_bps: Any = None


class NewAccount(BaseModel):
    name: Any
    capital: Any


class Reset(BaseModel):
    confirmed: Any = None


class OrdersEnabled(BaseModel):
    enabled: Any = None


class AddAccount(BaseModel):
    account_id: Any = None                    # an account that exists, or
    demo: Any = None                          # {"name": ..., "initial_capital": ...}: a new demo account


class BindAccount(BaseModel):
    account_id: Any


class OrderBody(BaseModel):
    order: Any = None
    reason: Any = None


class CancelBody(BaseModel):
    reason: Any = None


class LimitsBody(BaseModel):
    limits: Any = None


@router.get("/api/brokers")
def list_brokers() -> dict:
    return {"brokers": service.list_brokers()}


@router.post("/api/brokers", status_code=201)
def create_broker(body: NewBroker) -> dict:
    return service.create_broker(body.name, body.commission, body.slippage_bps)


@router.put("/api/brokers/{broker_id}/costs")
def update_costs(broker_id: int, body: Costs) -> dict:
    return service.update_costs(broker_id, body.commission, body.slippage_bps)


@router.put("/api/brokers/{broker_id}/orders-enabled")
def set_orders_enabled(broker_id: int, body: OrdersEnabled) -> dict:
    """The switch of the orders of a broker that takes them (18.2 point 13). 422 for a broker that cannot."""
    return service.set_orders_enabled(broker_id, body.enabled)


@router.get("/api/brokers/{broker_id}/accounts")
def list_accounts(broker_id: int) -> dict:
    return {"accounts": service.list_accounts(broker_id)}


@router.post("/api/brokers/{broker_id}/accounts", status_code=201)
def create_account(broker_id: int, body: NewAccount) -> dict:
    return service.create_account(broker_id, body.name, body.capital)


@router.get("/api/accounts/pnl")
def accounts_pnl() -> dict:
    """The P&L of the day each account has from its BROKER (SPEC-026 v2, 4.1), from the last reads, with no call to the broker. By account id:
    {state: ok | no_data | pending | unavailable, source, since, last: {day_pnl, equity_now, equity_start, positions[], as_of}}."""
    from hokma.broker import pnl

    return {"accounts": {str(k): v for k, v in pnl.views().items()}}


@router.post("/api/alpaca/account-sample")
def alpaca_account_sample() -> dict:
    """One read of the account and the positions of Alpaca (two calls), anonymized and saved in the downloads folder of the owner (SPEC-026 v2, 3390e1b).
    The account number and the account id are "XXXX" in the file; the amounts are intact. It never sends an order."""
    from hokma.broker import alpaca_sample

    return alpaca_sample.capture()


@router.get("/api/accounts/{account_id}")
def get_account(account_id: int) -> dict:
    return service.get_account(account_id)


@router.post("/api/accounts/{account_id}/reset")
def reset_account(account_id: int, body: Reset) -> dict:
    return service.reset_account(account_id, body.confirmed)


@router.post("/api/desks/{desk_id}/accounts", status_code=201)
def add_account(desk_id: int, body: AddAccount) -> dict:
    """Add an account to a desk (SPEC-027): `{account_id}` for one that exists (it may serve other desks too) or `{demo: {name,
    initial_capital}}` for a new demo account, created and added in one step. The first account also becomes the account of the
    principal strategy."""
    if (body.account_id is None) == (body.demo is None):
        raise service.BrokerError("invalid", 400)
    if body.demo is None:
        return desk_accounts.add_account(desk_id, body.account_id)
    if not isinstance(body.demo, dict):
        raise service.BrokerError("invalid", 400)
    return desk_accounts.add_demo_account(desk_id, body.demo.get("name"), body.demo.get("initial_capital"))


@router.delete("/api/desks/{desk_id}/accounts/{account_id}")
def remove_account(desk_id: int, account_id: int) -> dict:
    """409 `account_in_use_by_strategy` while a strategy of the desk uses the account; 409 `working_orders` while the desk has orders alive in it."""
    return desk_accounts.remove_account(desk_id, account_id)


@router.put("/api/desks/{desk_id}/strategies/{strategy_key}/account")
def bind_strategy(desk_id: int, strategy_key: str, body: BindAccount) -> dict:
    return desk_accounts.bind_strategy(desk_id, strategy_key, body.account_id)


# ---- orders of a desk (SPEC-015 18.2 to 18.6): the same service as the tools of the chat. The app is the owner, so a soft
# condition of 18.3 does not ask again; the refusals (switch off, desk paused, Schwab rejects) still apply. No route returns
# an account number or a hash: an account is its account_ref and mask.

OWNER = service.owner_who()      # holds the start secret of the app (generated in memory, never saved): see service.Who


@router.get("/api/desks/{desk_id}/orders")
def list_orders(desk_id: int, status: str | None = None, fromEnteredTime: str | None = None,  # noqa: N803
                toEnteredTime: str | None = None, maxResults: int | None = Query(default=None)) -> dict:  # noqa: N803
    return service.order_list(desk_id, status, fromEnteredTime, toEnteredTime, maxResults)


@router.get("/api/desks/{desk_id}/orders/log")
def order_log(desk_id: int, since: str | None = None, limit: int = 50) -> dict:
    return service.order_log(desk_id, since, limit)


@router.get("/api/desks/{desk_id}/orders/pending")
def pending_orders(desk_id: int) -> dict:
    """The orders that wait for the owner (18.3), with the decision id to approve or reject."""
    return {"pending": service.list_pending(desk_id)}


@router.post("/api/desks/{desk_id}/orders/pending/{decision_id}/approve")
def approve_pending(desk_id: int, decision_id: int) -> dict:
    """Preview again, apply the rules with the prices of now and send it when it passes; past 30 minutes it only expires."""
    return service.approve_pending(decision_id, desk_id=desk_id, who=OWNER)


class NoticeFollow(BaseModel):
    scope: str          # "order" (only this order) or "day" (the rest of the day: a temporary permission that ends with the session)


class CloseConfirm(BaseModel):
    plan_token: str     # the token of the list the first step showed


@router.get("/api/orders/notices")
def pending_notices() -> dict:
    """The orders that wait for the owner because a rule in notice mode tripped, of every desk: what the cards of the chat show."""
    from hokma.exec import notices
    return {"notices": notices.pending_cards()}


@router.post("/api/orders/notices/{proposal_id}/follow")
def follow_notice(proposal_id: int, body: NoticeFollow) -> dict:
    """Follow: the owner chose the scope (asked every time). The order goes out as it was approved; "day" also records the permission of the day."""
    from hokma.exec import approvals
    if body.scope not in ("order", "day"):
        raise service.BrokerError("scope_required", 409, text="scope is order or day")
    return service._exec_answer(approvals.approve(proposal_id, who=OWNER, scope=body.scope), proposal_id)


@router.post("/api/orders/notices/{proposal_id}/no")
def refuse_notice(proposal_id: int) -> dict:
    from hokma.exec import approvals
    return service._exec_answer(approvals.reject(proposal_id, who=OWNER), proposal_id)


@router.post("/api/orders/notices/{proposal_id}/close")
def close_notice(proposal_id: int) -> dict:
    """Close, first step: NOTHING is closed. It answers with the list of what would be closed and what stays (positions and desks); the second route confirms."""
    from hokma.exec import approvals
    asked = approvals.close_ask(proposal_id, who=OWNER)
    return {"status": "close_confirm", "proposal_id": proposal_id, "plan": asked["plan"], "plan_token": asked["plan_token"], "text": asked["text"]}


@router.post("/api/orders/notices/{proposal_id}/close/confirm")
def confirm_close_notice(proposal_id: int, body: CloseConfirm) -> dict:
    """Close, second step: the order is rejected and the positions of the list that was shown are closed, and no others."""
    from hokma.exec import approvals
    return {"status": approvals.close_account(proposal_id, who=OWNER, plan=body.plan_token), "proposal_id": proposal_id}


@router.get("/api/orders/cap-notices")
def pending_cap_notices() -> dict:
    """The notices of a cap with no order in the way that wait for the owner (a rule in notice mode reached its cap): what the cards of the chat show (SPEC-026, 3390e4)."""
    from hokma.exec import cap_notices
    return {"notices": cap_notices.pending_cards()}


@router.post("/api/orders/cap-notices/{cap_id}/follow")
def follow_cap_notice(cap_id: int) -> dict:
    """Keep trading today: the permission of the day for that rule and account; it does not warn again that session unless the cap is a higher one."""
    from hokma.exec import cap_notices
    return cap_notices.follow(cap_id, who=OWNER)


@router.post("/api/orders/cap-notices/{cap_id}/block")
def block_cap_notice(cap_id: int) -> dict:
    """Block entries today: the session becomes a restriction that only blocks entries. Closes are never blocked."""
    from hokma.exec import cap_notices
    return cap_notices.block(cap_id, who=OWNER)


@router.post("/api/orders/cap-notices/{cap_id}/close")
def close_cap_notice(cap_id: int) -> dict:
    """Close positions, first step: NOTHING is closed. It answers with the list of what would be closed and what stays; the second route confirms."""
    from hokma.exec import cap_notices
    asked = cap_notices.close_ask(cap_id, who=OWNER)
    return {"status": "close_confirm", "cap_id": cap_id, "plan": asked["plan"], "plan_token": asked["plan_token"], "text": asked["text"]}


@router.post("/api/orders/cap-notices/{cap_id}/close/confirm")
def confirm_close_cap_notice(cap_id: int, body: CloseConfirm) -> dict:
    """Close positions, second step: the positions of the list that was shown are closed, and no others; if the book changed, nothing is closed."""
    from hokma.exec import cap_notices
    return {"status": cap_notices.close_confirm(cap_id, who=OWNER, plan=body.plan_token), "cap_id": cap_id}


@router.post("/api/orders/cap-notices/{cap_id}/back")
def back_cap_notice(cap_id: int) -> dict:
    """The owner did not confirm the list: the notice goes back to its three answers and nothing was closed."""
    from hokma.exec import cap_notices
    return cap_notices.back(cap_id, who=OWNER)


@router.post("/api/desks/{desk_id}/orders/pending/{decision_id}/reject")
def reject_pending(desk_id: int, decision_id: int) -> dict:
    return service.reject_pending(decision_id, desk_id=desk_id, who=OWNER)


@router.post("/api/desks/{desk_id}/orders/preview")
def preview_order(desk_id: int, body: OrderBody) -> dict:
    return service.preview(desk_id, body.order, OWNER)


@router.post("/api/desks/{desk_id}/orders")
def place_order(desk_id: int, body: OrderBody) -> dict:
    return service.place(desk_id, body.order, body.reason, OWNER)


@router.get("/api/desks/{desk_id}/orders/{order_id}")
def order_status(desk_id: int, order_id: str) -> dict:
    return service.order_status(desk_id, order_id)


@router.put("/api/desks/{desk_id}/orders/{order_id}")
def replace_order(desk_id: int, order_id: str, body: OrderBody) -> dict:
    return service.replace(desk_id, order_id, body.order, body.reason, OWNER)


@router.post("/api/desks/{desk_id}/orders/{order_id}/cancel")
def cancel_order(desk_id: int, order_id: str, body: CancelBody) -> dict:
    return service.cancel(desk_id, order_id, body.reason, OWNER)


@router.get("/api/desks/{desk_id}/positions")
def list_positions(desk_id: int) -> dict:
    return service.position_list(desk_id)


@router.get("/api/desks/{desk_id}/limits")
def get_limits(desk_id: int) -> dict:
    """The optional limits of a desk (18.5); empty means all off."""
    return {"limits": service.get_limits(desk_id)}


@router.put("/api/desks/{desk_id}/limits")
def set_limits(desk_id: int, body: LimitsBody) -> dict:
    """Only the given keys change; a null turns that limit off."""
    return {"limits": service.set_limits(desk_id, body.limits)}


# ---- the rules of a desk (SPEC-025): controls the guards apply and instructions the chief reads. The app is the owner.

class RuleBody(BaseModel):
    kind: str = "control"                      # "control" or "instruction"
    control_id: str | None = None
    value: Any = None
    match: str | None = None
    text: str | None = None
    source: str = "hand"
    suggested_id: str | None = None
    mode: str | None = None                    # SPEC-026: `restriction` or `notice`, asked of the owner; a new control is not saved without it
    on_trip: str | None = None
    timeout_min: int | None = None
    scope: str = "desk"                        # `desk` (all the accounts of the desk), `account` (the whole account) or `desk_account` (this desk in one account)
    account_id: int | None = None


class RuleEdit(BaseModel):
    value: Any = None
    match: str | None = None
    text: str | None = None
    mode: str | None = None
    on_trip: str | None = None
    timeout_min: int | None = None


class ModeBody(BaseModel):
    mode: str
    on_trip: str | None = None
    timeout_min: int | None = None


class LiftBody(BaseModel):
    confirm: bool = False


class StartBody(BaseModel):
    equity: Any = None
    confirm: bool = False


class ApproveBody(BaseModel):
    confirm_loosen: bool = False


class Dismiss(BaseModel):
    suggested_id: str


@router.get("/api/desks/{desk_id}/rules")
def get_rules(desk_id: int) -> dict:
    """The catalog, the active rules, the suggestions and the proposals of the chief."""
    return desk_rules.view(desk_id)


@router.post("/api/desks/{desk_id}/rules", status_code=201)
def add_rule(desk_id: int, body: RuleBody) -> dict:
    if body.kind == "instruction":
        return desk_rules.add_instruction(desk_id, body.text, OWNER, source=body.source, suggested_id=body.suggested_id)
    if body.scope in ("account", "desk_account"):
        if body.account_id is None:
            raise service.BrokerError("bad_limits", 400, text="a rule of an account needs account_id")
        return account_rules.set_rule(desk_id, body.account_id, body.control_id or "", body.value, OWNER, mode=body.mode, on_trip=body.on_trip,
                                      timeout_min=body.timeout_min, only_desk=body.scope == "desk_account", source=body.source)
    return desk_rules.set_control(desk_id, body.control_id or "", body.value, OWNER, match=body.match, source=body.source, mode=body.mode,
                                  on_trip=body.on_trip, timeout_min=body.timeout_min)


@router.put("/api/desks/{desk_id}/rules/{rule_id}")
def edit_rule(desk_id: int, rule_id: str, body: RuleEdit) -> dict:
    if body.text is not None:
        return desk_rules.edit_instruction(desk_id, rule_id, body.text, OWNER)
    if rule_id.startswith("a") and rule_id[1:2].isdigit():
        return account_rules.edit_rule(desk_id, rule_id, OWNER, value=body.value, mode=body.mode, on_trip=body.on_trip, timeout_min=body.timeout_min)
    head, _, word = rule_id.partition(":")
    if body.value is None and body.mode is not None:                       # only the mode changes
        return desk_rules.set_mode(desk_id, rule_id, OWNER, mode=body.mode, on_trip=body.on_trip, timeout_min=body.timeout_min)
    return desk_rules.set_control(desk_id, head, body.value, OWNER, match=body.match or word or None, mode=body.mode, on_trip=body.on_trip,
                                  timeout_min=body.timeout_min)


@router.delete("/api/desks/{desk_id}/rules/{rule_id}")
def remove_rule(desk_id: int, rule_id: str) -> dict:
    if rule_id.startswith("a") and rule_id[1:2].isdigit():                   # a rule of an account: a12:daily_loss or a12:d3:daily_loss
        return account_rules.remove_rule(desk_id, rule_id, OWNER)
    return desk_rules.remove_rule(desk_id, rule_id, OWNER)


@router.post("/api/accounts/{account_id}/day-close/lift")
def lift_day_close(account_id: int, body: LiftBody) -> dict:
    """The owner opens an account that a loss rule closed for the day. Without `confirm` true it answers 409 with what closed it."""
    return day_close.lift(account_id, OWNER, confirm=body.confirm)


@router.get("/api/accounts/{account_id}/day-close")
def get_day_close(account_id: int) -> dict:
    with service._db(None).read() as u:
        return {**day_close.state_of(u, account_id), "events": day_close.events(account_id)}


@router.get("/api/accounts/{account_id}/session-start")
def get_session_start(account_id: int) -> dict:
    """Whether the loss of the day of the account is measured, why not, and the equity the owner declared (SPEC-026, 3390c2)."""
    from hokma.exec import live

    return live.start_state(account_id)


@router.post("/api/accounts/{account_id}/session-start")
def declare_session_start(account_id: int, body: StartBody) -> dict:
    """The owner declares the equity the account started its session with when a carried position left it unmeasured. Without `confirm` true it answers
    409 `equity_confirm`; it is refused when the start is already measured. Only the owner; recorded with who and when."""
    from hokma.exec import live

    return live.declare_start(account_id, body.equity, OWNER, confirm=body.confirm)


@router.post("/api/desks/{desk_id}/rules-dismiss")
def dismiss_rule(desk_id: int, body: Dismiss) -> dict:
    return desk_rules.dismiss(desk_id, body.suggested_id, OWNER)


@router.post("/api/desks/{desk_id}/rules-proposals/{proposal_id}/approve")
def approve_rule(desk_id: int, proposal_id: str, body: ApproveBody | None = None) -> dict:
    return desk_rules.approve(desk_id, proposal_id, OWNER, confirm_loosen=bool(body and body.confirm_loosen))


@router.post("/api/desks/{desk_id}/rules-proposals/{proposal_id}/reject")
def reject_rule(desk_id: int, proposal_id: str) -> dict:
    return desk_rules.reject(desk_id, proposal_id, OWNER)
