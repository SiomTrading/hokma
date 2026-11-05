"""Server startup, localhost only."""
import uvicorn

from hokma.core.app import create_app
from hokma.core.config import DEFAULT_HOST, DEFAULT_PORT


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    # access_log off: the access log prints the full URL, and the session token travels in ?token=
    uvicorn.run(create_app(port), host=host, port=port, log_level="info", access_log=False)
