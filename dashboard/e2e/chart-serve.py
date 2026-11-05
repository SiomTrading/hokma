"""Server for the chart E2E: the real app (create_app) on a free port, a compiled dashboard folder, temporary
HOKMA_HOME / HOKMA_DATA_DIR, a fake `claude` first on PATH and HOKMA_MARKET_REPLAY_DIR pointing at recorded market
answers (all set by the runner). No network is used.

    python e2e/chart-serve.py <dist-folder> <port>

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
from hokma.projects import service  # noqa: E402
from hokma.rpc.ws import SESSION_TOKEN  # noqa: E402

app_module.dashboard_dist = lambda: dist
migrate()
home = hokma_home()
home.mkdir(parents=True, exist_ok=True)
db = open_database()
with db.write() as u:
    u.profile.set_owner("E2E Owner", "E2E Fund")
    u.profile.complete_setup()
service.ensure_default(db)
db.close()

print(json.dumps({"token": SESSION_TOKEN, "home": str(home)}), flush=True)
uvicorn.run(app_module.create_app(port), host="127.0.0.1", port=port, log_level="warning")
