"""The Schwab Streamer session (SPEC-016 4.1): ONE WebSocket per installation, shared by price, candles, options and futures.

The protocol is the official guide (REF schwab-trader-api-documentacion.md, section 7): `userPreference` gives the socket and the ids,
`LOGIN` (service ADMIN) must answer code 0 before any other command, then `ADD`, `UNSUBS`, `VIEW` and `LOGOUT`. Commands go ONE AT A
TIME, each waiting for its answer.

What this module keeps:
* the desired symbols per service and what the server already has, so a reconnection replays everything;
* the last value of every field per service and key for the Change services (LEVELONE_*), because Schwab sends only what changed;
* nothing secret outside memory: the address, the four ids and the token live in local variables of one session. Nothing outgoing is
  logged, and of the incoming only service, command and code. A host that is not in `stream_hosts` of `hardblock_brokers.json`
  (or an address that is not wss) is refused before any token leaves.

It never sends an order: the services this module can name are market data only.
"""
import asyncio
import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from hokma.core import tls
from hokma.kit import hardblock
from hokma.market.stream import BACKOFF_SECONDS, TerminalError
from hokma.schwab import endpoints, http

log = logging.getLogger("hokma.schwab.stream")

ADMIN = "ADMIN"
LEVELONE_EQUITIES = "LEVELONE_EQUITIES"
LEVELONE_OPTIONS = "LEVELONE_OPTIONS"
LEVELONE_FUTURES = "LEVELONE_FUTURES"
CHART_EQUITY = "CHART_EQUITY"
CHART_FUTURES = "CHART_FUTURES"
CHANGE_SERVICES = (LEVELONE_EQUITIES, LEVELONE_OPTIONS, LEVELONE_FUTURES)       # tipo Change: the last value of each field is kept
SEQUENCE_SERVICES = (CHART_EQUITY, CHART_FUTURES)                               # tipo All Sequence: each message is whole
SERVICES = CHANGE_SERVICES + SEQUENCE_SERVICES

# The fields asked for (guide 7.8, 7.9, 7.10, 7.14). `0` is the key and arrives as `key`.
FIELDS = {
    LEVELONE_EQUITIES: "0,1,2,3,4,5,8,9,10,11,12,17,18,29,30,31,32,33,34,35,36,37,38,42",
    LEVELONE_OPTIONS: "0,2,3,4,5,6,7,8,9,10,11,15,16,17,18,19,20,21,22,27,28,29,30,31,32,33,34,35,37,38,39",
    LEVELONE_FUTURES: "0,1,2,3,4,5,8,9,10,11,12,13,14,18,19,22,23,24,29,32,33,34,35,37,38",
    CHART_EQUITY: "0,1,2,3,4,5,6,7,8",
    CHART_FUTURES: "0,1,2,3,4,5,6",
}

SUCCESS_CODES = (0, 26, 27, 28, 29)
FAILED_COMMAND_CODES = (22, 23, 24, 25)
LOGIN_DENIED, SERVICE_UNAVAILABLE, CLOSE_CONNECTION, SYMBOL_LIMIT = 3, 11, 12, 19
NO_CONNECTION, BAD_FORMAT, STOP_STREAMING = 20, 21, 30

HEARTBEAT_SAMPLES = 50


@dataclass(frozen=True)
class StreamConfig:
    """The values that `providers.json` (section `stream` of the Schwab provider) holds. None is a constant of the code."""

    login_timeout: float = 10.0
    command_timeout: float = 10.0
    idle_timeout: float = 120.0               # no message and no heartbeat for this long: close and reconnect
    idle_logout: float = 300.0                # no subscriptions for this long: LOGOUT, reopened on demand
    other_session_wait: float = 30.0          # code 12: another Streamer connection of the same user
    rate_pause: float = 60.0
    chunk: int = 100                          # keys per command

    @classmethod
    def from_spec(cls, values: dict | None) -> "StreamConfig":
        v = values or {}
        return cls(float(v.get("login_timeout_seconds", cls.login_timeout)),
                   float(v.get("command_timeout_seconds", cls.command_timeout)),
                   float(v.get("idle_timeout_seconds", cls.idle_timeout)),
                   float(v.get("idle_logout_seconds", cls.idle_logout)),
                   float(v.get("other_session_wait_seconds", cls.other_session_wait)),
                   float(v.get("rate_pause_seconds", cls.rate_pause)),
                   int(v.get("chunk", cls.chunk)))


