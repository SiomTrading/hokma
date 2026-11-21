"""The door of Schwab orders (SPEC-015 18.2, 18.7 and 18.8): send, replace, cancel and preview. The ONLY module that names the
consumer `hokma_broker`, so it is the only one the `order_exceptions` of `kit/hardblock_brokers.json` let through
`schwab/http.py`. `endpoints.py` stays read-only (its test proves there is no write there); the four order routes live here.

These functions decide no policy: whether an order may leave (the switch of the broker, the state and the capital of the desk,
the preview rules of 18.3) is the broker service's job, which calls them. They do three things only:

1. Validate the `OrderRequest` locally against the schema of REF B.1 (every field, every enum, the nested `childOrderStrategies`
   and `replacingOrderCollection`, the 21 character OCC symbol of options) and raise `SchwabError(INVALID_PARAM)` with a clear
   detail BEFORE anything is sent. Hokma does not invent combinations: a valid schema that Schwab does not accept is
   `previewOrder`'s answer, not a local rule.
2. Translate the `account_ref` ("A1") to the hash of the path with `accounts.hash_for`. The hash, the account number and the
   token never leave: not in a log, not in an error, not in a return value.
3. Send through `http.request(..., consumer="hokma_broker")` and read the answer: the order id from the `Location` header of the
   201 (last segment), the preview as `models.PreviewOrder`.

Blocking: call from a thread. A `PreviewOrder` carries the `accountNumber` of its order strategy as Schwab sent it: pass it
through `accounts.public` before it leaves the backend, like every object of `models`.
"""
import datetime as dt
import json
import math
import re
from typing import Any

from hokma.schwab import accounts, http, models
from hokma.schwab.http import INVALID_PARAM, TRADER, SchwabError

