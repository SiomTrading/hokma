"""A fake Schwab Streamer (SPEC-016, guide section 7) on a local port, with faults a test can inject. Nothing touches the network.

Server side: `LOGIN` (ADMIN) with the access token and the ids of `userPreference` (a wrong token: code 3 and close; a wrong id: code 20),
no other command before a good LOGIN (code 20), then `ADD`, `SUBS`, `UNSUBS`, `VIEW` and `LOGOUT`, each answered by requestid. Commands are
handled in tasks so a client that sends two at once is caught (`max_inflight`). The ids and the token are made up and distinctive so a
redaction test can look for them.
"""
import asyncio
import json

from live_support import FakeWs
from schwab_support import FAKE_TOKEN

CUSTOMER = "CUST-SECRET-7788"
CORREL = "CORREL-SECRET-9911"
CHANNEL = "N9"
FUNCTION = "APIAPP"


class StreamerFakeServer:
    def __init__(self, tokens=(FAKE_TOKEN,), max_symbols: int | None = None, answer_delay: float = 0.0) -> None:
        self.tokens = set(tokens)
        self.max_symbols = max_symbols
        self.answer_delay = answer_delay
        self.unavailable: set[str] = set()
        self.fail_next: list[int] = []                     # codes the next subscription commands answer with
        self.silent = False                                # reads everything, answers nothing and sends no heartbeat
        self.login_code: int | None = None                 # forces the answer of every LOGIN
        self.received: list[dict] = []                     # every command, in arrival order
        self.logins: list[str] = []                        # the Authorization of each LOGIN
        self.subscribed: dict[str, set[str]] = {}
        self.fields: dict[str, str] = {}
        self.attempts = 0
        self.inflight = 0
        self.max_inflight = 0
        self.log: list[str] = []                           # "SERVICE COMMAND" in order, to check what came before what
        self._fake = FakeWs(self._handler)

    @property
    def url(self) -> str:
        return self._fake.url

    def start(self) -> "StreamerFakeServer":
        self._fake.start()
        return self

    def stop(self) -> None:
        self._fake.stop()

    def commands(self, service: str | None = None, command: str | None = None) -> list[dict]:
        return [c for c in self.received if (service is None or c["service"] == service)
                and (command is None or c["command"] == command)]

    # ---- what a test injects ----

    def push(self, payload: dict) -> None:
        self._fake.send_all(payload)

    def data(self, service: str, items: list[dict], delayed: bool = False) -> None:
        self.push({"data": [{"service": service, "timestamp": 1700000000000, "command": "SUBS",
                             "content": [{"delayed": delayed, **item} for item in items]}]})

    def heartbeat(self) -> None:
        self.push({"notify": [{"heartbeat": "1700000000000"}]})

    def respond(self, service: str, command: str, code: int, msg: str = "") -> None:
        """An answer nobody asked for (code 12 or 30 arrive like this)."""
        self.push({"response": [{"service": service, "command": command, "requestid": "0", "SchwabClientCorrelId": CORREL,
                                 "timestamp": 1700000000000, "content": {"code": code, "msg": msg}}]})

    def close_clients(self, code: int = 1011) -> None:
        async def go():
            for ws in list(self._fake.clients):
                await ws.close(code)
        asyncio.run_coroutine_threadsafe(go(), self._fake._thread.loop)          # not awaited: the close waits for the client's loop

    # ---- the server's side of one connection ----

    async def _handler(self, ws, fake: FakeWs) -> None:
        self.attempts += 1
        logged = False
        tasks: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                body = json.loads(raw)
                for request in body.get("requests", []):
                    task = asyncio.create_task(self._handle(ws, request, lambda: logged))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                    if request.get("service") == "ADMIN" and request.get("command") == "LOGIN":
                        await task                        # nothing else is read before the LOGIN answer, as a real server does
                        logged = self._last_login_ok
        finally:
            for task in tasks:
                task.cancel()

    _last_login_ok = False

    async def _handle(self, ws, request: dict, logged) -> None:
        service, command, request_id = request["service"], request["command"], request["requestid"]
        params = request.get("parameters") or {}
        self.received.append({"service": service, "command": command, "keys": params.get("keys"), "fields": params.get("fields"),
                              "requestid": request_id, "customer": request.get("SchwabClientCustomerId"),
                              "correl": request.get("SchwabClientCorrelId")})
        self.log.append(f"{service} {command}")
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.silent:
                return
            if self.answer_delay:
                await asyncio.sleep(self.answer_delay)
            if service == "ADMIN" and command == "LOGIN":
                await self._login(ws, request, params)
            elif service == "ADMIN" and command == "LOGOUT":
                await self._reply(ws, request, 0, "SUCCESS")
                await ws.close(1000)
            elif not logged():
                await self._reply(ws, request, 20, "stream connection not found")
            else:
                await self._subscription(ws, request, params)
        finally:
            self.inflight -= 1

    async def _login(self, ws, request: dict, params: dict) -> None:
        token = params.get("Authorization", "")
        self.logins.append(token)
        wrong_ids = request.get("SchwabClientCustomerId") != CUSTOMER or request.get("SchwabClientCorrelId") != CORREL
        if self.login_code is not None:
            code = self.login_code
        elif token not in self.tokens:
            code = 3
        elif wrong_ids or params.get("SchwabClientChannel") != CHANNEL or params.get("SchwabClientFunctionId") != FUNCTION:
            code = 3
        else:
            code = 0
        self._last_login_ok = code == 0
        await self._reply(ws, request, code, "server=fake;status=NP" if code == 0 else "Login Denied.")
        if code != 0:
            await ws.close(1000)

    async def _subscription(self, ws, request: dict, params: dict) -> None:
        service, command = request["service"], request["command"]
        keys = [k for k in (params.get("keys") or "").split(",") if k]
        have = self.subscribed.setdefault(service, set())
        if self.fail_next:
            await self._reply(ws, request, self.fail_next.pop(0), "failed")
            return
        if service in self.unavailable:
            await self._reply(ws, request, 11, "service not available")
            return
        if command in ("ADD", "SUBS"):
            if command == "SUBS":
                have.clear()
            total = sum(len(v) for v in self.subscribed.values())
            if self.max_symbols is not None and total + len(set(keys) - have) > self.max_symbols:
                await self._reply(ws, request, 19, "symbol limit reached")
                return
            have.update(keys)
            self.fields[service] = params.get("fields", self.fields.get(service, ""))
        elif command == "UNSUBS":
            have.difference_update(keys)
        elif command == "VIEW":
            self.fields[service] = params.get("fields", "")
        await self._reply(ws, request, 0, f"{command} command succeeded")

    async def _reply(self, ws, request: dict, code: int, msg: str) -> None:
        await ws.send(json.dumps({"response": [{
            "service": request["service"], "command": request["command"], "requestid": request["requestid"],
            "SchwabClientCorrelId": request.get("SchwabClientCorrelId"), "timestamp": 1700000000000,
            "content": {"code": code, "msg": msg}}]}))
