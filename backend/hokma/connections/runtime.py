"""The one `MarketHub` and the one `ConnectionService` of the engine, created on first use.

Tests build their own with `install` and drop them with `reset`."""
import threading

from hokma.connections.service import ConnectionService
from hokma.market.hub import MarketHub
from hokma.market.providers import get_catalog

_lock = threading.Lock()
_service: ConnectionService | None = None
_hub: MarketHub | None = None


def get_service() -> ConnectionService:
    global _service
    with _lock:
        if _service is None:
            _service = ConnectionService(get_catalog())
        return _service


def get_hub() -> MarketHub:
    global _hub
    service = get_service()
    with _lock:
        if _hub is None:
            from hokma.market.api import get_source, now

            _hub = MarketHub(service.catalog, service, get_source, clock=now)
        return _hub


def install(service: ConnectionService, hub: MarketHub) -> None:
    global _service, _hub
    with _lock:
        _service, _hub = service, hub


def reset() -> None:
    global _service, _hub
    with _lock:
        _service, _hub = None, None


async def prepare() -> MarketHub:
    """Learn which keys are saved (once, in a thread) and start warming the providers that are ready."""
    import asyncio

    service, hub = get_service(), get_hub()
    if not service.loaded:
        await asyncio.to_thread(service.load)
    await hub.ensure_started()
    return hub


async def shutdown() -> None:
    hub = _hub
    if hub is not None:
        await hub.close()
