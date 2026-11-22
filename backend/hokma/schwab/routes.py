"""The internal API of the Schwab read endpoints (SPEC-015 5.10), prefix /api/schwab. Every route needs the per-boot
session token (the X-Hokma-Token header only, never the address) and sits behind the same Host and Origin guard as the rest of the API, so no
agent can reach it. All of them are GET: there is no route that places, changes or cancels an order.

Market Data (REF 2), the same parameters as the Schwab endpoint:

    GET /api/schwab/marketdata/quotes?symbols=&fields=&indicative=
    GET /api/schwab/marketdata/{symbol}/quotes?fields=
    GET /api/schwab/marketdata/chains?symbol=&contractType=&strikeCount=&includeUnderlyingQuote=&strategy=&interval=
        &strike=&range=&fromDate=&toDate=&volatility=&underlyingPrice=&interestRate=&daysToExpiration=&expMonth=
        &optionType=&entitlement=
    GET /api/schwab/marketdata/expirationchain?symbol=
    GET /api/schwab/marketdata/pricehistory?symbol=&periodType=&period=&frequencyType=&frequency=&startDate=&endDate=
        &needExtendedHoursData=&needPreviousClose=
    GET /api/schwab/marketdata/movers/{index}?sort=&frequency=
    GET /api/schwab/marketdata/markets?markets=&date=          GET /api/schwab/marketdata/markets/{market_id}?date=
    GET /api/schwab/marketdata/instruments?symbol=&projection=  GET /api/schwab/marketdata/instruments/{cusip}

Accounts and Trading (REF 3), read only; an account is ONLY its account_ref ("A1", "A2"...), never a number or a hash:

    GET /api/schwab/accounts/accountNumbers                    [{account_ref, mask}]
    GET /api/schwab/accounts?fields=                           GET /api/schwab/accounts/{account_ref}?fields=
    GET /api/schwab/orders?fromEnteredTime=&toEnteredTime=&status=&maxResults=
    GET /api/schwab/accounts/{account_ref}/orders?(the same)   GET /api/schwab/accounts/{account_ref}/orders/{order_id}
    GET /api/schwab/accounts/{account_ref}/transactions?startDate=&endDate=&types=&symbol=
    GET /api/schwab/accounts/{account_ref}/transactions/{transaction_id}
    GET /api/schwab/userPreference

Calculations (16): GET /api/schwab/analytics/{name} with name one of reader.ANALYTICS_READS (volatility_history, portfolio_greeks,
exposure_and_pnl, account_activity, market_hours).

Every answer is {"source": "Schwab", "as_of", "delayed"?, "data"}. Errors are {"detail": {"code", "message"}} with the
catalog text in the active language; without a session the code is schwab.err.not_connected (409).
"""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from hokma.core.origin import token_matches
from hokma.rpc.ws import SESSION_TOKEN
from hokma.schwab import reader
from hokma.schwab.http import INVALID_PARAM, SchwabError

log = logging.getLogger("hokma.schwab")
router = APIRouter(prefix="/api/schwab")

# (path under /api/schwab, read name, path parameter -> parameter of the read)
ROUTES: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("/marketdata/quotes", "quotes", {}),
    ("/marketdata/{symbol}/quotes", "quote", {"symbol": "symbol"}),
    ("/marketdata/chains", "chains", {}),
    ("/marketdata/expirationchain", "expirationchain", {}),
    ("/marketdata/pricehistory", "pricehistory", {}),
    ("/marketdata/movers/{index}", "movers", {"index": "index"}),
    ("/marketdata/markets", "markets", {}),
    ("/marketdata/markets/{market_id}", "markets", {"market_id": "markets"}),
    ("/marketdata/instruments", "instruments", {}),
    ("/marketdata/instruments/{cusip}", "instrument", {"cusip": "cusip"}),
    ("/accounts/accountNumbers", "account_list", {}),
    ("/accounts", "accounts", {}),
    ("/accounts/{account_ref}", "accounts", {"account_ref": "account_ref"}),
    ("/orders", "orders", {}),
    ("/accounts/{account_ref}/orders", "orders", {"account_ref": "account_ref"}),
    ("/accounts/{account_ref}/orders/{order_id}", "order", {"account_ref": "account_ref", "order_id": "order_id"}),
    ("/accounts/{account_ref}/transactions", "transactions", {"account_ref": "account_ref"}),
    ("/accounts/{account_ref}/transactions/{transaction_id}", "transaction",
     {"account_ref": "account_ref", "transaction_id": "transaction_id"}),
    ("/userPreference", "user_preference", {}),
)


def _fail(error: SchwabError) -> HTTPException:
    return HTTPException(status_code=reader.status_of(error),
                         detail={"code": error.code, "message": reader.message_of(error)})


def _guard(request: Request) -> None:
    candidate = request.headers.get("x-hokma-token") or ""          # the header only: a token in an address is kept in logs and histories
    if not token_matches(candidate, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "unauthorized"})


def _params(request: Request, name: str, path: dict[str, str]) -> dict[str, Any]:
    """The query string and the path parameters as the parameters of the read. A name the read does not have is a 422,
    never ignored; an account is a reference, so a hash or a number in the path is refused by the read itself."""
    found: dict[str, Any] = {}
    for key, value in request.query_params.items():
        found[key] = value
    for key, value in request.path_params.items():
        if key in path:
            found[path[key]] = value
    return found


def _handler(name: str, path: dict[str, str]):
    async def handle(request: Request) -> dict:
        _guard(request)
        try:
            return await asyncio.to_thread(reader.read, name, _params(request, name, path))
        except SchwabError as error:
            raise _fail(error) from None
    return handle


for _path, _name, _path_params in ROUTES:
    router.add_api_route(_path, _handler(_name, _path_params), methods=["GET"], name=f"schwab_{_name}",
                         summary=f"Schwab read: {_name}")


@router.post("/diagnostic/streaming", summary="Measure the Streamer and the raw Schwab answers (SPEC-016 T1, read only)")
async def streaming_diagnostic(request: Request) -> dict:
    """One run of the measuring tool: it records from the session of the hub (never a second Streamer), it never sends an order, and the file it
    writes carries no token, no account and no id. One run at a time. Body: `{"symbol": "QQQ", "seconds": 75}`."""
    _guard(request)
    from hokma.schwab import diagnostic

    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    symbol, seconds = body.get("symbol", "QQQ"), body.get("seconds", 75)
    if not isinstance(symbol, str) or isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise HTTPException(status_code=422, detail={"code": INVALID_PARAM, "message": "symbol and seconds are not valid"})
    try:
        return await diagnostic.run(symbol.upper()[:10], float(seconds))
    except diagnostic.DiagnosticBusy:
        raise HTTPException(status_code=409, detail={"code": "schwab.err.diagnostic_busy", "message": "a measuring run is already going"}) from None
    except SchwabError as error:
        raise _fail(error) from None


@router.get("/analytics/{calc}", summary="A calculation over Schwab data")
async def analytics_route(calc: str, request: Request) -> dict:
    _guard(request)
    if calc not in reader.ANALYTICS_READS:
        raise _fail(SchwabError(INVALID_PARAM, 404, detail="calc must be one of " + ", ".join(reader.ANALYTICS_READS)))
    try:
        return await asyncio.to_thread(reader.read, calc, _params(request, calc, {}))
    except SchwabError as error:
        raise _fail(error) from None
