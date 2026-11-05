"""Hokma FastAPI application."""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from hokma import __version__
from hokma.core.config import dashboard_dist
from hokma.rpc.ws import router as rpc_router
from hokma.update.api import router as update_router


def create_app() -> FastAPI:
    app = FastAPI(title="Hokma", version=__version__)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    app.include_router(rpc_router)
    app.include_router(update_router)

    dist = dashboard_dist()
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")
    return app
