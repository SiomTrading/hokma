"""Alpaca market data stream, IEX feed. Authentication is a message with the Key ID and the Secret; the free plan
allows one connection and 30 symbols."""
from hokma.market.adapters.ws_base import WebSocketSource, parse_iso_ms
from hokma.market.providers import ProviderSpec
from hokma.market.stream import SourceStatus, Tick, TerminalError

AUTH_ERRORS = {401, 402, 403, 404}


class AlpacaSource(WebSocketSource):
    def __init__(self, spec: ProviderSpec, credentials: dict, url: str | None = None, **kwargs) -> None:
        super().__init__(spec, url, **kwargs)
        self.__credentials = {"key": credentials["key_id"], "secret": credentials["secret"]}

    async def _handshake(self, ws) -> None:
        await self._recv(ws)                                           # [{"T":"success","msg":"connected"}]
        await self._send({"action": "auth", **self.__credentials})
        for item in await self._recv(ws):
            if item.get("T") == "error":
                code = int(item.get("code", 0))
                raise TerminalError("connection_limit" if code == 406 else "auth_failed")
        if self._symbols:
            await self._send_subscribe(sorted(self._symbols))
        self._mark_ready()

    async def _send_subscribe(self, symbols: list[str]) -> None:
        await self._send({"action": "subscribe", "trades": symbols})

    async def _send_unsubscribe(self, symbols: list[str]) -> None:
        await self._send({"action": "unsubscribe", "trades": symbols})

    def _on_message(self, message) -> None:
        for item in message if isinstance(message, list) else [message]:
            if not isinstance(item, dict):
                continue
            kind = item.get("T")
            try:
                if kind == "t":
                    self.emit(Tick(item["S"], parse_iso_ms(item["t"]), float(item["p"]), float(item.get("s") or 0)))
                elif kind == "subscription":
                    self.emit(SourceStatus(self.data_state, "subscribed"))
                elif kind == "error":
                    code = int(item.get("code", 0))
                    if code in AUTH_ERRORS:
                        raise TerminalError("auth_failed")
                    if code == 406:
                        raise TerminalError("connection_limit")
            except (KeyError, ValueError, TypeError):
                continue
