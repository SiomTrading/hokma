"""Server for the chat E2E: the real app (create_app) on a free port, a compiled dashboard folder, temporary
HOKMA_HOME / HOKMA_DATA_DIR and a fake `claude` first on PATH (all set by the runner).

    python e2e/serve.py <dist-folder> <port>

Prints one JSON line with the session token once it is listening, then serves until it is killed.
"""
import json
import sys
from pathlib import Path

import uvicorn

dist, port = Path(sys.argv[1]), int(sys.argv[2])

import hokma.core.app as app_module  # noqa: E402
from hokma.core.config import hokma_home  # noqa: E402
from hokma.db import migrate, open_database  # noqa: E402
from hokma.core import session  # noqa: E402
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
    u.agents.create_custom("risk-desk", "Risk desk", description="Watches the limits")
    u.agents.set_status("risk-desk", "active")
from hokma.projects import service  # noqa: E402

service.ensure_default(db)                      # "principal", named after the fund
service.create_project("Strategy", "other", slug="strategy", db=db)
db.close()

print(json.dumps({"token": SESSION_TOKEN, "once": ONCE, "home": str(home)}), flush=True)
uvicorn.run(app_module.create_app(port), host="127.0.0.1", port=port, log_level="warning")
