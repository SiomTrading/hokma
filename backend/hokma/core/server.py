"""Server startup, localhost only."""
import uvicorn

from hokma.core.app import create_app
from hokma.core.config import DEFAULT_HOST, DEFAULT_PORT
from hokma.core.lifecycle import Lifecycle

GRACEFUL_SHUTDOWN_SECONDS = 5


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, lifecycle: Lifecycle | None = None) -> int:
    """Serve until stopped. Returns the exit code the process should end with: 75 after an update (the supervisor
    starts the new version), 0 otherwise."""
    # access_log off: the access log prints the full URL, and the session token travels in ?token=
    app = create_app(port, lifecycle=lifecycle, auto_update_check=True)
    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False,
                            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS)
    server = uvicorn.Server(config)
    app.state.lifecycle.server = server
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return app.state.lifecycle.exit_code