CONSUMER = "hokma_broker"
PREVIEW_WRAPPED_FIRST = False        # SPEC-015 18.8 point 1: plain first, wrapped in {"orderStrategy": ...} after a 400; T0 fixes it
MAX_DEPTH = 6                        # TRIGGER > OCO > SINGLE is 3; the REF shows no deeper
MAX_ORDERS = 50                      # nodes of one request (children and replacing orders included)
PRICE_FIELDS = ("price", "stopPrice", "stopPriceOffset", "activationPrice")      # a number, or text as in REF 4 ("210.50")
_NUMBER_FIELDS = ("quantity", "filledQuantity", "remainingQuantity")
_ID = re.compile(r"[0-9]{1,19}")
_PRICE_TEXT = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,8})?")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:?\d{2})")
_SYMBOL = re.compile(r"[A-Za-z0-9.$/_^-]{1,20}")
_OCC_ROOT = re.compile(r"[A-Z0-9]{1,6} {0,5}")
_OCC_TAIL = re.compile(r"[0-9]{6}[CP][0-9]{8}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Fields of an OrderRequest (REF B.1) and how each is read. 'accountNumber' is refused on purpose (see _order).
_ENUM_FIELDS = {"session": "Session", "duration": "Duration", "orderType": "OrderTypeRequest",
                "complexOrderStrategyType": "ComplexOrderStrategyType", "stopPriceLinkBasis": "StopPriceLinkBasis",
                "stopPriceLinkType": "StopPriceLinkType", "stopType": "StopType", "priceLinkBasis": "PriceLinkBasis",
                "priceLinkType": "PriceLinkType", "taxLotMethod": "TaxLotMethod", "specialInstruction": "SpecialInstruction",
                "orderStrategyType": "OrderStrategyType", "status": "OrderStatus"}
_TIME_FIELDS = ("cancelTime", "releaseTime", "enteredTime", "closeTime")
_BOOL_FIELDS = ("cancelable", "editable")
_ORDER_KEYS = (set(_ENUM_FIELDS) | set(_TIME_FIELDS) | set(_BOOL_FIELDS) | set(PRICE_FIELDS) | set(_NUMBER_FIELDS)
               | {"destinationLinkName", "orderLegCollection", "orderId", "orderActivityCollection",
                  "replacingOrderCollection", "childOrderStrategies", "statusDescription"})
_RESPONSE_ONLY_ENUMS = ("UNKNOWN",)                    # an enum placeholder of the answers: never sent
_INSTRUMENT_BASE = {"assetType", "cusip", "symbol", "description", "instrumentId", "netChange"}
_INSTRUMENT_BY_TYPE = {"OPTION": {"optionDeliverables", "putCall", "optionMultiplier", "type", "underlyingSymbol"},
                       "CASH_EQUIVALENT": {"type"}, "FIXED_INCOME": {"maturityDate", "factor", "variableRate"}}
_LEG_KEYS = {"orderLegType", "legId", "instrument", "instruction", "positionEffect", "quantity", "quantityType",
             "divCapGains", "toSymbol"}


# ------------------------------------------------------------------------------------------------ validation

def _bad(detail: str) -> SchwabError:
    return SchwabError(INVALID_PARAM, 422, detail=detail)


def _enum(where: str, value: Any, enum: str) -> str:
    allowed = tuple(v for v in models.enum_values(enum) if v not in _RESPONSE_ONLY_ENUMS)
    if not isinstance(value, str) or value not in allowed:
        raise _bad(f"{where} must be one of {', '.join(allowed)}")
    return value


def _number(where: str, value: Any, *, text: bool = False, positive: bool = False, signed: bool = False) -> float:
    if isinstance(value, str) and text:
        if not _PRICE_TEXT.fullmatch(value):
            raise _bad(f"{where} must be a number (a decimal text like \"210.50\" is accepted)")
        number = float(value)
    elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _bad(f"{where} must be a number")
    else:
        number = float(value)
    if (number < 0 and not signed) or (positive and number == 0):
        raise _bad(f"{where} must be " + ("greater than zero" if positive else "zero or more"))
    return number


def _integer(where: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _bad(f"{where} must be a whole number")
    return value


def _flag(where: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _bad(f"{where} must be true or false")
    return value


def _short(where: str, value: Any, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or _CONTROL.search(value):
        raise _bad(f"{where} must be a short text")
    return value


def _keys(where: str, data: dict, allowed: set) -> None:
    for key in data:
        if not isinstance(key, str) or key not in allowed:
            shown = key[:40] if isinstance(key, str) and not _CONTROL.search(key) else "?"
            raise _bad(f"{where} has a field that is not in the order schema: {shown}")


def occ_symbol(root: str, expiry: dt.date, kind: str, strike: float) -> str:
    """The 21 character OCC symbol of REF 4: the root padded to 6 with spaces, `yymmdd`, `C` or `P`, the strike times 1000 in
    8 digits. A helper for callers and tests; `validate_order` checks the same shape."""
    if not _OCC_ROOT.fullmatch(root.ljust(6)) or kind not in ("C", "P") or not strike > 0:
        raise _bad("an option symbol needs a root of 1 to 6 capitals or digits, C or P and a strike above zero")
    return f"{root.ljust(6)}{expiry:%y%m%d}{kind}{round(strike * 1000):08d}"


def _occ(where: str, symbol: Any) -> str:
    if not isinstance(symbol, str) or len(symbol) != 21 or not _OCC_ROOT.fullmatch(symbol[:6]) \
            or not _OCC_TAIL.fullmatch(symbol[6:]):
        raise _bad(f"{where} must be a 21 character option symbol: root padded to 6 with spaces, yymmdd, C or P, "
                   "strike x 1000 in 8 digits")
    try:
        dt.datetime.strptime(symbol[6:12], "%y%m%d")
    except ValueError:
        raise _bad(f"{where} has a date that does not exist (yymmdd)") from None
    if int(symbol[13:]) == 0:
        raise _bad(f"{where} has a strike of zero")
    return symbol


def _deliverables(where: str, value: Any) -> None:
    if not isinstance(value, list):
        raise _bad(f"{where} must be a list")
    for index, item in enumerate(value):
        spot = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise _bad(f"{spot} must be an object")
        _keys(spot, item, {"symbol", "deliverableUnits", "apiCurrencyType", "assetType"})
        if "symbol" in item:
            _short(spot + ".symbol", item["symbol"], 40)
        if "deliverableUnits" in item:
            _number(spot + ".deliverableUnits", item["deliverableUnits"])
        if "apiCurrencyType" in item:
            _enum(spot + ".apiCurrencyType", item["apiCurrencyType"], "ApiCurrencyType")
        if "assetType" in item:
            _enum(spot + ".assetType", item["assetType"], "AssetType")


def _instrument(where: str, data: Any) -> None:
    if not isinstance(data, dict):
        raise _bad(f"{where} must be an object")
    kind = _enum(where + ".assetType", data.get("assetType"), "AccountAssetType")       # required by the schema
    _keys(where, data, _INSTRUMENT_BASE | _INSTRUMENT_BY_TYPE.get(kind, set()))
    if "symbol" not in data and "cusip" not in data:
        raise _bad(f"{where}.symbol is required")
    if "symbol" in data:
        if kind == "OPTION":
            _occ(where + ".symbol", data["symbol"])
        elif not isinstance(data["symbol"], str) or not _SYMBOL.fullmatch(data["symbol"]):
            raise _bad(f"{where}.symbol must be a symbol of letters, digits and . $ / _ ^ - (an option is assetType OPTION)")
    for key in ("cusip", "description"):
        if key in data:
            _short(f"{where}.{key}", data[key], 200)
    if "instrumentId" in data:
        _integer(where + ".instrumentId", data["instrumentId"])
    if "netChange" in data:
        _number(where + ".netChange", data["netChange"], signed=True)
    if "putCall" in data:
        _enum(where + ".putCall", data["putCall"], "PutCall")
    if "optionMultiplier" in data:
        _integer(where + ".optionMultiplier", data["optionMultiplier"])
    if "underlyingSymbol" in data:
        _short(where + ".underlyingSymbol", data["underlyingSymbol"], 40)
    if "optionDeliverables" in data:
        _deliverables(where + ".optionDeliverables", data["optionDeliverables"])
    if "type" in data:
        _enum(where + ".type", data["type"], "OptionType" if kind == "OPTION" else "CashEquivalentType")
    if "maturityDate" in data and (not isinstance(data["maturityDate"], str) or not _TIME.fullmatch(data["maturityDate"])):
        raise _bad(f"{where}.maturityDate must be a date-time (yyyy-MM-dd'T'HH:mm:ss.SSSZ)")
    for key in ("factor", "variableRate"):
        if key in data:
            _number(f"{where}.{key}", data[key])


def _leg(where: str, data: Any) -> None:
    if not isinstance(data, dict):
        raise _bad(f"{where} must be an object")
    _keys(where, data, _LEG_KEYS)
    _enum(where + ".instruction", data.get("instruction"), "Instruction")                 # a leg without one is no leg
    _instrument(where + ".instrument", data.get("instrument"))
    if "orderLegType" in data:
        _enum(where + ".orderLegType", data["orderLegType"], "OrderLegType")
    if "legId" in data:
        _integer(where + ".legId", data["legId"])
    if "positionEffect" in data:
        _enum(where + ".positionEffect", data["positionEffect"], "PositionEffect")
    if "quantityType" in data:
        _enum(where + ".quantityType", data["quantityType"], "QuantityType")
    if "divCapGains" in data:
        _enum(where + ".divCapGains", data["divCapGains"], "DivCapGains")
    if "toSymbol" in data:
        _short(where + ".toSymbol", data["toSymbol"], 40)
    if "quantity" in data:
        _number(where + ".quantity", data["quantity"], positive=True)
    elif data.get("quantityType") != "ALL_SHARES":
        raise _bad(f"{where}.quantity is required (or quantityType ALL_SHARES)")


def _order(where: str, data: Any, depth: int, count: list[int]) -> None:
    count[0] += 1
    if depth > MAX_DEPTH or count[0] > MAX_ORDERS:
        raise _bad("the order has too many nested orders")
    if not isinstance(data, dict):
        raise _bad(f"{where} must be an object")
    if "accountNumber" in data:
        raise _bad(f"{where}.accountNumber is not accepted: the account is the account_ref of the call")
    _keys(where, data, _ORDER_KEYS)
    kind = _enum(where + ".orderStrategyType", data.get("orderStrategyType"), "OrderStrategyType")
    for key, enum in _ENUM_FIELDS.items():
        if key in data and key != "orderStrategyType":
            _enum(f"{where}.{key}", data[key], enum)
    for key in _TIME_FIELDS:
        if key in data and (not isinstance(data[key], str) or not _TIME.fullmatch(data[key])):
            raise _bad(f"{where}.{key} must be a date-time (yyyy-MM-dd'T'HH:mm:ss.SSSZ)")
    for key in _BOOL_FIELDS:
        if key in data:
            _flag(f"{where}.{key}", data[key])
    for key in PRICE_FIELDS:
        if key in data:
            _number(f"{where}.{key}", data[key], text=True)
    for key in _NUMBER_FIELDS:
        if key in data:
            _number(f"{where}.{key}", data[key], positive=key == "quantity")
    if "orderId" in data:
        _integer(where + ".orderId", data["orderId"])
    if "destinationLinkName" in data:
        _short(where + ".destinationLinkName", data["destinationLinkName"], 100)
    if "statusDescription" in data:
        _short(where + ".statusDescription", data["statusDescription"], 500)
    if "orderActivityCollection" in data and not (isinstance(data["orderActivityCollection"], list)
                                                  and all(isinstance(x, dict) for x in data["orderActivityCollection"])):
        raise _bad(where + ".orderActivityCollection must be a list of objects")
    legs = data.get("orderLegCollection")
    if legs is not None:
        if not isinstance(legs, list) or not legs:
            raise _bad(f"{where}.orderLegCollection must be a list with at least one leg")
        for index, leg in enumerate(legs):
            _leg(f"{where}.orderLegCollection[{index}]", leg)
    if kind in ("SINGLE", "TRIGGER"):
        for key in ("orderType", "session", "duration", "orderLegCollection"):
            if key not in data:
                raise _bad(f"{where}.{key} is required for orderStrategyType {kind}")
    for key in ("childOrderStrategies", "replacingOrderCollection"):
        if key in data:
            if not isinstance(data[key], list) or not data[key]:
                raise _bad(f"{where}.{key} must be a list with at least one order")
            for index, child in enumerate(data[key]):
                _order(f"{where}.{key}[{index}]", child, depth + 1, count)
    if kind in ("OCO", "TRIGGER") and "childOrderStrategies" not in data:
        raise _bad(f"{where}.childOrderStrategies is required for orderStrategyType {kind}")


def validate_order(order: Any) -> dict:
    """The `OrderRequest` of REF B.1 checked field by field. Returns it unchanged (the body is sent as given: a price as
    text stays text, REF 4); raises `SchwabError(INVALID_PARAM, 422)` with the path of the first problem, before any call."""
    _order("order", order, 0, [0])
    try:
        json.dumps(order, allow_nan=False)
    except (TypeError, ValueError):
        raise _bad("order must be made of JSON values only") from None
    return order


def _order_id(value: Any) -> str:
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(text, str) or not _ID.fullmatch(text):
        raise _bad("order_id must be a whole number")
    return text


def _body(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")


_JSON = {"Content-Type": "application/json"}


# ------------------------------------------------------------------------------------------------ the routes

OUTCOME_UNKNOWN = "the outcome is unknown: read the orders of the account before sending it again"
UNKNOWN_CODES = (http.UNREACHABLE, http.UPSTREAM)      # no answer, or a 5xx (or an unreadable 2xx): the order may exist


def _sent(method: str, path: str, body: bytes | None, *, outcome_unknown: bool) -> http.Reply:
    """One order call through the order list of the door, ONE attempt: nothing here or above retries a POST or a PUT.

    A network failure, or a 5xx of Schwab (bug 1224), after the request left leaves the outcome unknown for an order that is
    sent or replaced: Schwab may have created it before failing. The error says so (the same notice for both), so the agent reads
    `order_list` before sending the same order again. A 4xx is a refusal and the order does not exist.

    The only repeat in the path is the one of `http.request` after a 401: the answer comes from the authentication layer, before
    an order is read, so renewing the token and sending once more cannot make a second order; a second 401 is an error."""
    try:
        return http.request(method, path, None, body=body, headers=_JSON if body is not None else None,
                            consumer=CONSUMER, reply=True)
    except SchwabError as error:
        if outcome_unknown and error.code in UNKNOWN_CODES:
            error.detail = OUTCOME_UNKNOWN
        raise


def _created(reply: http.Reply) -> dict:
    """{order_id, correl_id} of a 201: the id is the last segment of `Location`. A 201 without a readable id is still an order
    that exists: `order_id` is None and the caller reads the orders of the account (never an error that invites a resend)."""
    tail = (reply.location or "").rstrip("/").rsplit("/", 1)[-1]
    return {"order_id": int(tail) if _ID.fullmatch(tail) else None, "correl_id": reply.correl_id}


def place_order(account_ref: str, order: dict) -> dict:
    """POST /trader/v1/accounts/{accountNumber}/orders (REF 3.2) with the whole `OrderRequest`. 201 without body: returns
    {order_id (int, from `Location`), correl_id}."""
    validate_order(order)
    path = f"{TRADER}/accounts/{accounts.hash_for(account_ref)}/orders"
    return _created(_sent("POST", path, _body(order), outcome_unknown=True))


def replace_order(account_ref: str, order_id: Any, order: dict) -> dict:
    """PUT /trader/v1/accounts/{accountNumber}/orders/{orderId} (REF 3.2): 201 with `Location` (Schwab gives the replacement a
    new id). Returns {order_id (the new one), correl_id}."""
    validate_order(order)
    number = _order_id(order_id)
    path = f"{TRADER}/accounts/{accounts.hash_for(account_ref)}/orders/{number}"
    return _created(_sent("PUT", path, _body(order), outcome_unknown=True))


def cancel_order(account_ref: str, order_id: Any) -> dict:
    """DELETE /trader/v1/accounts/{accountNumber}/orders/{orderId} (REF 3.2): 200 without body. Returns {order_id, correl_id}."""
    number = _order_id(order_id)
    path = f"{TRADER}/accounts/{accounts.hash_for(account_ref)}/orders/{number}"
    reply = _sent("DELETE", path, None, outcome_unknown=False)
    return {"order_id": int(number), "correl_id": reply.correl_id}


def preview_order(account_ref: str, order: dict) -> models.PreviewOrder:
    """POST /trader/v1/accounts/{accountNumber}/previewOrder (REF 3.2, B.2). Sends the `OrderRequest` as it is and, if Schwab
    answers 400, once more wrapped as {"orderStrategy": ...} (18.8 point 1; `PREVIEW_WRAPPED_FIRST` swaps the two). Returns
    the `PreviewOrder`; nothing is placed."""
    validate_order(order)
    path = f"{TRADER}/accounts/{accounts.hash_for(account_ref)}/previewOrder"
    forms = [{"orderStrategy": order}, order] if PREVIEW_WRAPPED_FIRST else [order, {"orderStrategy": order}]
    for index, form in enumerate(forms):
        try:
            reply = _sent("POST", path, _body(form), outcome_unknown=False)
            break
        except SchwabError as error:
            if error.code != http.BAD_REQUEST or error.status != 400 or index == len(forms) - 1:
                raise
    preview = models.PreviewOrder.read(reply.body)
    if preview is None:
        raise SchwabError(http.UPSTREAM, 502, correl_id=reply.correl_id)
    return preview
