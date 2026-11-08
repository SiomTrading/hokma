"""Base of the adapters that talk to a provider over a WebSocket."""
import asyncio
import json
import logging
from datetime import datetime, timezone

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from hokma.market.providers import ProviderSpec
from hokma.market.stream import LiveSource, SourceCapabilities, TerminalError

OPEN_TIMEOUT = 10
MAX_MESSAGE_BYTES = 1 << 20


class _NoSecretInLogs(logging.Filter):
    """Some providers carry the key in the connection URL. No library log line may print it."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "token=" not in record.getMessage().lower()


logging.getLogger("websockets").addFilter(_NoSecretInLogs())
logging.getLogger("websockets").setLevel(logging.WARNING)


def parse_iso_ms(text: str) -> int:
    """RFC 3339 time (any fraction length, `Z` or an offset) to epoch milliseconds."""
    body = text.strip()
    if body.endswith("Z"):
        body = body[:-1] + "+00:00"
    if "." in body:
        head, _, rest = body.partition(".")
        digits = ""
        for i, ch in enumerate(rest):
            if not ch.isdigit():
                tail = rest[i:]
                break
            digits += ch
        else:
            tail = ""
        body = f"{head}.{digits[:6].ljust(6, '0')}{tail}"
    moment = datetime.fromisoformat(body)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


class WebSocketSource(LiveSource):
    def __init__(self, spec: ProviderSpec, url: str | None = None, backoff: tuple[float, ...] | None = None) -> None:
        super().__init__(backoff)
        self.provider_id = spec.id
        self.name = spec.name
        self.data_state = spec.data_state
        self.capabilities = SourceCapabilities(True, spec.max_symbols, spec.asset_classes, spec.delay_seconds)
        self._url_base = url or spec.url or ""
        self._ws = None

    # ---- to override ----

    def _url(self) -> str:
        return self._url_base

    async def _handshake(self, ws) -> None:
        """Authenticate and replay `self._symbols`, then call `_mark_ready()`."""
        self._mark_ready()

    def _on_message(self, message) -> None: ...

    # ---- plumbing ----

    async def _session(self) -> None:
        try:
            async with connect(self._url(), open_timeout=OPEN_TIMEOUT, max_size=MAX_MESSAGE_BYTES,
                               close_timeout=2, ping_interval=20, ping_timeout=20) as ws:
                self._ws = ws
                try:
                    await self._handshake(ws)
                    async for raw in ws:
                        try:
                            message = json.loads(raw)
                        except ValueError:
                            continue
                        self._on_message(message)
                finally:
                    self._ws = None
        except InvalidStatus as error:
            if error.response.status_code in (401, 403):
                raise TerminalError("auth_failed") from None
            raise

    async def _recv(self, ws, timeout: float = OPEN_TIMEOUT):
        return json.loads(await asyncio.wait_for(ws.recv(), timeout))

    async def _send(self, payload) -> None:
        ws = self._ws
        if ws is not None:
            await ws.send(json.dumps(payload, separators=(",", ":")))

    async def _after_close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
