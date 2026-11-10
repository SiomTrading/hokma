"""Hokma FastAPI application."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request

from hokma import __version__
from hokma.agents.api import router as agents_router
from hokma.connections.api import router as connections_router
from hokma.connections.runtime import shutdown as connections_shutdown
from hokma.core import identity
from hokma.core.config import browser_host, dashboard_dist
from hokma.core.dashboard_static import DashboardFiles
from hokma.core.lifecycle import Lifecycle, WebSocketTracker
from hokma.core.shutdown_api import router as shutdown_router
from hokma.core.origin import TEST_HOSTS, LocalOnlyMiddleware, hosts_for_port
from hokma.db.api import database_lifespan, router as database_router
from hokma.engines.api import router as engines_router
from hokma.engine.api import router as engine_router
from hokma.engine.conversations_api import router as conversations_router
from hokma.learn.api import router as learn_router
from hokma.permissions.api import router as permissions_router
from hokma.market.api import router as market_router
from hokma.market.ws import router as market_ws_router
from hokma.projects.api import router as projects_router
from hokma.rpc.ws import router as rpc_router
from hokma.setup import router as setup_router
from hokma.status.api import router as status_router
from hokma.terminal.api import router as terminal_router
from hokma.update.api import router as update_router
from hokma.update.state import UpdateState
from hokma.voice.api import router as voice_router


def create_app(port: int | None = None, *, lifecycle: Lifecycle | None = None,
               auto_update_check: bool = False) -> FastAPI:
    """`port` is the one the server listens on: only 127.0.0.1:port and localhost:port are accepted as Host.
    Without it only the TestClient host is accepted, so a forgotten port fails closed.
    `auto_update_check` starts the automatic search for a new version (at start and every 24 hours); only the
    real engine turns it on."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with database_lifespan(app):
            checker = None
            if auto_update_check:
                checker = asyncio.create_task(app.state.updates.auto_check_loop(lambda: app.state.db))
            try:
                yield
            finally:
                if checker is not None:
                    checker.cancel()
                await connections_shutdown()

    app = FastAPI(title="Hokma", version=__version__, lifespan=lifespan)  # opens app.state.db
    app.state.allowed_hosts = hosts_for_port(port, browser_host()) if port is not None else TEST_HOSTS
    app.state.lifecycle = lifecycle or Lifecycle()
    app.state.updates = UpdateState()
    app.add_middleware(LocalOnlyMiddleware)
    app.add_middleware(WebSocketTracker)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get(identity.ROUTE)
    def engine_proof(request: Request, nonce: str = Query(default="")) -> dict:
        """Proof that this is the engine of this installation (see `identity.py`). Answers only on the numeric
        loopback address, which is where `hokma start` asks; nothing secret is in the answer."""
        server = request.scope.get("server")
        if not identity.NONCE_PATTERN.fullmatch(nonce) or not server or request.headers.get("host") != f"127.0.0.1:{server[1]}":
            raise HTTPException(status_code=404)
        return {"proof": identity.proof(nonce, int(server[1]))}

    app.include_router(rpc_router)
    app.include_router(update_router)
    app.include_router(shutdown_router)
    app.include_router(database_router)
    app.include_router(voice_router)
    app.include_router(engine_router)
    app.include_router(engines_router)
    app.include_router(projects_router)
    app.include_router(setup_router)
    app.include_router(terminal_router)
    app.include_router(conversations_router)
    app.include_router(status_router)
    app.include_router(learn_router)
    app.include_router(market_router)
    app.include_router(market_ws_router)
    app.include_router(connections_router)
    app.include_router(agents_router)
    app.include_router(permissions_router)

    dist = dashboard_dist()
    if dist.is_dir():
        app.mount("/", DashboardFiles(directory=dist, html=True), name="dashboard")
    return app
