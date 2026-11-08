"""Massive futures stream. Channels: trades (`T.`) and minute aggregates (`AM.`); the aggregate is the finished minute
bar and replaces the one built from trades."""
from hokma.market.adapters.ws_base import WebSocketSource
from hokma.market.providers import ProviderSpec
from hokma.market.stream import BarUpdate, SourceStatus, Tick, TerminalError


class MassiveFuturesSource(WebSocketSource):
    def __init__(self, spec: ProviderSpec, key: str, url: str | None = None, **kwargs) -> None:
        super().__init__(spec, url, **kwargs)
        self.__key = key

    async def _handshake(self, ws) -> None:
        await self._recv(ws)                                           # [{"ev":"status","status":"connected"}]
        await self._send({"action": "auth", "params": self.__key})
        for item in await self._recv(ws):
            if item.get("status") == "auth_failed":
                raise TerminalError("auth_failed")
        if self._symbols:
            await self._send_subscribe(sorted(self._symbols))
        self._mark_ready()

    @staticmethod
    def _params(symbols: list[str]) -> str:
        return ",".join(f"{channel}.{s}" for s in symbols for channel in ("T", "AM"))

    async def _send_subscribe(self, symbols: list[str]) -> None:
        await self._send({"action": "subscribe", "params": self._params(symbols)})

    async def _send_unsubscribe(self, symbols: list[str]) -> None:
        await self._send({"action": "unsubscribe", "params": self._params(symbols)})

    def _on_message(self, message) -> None:
        for item in message if isinstance(message, list) else [message]:
            if not isinstance(item, dict):
                continue
            kind = item.get("ev")
            try:
                if kind == "T":
                    self.emit(Tick(item["sym"], int(item["t"]), float(item["p"]), float(item.get("s") or 0)))
                elif kind == "AM":
                    self.emit(BarUpdate(item["sym"], int(item["s"]), float(item["o"]), float(item["h"]),
                                        float(item["l"]), float(item["c"]), float(item.get("v") or 0), final=True))
                elif kind == "status":
                    if item.get("status") == "auth_failed":
                        raise TerminalError("auth_failed")
                    if item.get("status") == "success" and "subscribed" in str(item.get("message", "")):
                        self.emit(SourceStatus(self.data_state, "subscribed"))
            except (KeyError, ValueError, TypeError):
                continue
