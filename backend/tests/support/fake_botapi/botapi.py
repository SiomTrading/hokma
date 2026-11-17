"""Local double of the Telegram Bot API. `python -m support.fake_botapi.botapi PORT` serves it on 127.0.0.1."""
import asyncio
import email
import itertools
import json
import sys
from urllib.parse import parse_qsl

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Fake tokens, accepted by this double only.
VALID = {"111111111:" + "A" * 35: "bot1", "222222222:" + "B" * 35: "bot2"}

app = FastAPI()
S = {"revoked": set(), "net_down": False, "updates": [], "sent": [], "answers": [], "edits": [], "topics": [],
     "uid": itertools.count(1), "mid": itertools.count(100), "tid": itertools.count(500), "calls": [],
     "forum": True, "topic_perm": True}


def err(code: int, desc: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error_code": code, "description": desc}, status_code=code)


def _parse_body(raw: bytes, content_type: str) -> dict:
    if not raw:
        return {}
    if "json" in content_type:
        return json.loads(raw)
    if "multipart" in content_type:
        body = {}
        msg = email.message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw)
        for part in msg.get_payload():
            value = part.get_payload(decode=True).decode()
            try:
                value = json.loads(value)
            except ValueError:
                pass
            body[part.get_param("name", header="content-disposition")] = value
        return body
    return dict(parse_qsl(raw.decode()))


@app.post("/bot{token}/{method}")
@app.get("/bot{token}/{method}")
async def bot(token: str, method: str, request: Request):
    if S["net_down"]:
        return err(502, "Bad Gateway")
    if token not in VALID or token in S["revoked"]:
        return err(401, "Unauthorized")
    body = _parse_body(await request.body(), request.headers.get("content-type", ""))
    S["calls"].append((method, token[:9]))
    if method == "getMe":
        return {"ok": True, "result": {"id": 111, "is_bot": True, "first_name": "B", "username": VALID[token]}}
    if method in ("deleteWebhook", "setMyCommands"):
        return {"ok": True, "result": True}
    if method == "getUpdates":
        offset = int(body.get("offset") or 0)
        for _ in range(20):                                   # a short long-poll
            if token in S["revoked"]:
                return err(401, "Unauthorized")
            if S["net_down"]:
                return err(502, "Bad Gateway")
            updates = [u for u in S["updates"] if u["update_id"] >= offset]
            if updates:
                return {"ok": True, "result": updates}
            await asyncio.sleep(0.25)
        return {"ok": True, "result": []}
    if method == "sendMessage":
        S["sent"].append({"token": token[:9], **body})
        if body.get("message_thread_id") == 999:
            return err(400, "Bad Request: message thread not found")
        return {"ok": True, "result": {"message_id": next(S["mid"]), "date": 1,
                                       "chat": {"id": body["chat_id"], "type": "private"}, "text": body.get("text", "")}}
    if method == "answerCallbackQuery":
        S["answers"].append(body)
        return {"ok": True, "result": True}
    if method == "editMessageText":
        S["edits"].append(body)
        return {"ok": True, "result": True}
    if method == "createForumTopic":
        if not S["topic_perm"]:
            return err(400, "Bad Request: not enough rights to create a topic")
        thread = next(S["tid"])
        S["topics"].append({"name": body.get("name"), "thread": thread, "chat": body.get("chat_id")})
        return {"ok": True, "result": {"message_thread_id": thread, "name": body.get("name"), "icon_color": 1}}
    return err(404, "Not Found: " + method)


@app.post("/_ctl/{what}")
async def ctl(what: str, request: Request):
    raw = await request.body()
    b = json.loads(raw) if raw else {}
    if what == "push":
        update = {"update_id": next(S["uid"]), **b}
        S["updates"].append(update)
        return update
    if what == "revoke":
        S["revoked"].add(b["token"])
    elif what == "net":
        S["net_down"] = b["down"]
    elif what == "perm":
        S["topic_perm"] = b["ok"]
    elif what == "reset":
        for key in ("sent", "answers", "edits", "topics", "calls"):
            S[key].clear()
    return {}


@app.get("/_ctl/state")
async def state():
    return {k: S[k] for k in ("sent", "answers", "edits", "topics", "calls")} | {"revoked": list(S["revoked"])}


def run(port: int) -> None:
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    run(int(sys.argv[1]))