class _NoStreamerSecrets(logging.Filter):
    """The `websockets` library can log what it sends at debug level. Nothing that carries the login or the ids may pass."""

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        return not any(mark in text for mark in ("SchwabClient", "Authorization", "requests"))


for _name in ("websockets", "websockets.client", "websockets.server"):
    logging.getLogger(_name).addFilter(_NoStreamerSecrets())

_test_local = False


def allow_local_for_tests(on: bool = True) -> None:
    """Tests only: let the session open a plain `ws://127.0.0.1` fake server. The host list still decides everything else."""
    global _test_local
    _test_local = on


def check_address(url: str) -> None:
    """Refuse the socket address unless it is `wss` and its host is in `stream_hosts` (4.1 point 2). The token never goes first."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if _test_local and parts.scheme == "ws" and host == "127.0.0.1":
        return
    if parts.scheme != "wss" or host not in hardblock.stream_hosts():
        raise TerminalError("connections.schwab_denied", "streamer host not allowed")


ID_KEYS = frozenset({"SchwabClientCorrelId", "SchwabClientCustomerId", "SchwabClientChannel", "SchwabClientFunctionId",
                 "Authorization"})


def sanitize(message):
    """An incoming message without the identifiers of the session, for the measuring tool: the correlation id and the others are
    dropped, and of the `msg` of an answer only `status=` stays (the server and instance names go). Market data is not touched."""
    if isinstance(message, dict):
        out = {}
        for key, value in message.items():
            if key in ID_KEYS:
                continue
            if key == "msg" and isinstance(value, str):
                value = ";".join(part for part in value.split(";") if part.startswith("status="))
            out[key] = sanitize(value)
        return out
    if isinstance(message, list):
        return [sanitize(item) for item in message]
    return message


class _Reconnect(Exception):
    """Drop this connection and open another. `code` is a short word for the status; `delay` an own wait."""

    def __init__(self, code: str, delay: float | None = None) -> None:
        super().__init__(code)
        self.code, self.delay = code, delay


class SchwabStreamer:
    """One Streamer session with its reconnection. Callbacks run in the event loop and must not block:

    on_data(service, key, fields, changed, delayed, ts_ms)  `fields` is the whole state of that key for a Change service
                                                            (field number as text), the message itself for a CHART one;
                                                            `changed` the field numbers this message carried
    on_status(state, code, detail)                          state: live | reconnecting | down | idle
    on_limit(service, keys)                                 code 19: these keys did not enter and are not retried
    on_unavailable(service)                                 code 11: that service is not available for the account
    """

    def __init__(self, config: StreamConfig, on_data: Callable, on_status: Callable,
                 on_limit: Callable | None = None, on_unavailable: Callable | None = None,
                 backoff: tuple[float, ...] | None = None, tap: Callable | None = None) -> None:
        self._cfg = config
        self._taps: list[Callable] = [tap] if tap is not None else []   # the measuring tool sees every incoming message, without ids (see `sanitize`)
        self._on_data, self._on_status = on_data, on_status
        self._on_limit, self._on_unavailable = on_limit, on_unavailable
        self._backoff = backoff if backoff is not None else BACKOFF_SECONDS
        self._want: dict[str, set[str]] = {}
        self._sent: dict[str, set[str]] = {}
        self._fields: dict[str, str] = dict(FIELDS)
        self._dead: set[str] = set()                        # services answered with code 11 this session
        self._state: dict[tuple[str, str], dict] = {}
        self._runner: asyncio.Task | None = None
        self._closing = False
        self._dirty: asyncio.Event | None = None
        self._ws = None
        self._ids: dict[str, str] = {}
        self._requests = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._lock: asyncio.Lock | None = None
        self._last_rx = 0.0
        self._empty_since: float | None = None
        self._force_token = False
        self._denied_once = False
        self._was_ready = False
        self.stats = {"heartbeats": 0, "heartbeat_gaps_ms": [], "logins": 0, "sessions": 0}   # for the measuring tool: counts only
        self._last_beat: float | None = None

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"                    # nothing the session holds is ever shown

    # ---- what the owner of the session asks ----

    async def start(self) -> None:
        if self._runner is not None and not self._runner.done():
            return
        self._closing = False
        self._dirty = self._dirty or asyncio.Event()
        self._lock = asyncio.Lock()
        self._runner = asyncio.create_task(self._run(), name="schwab-streamer")

    def want(self, service: str, keys: list[str] | set[str]) -> None:
        """Add keys to the desired set of a service. The session opens (or reopens after an idle LOGOUT) when it is not running."""
        if service not in SERVICES:
            raise ValueError("unknown streamer service")
        if self._dirty is None:
            self._dirty = asyncio.Event()
        self._want.setdefault(service, set()).update(k for k in keys if k)
        self._empty_since = None
        self._dirty.set()
        if self._runner is None or self._runner.done():
            if not self._closing:
                asyncio.ensure_future(self.start())

    def drop(self, service: str, keys: list[str] | set[str]) -> None:
        self._want.get(service, set()).difference_update(keys)
        for key in keys:
            self._state.pop((service, key), None)
        if self._dirty is not None:
            self._dirty.set()

    def view(self, service: str, fields: str) -> None:
        """Change the fields of a whole service (command VIEW). Applies to the keys it already has and to those added later."""
        self._fields[service] = fields
        if self._sent.get(service) and self._dirty is not None:
            self._view_pending = getattr(self, "_view_pending", set()) | {service}
            self._dirty.set()

    def last(self, service: str, key: str) -> dict:
        """The last known value of every field of a key (a copy). Empty before its first message."""
        return dict(self._state.get((service, key), {}))

    def add_tap(self, tap: Callable) -> None:
        """A recorder on THIS session (the one the hub already has): Schwab allows one Streamer connection per user, so nobody opens a second."""
        self._taps.append(tap)

    def remove_tap(self, tap: Callable) -> None:
        if tap in self._taps:
            self._taps.remove(tap)

    def wanted(self, service: str) -> set[str]:
        return set(self._want.get(service, ()))

    async def stop(self) -> None:
        self._closing = True
        runner, self._runner = self._runner, None
        if runner is not None:
            await self._logout()
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    # ---- the loop of connections ----

    async def _run(self) -> None:
        attempt = 0
        while not self._closing:
            self._was_ready = False
            try:
                if await self._session() == "idle" and not self._closing:
                    self._on_status("idle", "idle", "")
                    return
                if self._closing:
                    return
                code, delay = "closed", None
            except asyncio.CancelledError:
                raise
            except TerminalError as error:
                self._on_status("down", error.code, error.detail)
                return
            except _Reconnect as error:
                code, delay = error.code, error.delay
            except http.SchwabError as error:
                code, delay = self._schwab_failure(error)
            except Exception as error:                       # a drop: classified, the text of the exception is never shown
                code, delay = self._classify(error), None
            self._ws = None
            self._sent = {}
            self._state = {}
            self._dead = set()
            self._fail_pending()
            if self._closing:
                return
            attempt = 0 if self._was_ready else attempt + 1
            self._on_status("reconnecting", code, "")
            wait = delay if delay is not None else self._backoff[min(attempt, len(self._backoff) - 1)] * (0.75 + random.random() / 2)
            await asyncio.sleep(wait)

    @staticmethod
    def _classify(error: BaseException) -> str:
        if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
            return "timeout"
        if isinstance(error, (OSError, ConnectionClosed)):
            return "network"
        return "dropped"

    def _schwab_failure(self, error: http.SchwabError) -> tuple[str, float | None]:
        if error.code == http.NOT_CONNECTED:
            raise TerminalError("connections.no_login")
        if error.code == http.EXPIRED:
            raise TerminalError("connections.schwab_expired")
        if error.code == http.DENIED:
            raise TerminalError("connections.schwab_denied")
        if error.code == http.RATE_LIMITED:
            return "rate_limited", self._cfg.rate_pause
        return "unreachable", None

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("closed"))
        self._pending = {}

    async def _session(self):
        """One connection from the preferences to its end. Returns "idle" when it closed itself for lack of subscriptions;
        anything else that ends it is an exception that `_run` classifies."""
        info = await self._preferences()
        check_address(info["url"])
        self._ids = info
        self.stats["sessions"] += 1
        ssl_context = tls.outbound_context() if info["url"].startswith("wss://") else None
        async with connect(info["url"], open_timeout=10, max_size=1 << 22, close_timeout=2, ping_interval=None,
                           ssl=ssl_context) as ws:
            self._ws = ws
            self._last_rx = time.monotonic()
            reader = asyncio.create_task(self._read(ws))
            try:
                await self._login()
                self._denied_once = False
                self._force_token = False
                self._was_ready = True
                self._empty_since = None if any(self._want.values()) else time.monotonic()
                self._dirty.set()
                self._on_status("live", "connected", "")
                work = asyncio.create_task(self._reconcile())
                watch = asyncio.create_task(self._watch())
                done, _ = await asyncio.wait({reader, work, watch}, return_when=asyncio.FIRST_COMPLETED)
                for task in (reader, work, watch):
                    if task not in done:
                        task.cancel()
                await asyncio.gather(reader, work, watch, return_exceptions=True)
                for task in done:
                    outcome = task.result()                 # raises what ended the session
                    if outcome == "idle":
                        await self._logout()
                        return "idle"
                return None
            except BaseException:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                raise

    async def _preferences(self) -> dict:
        preference = await asyncio.to_thread(endpoints.user_preference)
        streamers = getattr(preference, "streamerInfo", None) or []
        if not streamers:
            raise OSError("no streamer info")
        item = streamers[0]
        ids = {"url": getattr(item, "streamerSocketUrl", None), "customer": getattr(item, "schwabClientCustomerId", None),
               "correl": getattr(item, "schwabClientCorrelId", None), "channel": getattr(item, "schwabClientChannel", None),
               "function": getattr(item, "schwabClientFunctionId", None)}
        if not all(isinstance(v, str) and v for v in ids.values()):
            raise OSError("incomplete streamer info")
        return ids

    # ---- the wire ----

    def _request(self, service: str, command: str, parameters: dict | None = None) -> tuple[str, dict]:
        self._requests += 1
        request_id = str(self._requests)
        body = {"requestid": request_id, "service": service, "command": command,
                "SchwabClientCustomerId": self._ids["customer"], "SchwabClientCorrelId": self._ids["correl"]}
        if parameters is not None:
            body["parameters"] = parameters
        return request_id, body

    async def _send_and_wait(self, service: str, command: str, parameters: dict | None, timeout: float) -> dict:
        request_id, body = self._request(service, command, parameters)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._ws.send(json.dumps({"requests": [body]}, separators=(",", ":")))
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def _login(self) -> None:
        token = await asyncio.to_thread(http._token, self._force_token)
        params = {"Authorization": token, "SchwabClientChannel": self._ids["channel"],
                  "SchwabClientFunctionId": self._ids["function"]}
        answer = await self._send_and_wait(ADMIN, "LOGIN", params, self._cfg.login_timeout)
        code = answer.get("code")
        if code == LOGIN_DENIED:
            # an expired token: a new one is asked of `auth` and the LOGIN is made once more; a second denial ends the source
            if self._denied_once:
                raise TerminalError("connections.schwab_expired")
            self._denied_once = True
            self._force_token = True
            raise _Reconnect("login_denied", 0.0)
        if code != 0:
            raise _Reconnect("login_failed")
        self.stats["logins"] += 1
        self._empty_since = None

    async def _logout(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await self._send_and_wait(ADMIN, "LOGOUT", {}, 1.5)
        except Exception:
            pass

    async def _read(self, ws) -> None:
        async for raw in ws:
            self._last_rx = time.monotonic()
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict):
                if self._taps:
                    clean = sanitize(message)
                    for tap in list(self._taps):
                        try:
                            tap(clean)
                        except Exception:
                            log.warning("streamer tap failed")
                self._dispatch(message)
        raise _Reconnect("closed")

    def _dispatch(self, message: dict) -> None:
        for item in message.get("notify") or []:
            if isinstance(item, dict) and "heartbeat" in item:
                self._beat()
        for item in message.get("response") or []:
            if isinstance(item, dict):
                self._response(item)
        for block in message.get("data") or []:
            if isinstance(block, dict):
                self._data(block)

    def _beat(self) -> None:
        now = time.monotonic()
        self.stats["heartbeats"] += 1
        if self._last_beat is not None and len(self.stats["heartbeat_gaps_ms"]) < HEARTBEAT_SAMPLES:
            self.stats["heartbeat_gaps_ms"].append(int((now - self._last_beat) * 1000))
        self._last_beat = now

    def _response(self, item: dict) -> None:
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        code = content.get("code")
        log.debug("streamer response service=%s command=%s code=%s", item.get("service"), item.get("command"), code)
        future = self._pending.get(str(item.get("requestid")))
        if future is not None and not future.done():
            future.set_result({"code": code, "service": item.get("service")})
        if code == CLOSE_CONNECTION:
            raise _Reconnect("other_session", self._cfg.other_session_wait)
        if code == STOP_STREAMING and (future is None or future.done()):
            raise _Reconnect("stopped")
        if code == NO_CONNECTION and future is None:
            raise _Reconnect("no_connection")

    def _data(self, block: dict) -> None:
        service = block.get("service")
        content = block.get("content")
        ts = block.get("timestamp")
        if service not in SERVICES or not isinstance(content, list):
            return
        for entry in content:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if not isinstance(key, str):
                continue
            delayed = bool(entry.get("delayed")) if "delayed" in entry else self._state.get((service, key), {}).get("delayed", False)
            fields = {k: v for k, v in entry.items() if k not in ("key", "delayed", "assetMainType", "assetSubType", "cusip")}
            if service in CHANGE_SERVICES:
                state = self._state.setdefault((service, key), {})
                state.update(fields)
                state["delayed"] = delayed
                snapshot = dict(state)
            else:
                snapshot = dict(fields)
            try:
                self._on_data(service, key, snapshot, frozenset(fields), delayed, ts if isinstance(ts, int) else None)
            except Exception:
                log.warning("streamer data callback failed service=%s", service)

    # ---- the subscriptions: one command at a time ----

    async def _command(self, service: str, command: str, keys: list[str] | None = None, fields: str | None = None) -> int:
        params: dict = {}
        if keys is not None:
            params["keys"] = ",".join(keys)
        if fields is not None:
            params["fields"] = fields
        async with self._lock:
            for attempt in (1, 2):
                answer = await self._send_and_wait(service, command, params, self._cfg.command_timeout)
                code = answer.get("code")
                if code in FAILED_COMMAND_CODES and attempt == 1:
                    log.warning("streamer command failed service=%s command=%s code=%s: tried once more", service, command, code)
                    continue
                return code if isinstance(code, int) else -1
        return -1

    async def _reconcile(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            for service in sorted(set(self._want) | set(self._sent)):
                if service in self._dead:
                    continue
                await self._reconcile_service(service)
            if not any(self._want.values()) and not any(self._sent.values()):
                self._empty_since = self._empty_since or time.monotonic()
            else:
                self._empty_since = None

    async def _reconcile_service(self, service: str) -> None:
        chunk = max(1, self._cfg.chunk)
        have = self._sent.setdefault(service, set())
        gone = sorted(have - self._want.get(service, set()))
        for start in range(0, len(gone), chunk):
            part = gone[start:start + chunk]
            if await self._command(service, "UNSUBS", part) in SUCCESS_CODES:
                have.difference_update(part)
        pending_view = getattr(self, "_view_pending", set())
        if service in pending_view and have:
            pending_view.discard(service)
            await self._command(service, "VIEW", None, self._fields[service])
        new = sorted(self._want.get(service, set()) - have)
        for start in range(0, len(new), chunk):
            part = new[start:start + chunk]
            code = await self._command(service, "ADD", part, self._fields[service])
            if code in SUCCESS_CODES:
                have.update(part)
                self._on_status("live", "subscribed", "")
            elif code == SYMBOL_LIMIT:
                self._want[service].difference_update(part)
                await self._command(service, "UNSUBS", part)             # best effort: some of them may have entered
                if self._on_limit:
                    self._on_limit(service, part)
            elif code == SERVICE_UNAVAILABLE:
                self._dead.add(service)
                if self._on_unavailable:
                    self._on_unavailable(service)
                return
            elif code in (NO_CONNECTION,):
                raise _Reconnect("no_connection")
            else:
                log.warning("streamer subscription not applied service=%s code=%s", service, code)

    async def _watch(self):
        """The heartbeat of the server is the proof of life: without any message for `idle_timeout` the connection is closed and
        reopened. With nothing subscribed for `idle_logout` the session says goodbye by itself (Schwab would close it with code 30)."""
        step = max(0.05, min(self._cfg.idle_timeout, self._cfg.idle_logout) / 4)
        while True:
            await asyncio.sleep(step)
            now = time.monotonic()
            if now - self._last_rx > self._cfg.idle_timeout:
                raise _Reconnect("timeout")
            if self._empty_since is not None and now - self._empty_since > self._cfg.idle_logout:
                return "idle"
