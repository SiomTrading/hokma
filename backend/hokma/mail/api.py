"""Gmail connection over HTTP. Every route needs the per-boot session token (`X-Hokma-Token` or `?token=`).

    GET    /api/mail  -> {connected, email, store_available, apppasswords_url}   (the password never comes back)
    PUT    /api/mail  -> {connected: true, email}   body {email, password}; both servers are tested before saving
    DELETE /api/mail  -> {connected: false}
    GET    /api/mail/notify       -> {connected, email, fallback, jobs: [{id, name, mail}]}   (C7-2c)
    PUT    /api/mail/notify       -> same; body {fallback, jobs: [id]}: the routines that also notify by mail
    POST   /api/mail/notify/test  -> {sent: true}   a test mail to the connected address
"""
import asyncio

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hokma.connections.store import KeyStore
from hokma.core.origin import token_matches
from hokma.i18n import accepted_language, t, use_language
from hokma.mail import gmail, notify
from hokma.rpc.ws import SESSION_TOKEN

router = APIRouter(prefix="/api/mail")


class ConnectBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


async def _go(request: Request, header_token: str, query_token: str, fn):
    if not token_matches(header_token or query_token, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "unauthorized"})
    with use_language(accepted_language(request.headers.get("accept-language", ""))):
        try:
            return await asyncio.to_thread(fn)
        except gmail.MailError as error:
            raise HTTPException(status_code=error.status, detail={"code": error.code, "message": t(error.code)}) from None


def _state() -> dict:
    email = gmail.account()
    return {"connected": email is not None, "email": email, "store_available": KeyStore().available(),
            "apppasswords_url": gmail.APP_PASSWORDS_URL}


@router.get("")
async def state(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
    return await _go(request, x_hokma_token, token, _state)


@router.put("")
async def connect(body: ConnectBody, request: Request, x_hokma_token: str = Header(default=""),
                  token: str = Query(default="")):
    email = await _go(request, x_hokma_token, token, lambda: gmail.connect(body.email, body.password))
    return {"connected": True, "email": email}


@router.delete("")
async def disconnect(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
    await _go(request, x_hokma_token, token, gmail.disconnect)
    return {"connected": False}


class NotifyBody(BaseModel):
    fallback: bool
    jobs: list[int] = Field(max_length=500)


def _notify_state(request: Request) -> dict:
    choice = notify.load()
    with request.app.state.db.read() as u:
        jobs = [{"id": j.id, "name": j.name, "mail": j.id in choice["jobs"]} for j in u.scheduled_jobs.list()]
    return {"connected": gmail.account() is not None, "email": gmail.account(), "fallback": choice["fallback"], "jobs": jobs}


@router.get("/notify")
async def notify_state(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
    return await _go(request, x_hokma_token, token, lambda: _notify_state(request))


@router.put("/notify")
async def notify_save(body: NotifyBody, request: Request, x_hokma_token: str = Header(default=""),
                      token: str = Query(default="")):
    def save() -> dict:
        if gmail.account() is None:
            raise gmail.MailError("mail.not_connected", 409)
        notify.save(body.fallback, body.jobs)
        return _notify_state(request)
    return await _go(request, x_hokma_token, token, save)


@router.post("/notify/test")
async def notify_test(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
    def send() -> dict:
        notify.send_to_self(t("mail.notify.test_subject"), t("mail.notify.test_body"))
        return {"sent": True}
    return await _go(request, x_hokma_token, token, send)
