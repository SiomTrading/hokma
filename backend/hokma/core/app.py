"""Hokma FastAPI application."""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from hokma import __version__
from hokma.agents.api import router as agents_router
from hokma.core.config import dashboard_dist
from hokma.core.origin import TEST_HOSTS, LocalOnlyMiddleware, hosts_for_port
from hokma.db.api import database_lifespan, router as database_router
from hokma.engine.api import router as engine_router
from hokma.engine.conversations_api import router as conversations_router
from hokma.learn.api import router as learn_router
from hokma.market.api import router as market_router
from hokma.projects.api import router as projects_router
from hokma.rpc.ws import router as rpc_router
from hokma.setup import router as setup_router
from hokma.status.api import router as status_router
from hokma.terminal.api import router as terminal_router
from hokma.update.api import router as update_router
from hokma.voice.api import router as voice_router


def create_app(port: int | None = None) -> FastAPI:
    """`port` is the one the server listens on: only 127.0.0.1:port and localhost:port are accepted as Host.
    Without it only the TestClient host is accepted, so a forgotten port fails closed."""
    app = FastAPI(title="Hokma", version=__version__, lifespan=database_lifespan)  # opens app.state.db
    app.state.allowed_hosts = hosts_for_port(port) if port is not None else TEST_HOSTS
    app.add_middleware(LocalOnlyMiddleware)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    app.include_router(rpc_router)
    app.include_router(update_router)
    app.include_router(database_router)
    app.include_router(voice_router)
    app.include_router(engine_router)
    app.include_router(projects_router)
    app.include_router(setup_router)
    app.include_router(terminal_router)
    app.include_router(conversations_router)
    app.include_router(status_router)
    app.include_router(learn_router)
    app.include_router(market_router)
    app.include_router(agents_router)

    dist = dashboard_dist()
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")
    return app
