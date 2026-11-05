"""Server startup, localhost only."""
import uvicorn

from hokma.core.app import create_app
from hokma.core.config import DEFAULT_HOST, DEFAULT_PORT


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
