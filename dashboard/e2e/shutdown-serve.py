"""Server for the shutdown E2E: like serve.py, but the uvicorn server is wired into the lifecycle the way `hokma engine`
does it (hokma.core.server.run), so the shut down button really ends the process.

    python e2e/shutdown-serve.py <dist-folder> <port>

Prints one JSON line with the session token once it is listening, then serves until the shutdown or until it is killed.
"""
import json
import sys
from pathlib import Path

import uvicorn

dist, port = Path(sys.argv[1]), int(sys.argv[2])

import hokma.core.app as app_module  # noqa: E402
from hokma.core import session  # noqa: E402
from hokma.core.config import hokma_home  # noqa: E402
from hokma.db import migrate, open_database  # noqa: E402
from hokma.projects import service  # noqa: E402
from hokma.rpc.ws import SESSION_TOKEN  # noqa: E402

app_module.dashboard_dist = lambda: dist
ONCE = session.new_owner_secret()                       # the one-time code of the start link, for the tab
session.arm_owner_secret_for_tests(ONCE)
migrate()
home = hokma_home()
home.mkdir(parents=True, exist_ok=True)
db = open_database()
with db.write() as u:
    u.profile.set_owner("E2E Owner", "E2E Fund")
    u.profile.complete_setup()
service.ensure_default(db)
db.close()

app = app_module.create_app(port)
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=5))
app.state.lifecycle.server = server
print(json.dumps({"token": SESSION_TOKEN, "once": ONCE, "home": str(home)}), flush=True)
server.run()
sys.exit(app.state.lifecycle.exit_code)
